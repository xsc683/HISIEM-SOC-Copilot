"""MITRE ATT&CK release import (brief sections 33-38).

An import does TWO related but separate things, and keeping them separate is the
point of this handler:

1. It writes the CANONICAL technique rows for one pinned release
   (``attack_technique``): name, description, tactics, platforms, provenance,
   content hash.
2. It asks the ordinary ingestion use case to store each technique as a GLOBAL
   ``MITRE_ATTACK`` knowledge document. Those documents are retrieval content.

The same technique therefore has a canonical row and a document, and they are
RELATED but not the same authority (section 38). This handler never writes a
document itself -- it delegates to :class:`KnowledgeIngestionHandler`, so a
technique document is versioned, chunked, embedded and hashed by exactly the same
code path as an operator's runbook. There is no second ingestion path to keep
honest, and the interface layer is never the thing that decides what a document
says.

Ordering and failure: the canonical rows are committed first, in one short
transaction, and the documents follow one at a time. A crash between the two
leaves a complete release whose documents are partially ingested -- which a
re-run converges, because both halves are idempotent. The reverse order would
leave documents belonging to no canonical release, which no re-run could repair.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from ...domain.knowledge.enums import SourceKind, Visibility
from ...domain.knowledge.value_objects import normalize_and_hash
from ..commands.knowledge import ImportAttackRelease, IngestKnowledgeDocument
from ..errors import ApplicationError
from ..ports.attack import FRAMEWORK, AttackBundle, AttackBundleParser, AttackTechnique
from ..ports.clock import ClockPort
from ..ports.knowledge import AttackImportOutcome, AttackTechniqueRecord
from ..ports.unit_of_work import UnitOfWork
from ..services.attack_projection import (
    technique_document_body,
    technique_document_title,
    technique_external_key,
)
from .knowledge import KnowledgeIngestionHandler

#: A real Enterprise bundle is a few MB; this bound exists so a mistaken path (a
#: directory listing, the 100MB+ upstream dump, an unrelated binary) fails fast
#: and explicitly instead of being parsed.
DEFAULT_MAX_BUNDLE_BYTES = 64 * 1024 * 1024

#: Language tag for MITRE's English-language technique descriptions.
_TECHNIQUE_LANGUAGE = "en"


@dataclass(frozen=True)
class AttackImportLimits:
    """Operator-tunable bounds for one import."""

    max_bundle_bytes: int = DEFAULT_MAX_BUNDLE_BYTES


class AttackImportHandler:
    """Import one local ATT&CK STIX bundle into canonical rows + documents."""

    def __init__(
        self,
        *,
        unit_of_work_factory: Callable[[], UnitOfWork],
        parser: AttackBundleParser,
        ingestion: KnowledgeIngestionHandler,
        clock: ClockPort,
        limits: AttackImportLimits | None = None,
    ) -> None:
        self._uow_factory = unit_of_work_factory
        self._parser = parser
        self._ingestion = ingestion
        self._clock = clock
        self._limits = limits or AttackImportLimits()

    async def import_release(self, command: ImportAttackRelease) -> AttackImportOutcome:
        """Import ``command.payload`` as release ``command.release``.

        Idempotent on both halves: re-importing the same release with the same
        bundle creates no duplicate canonical rows and no duplicate document
        versions, and reports each recognised technique as ``unchanged``.
        """
        if not command.release or not command.release.strip():
            raise ApplicationError("an ATT&CK import requires a pinned release name")
        if command.framework != FRAMEWORK:
            raise ApplicationError(
                f"unsupported ATT&CK framework {command.framework!r}; only "
                f"{FRAMEWORK!r} is imported"
            )
        if len(command.payload) > self._limits.max_bundle_bytes:
            raise ApplicationError(
                f"ATT&CK bundle exceeds the {self._limits.max_bundle_bytes} byte "
                "import bound; refusing to parse it"
            )

        # A malformed or unsupported bundle raises InvalidMitreBundleError HERE,
        # before anything is written: a bad bundle is never partially imported.
        bundle = self._parser.parse(command.payload)

        created = await self._persist_release(command, bundle)
        documents_created, versions_ingested, unchanged = await self._ingest_documents(
            command, bundle
        )

        return AttackImportOutcome(
            release=command.release,
            techniques_parsed=len(bundle.techniques),
            techniques_created=created,
            documents_created=documents_created,
            versions_ingested=versions_ingested,
            unchanged=unchanged,
            skipped=bundle.skipped_ids,
        )

    # ------------------------------------------------------------------
    # canonical technique rows
    # ------------------------------------------------------------------
    async def _persist_release(self, command: ImportAttackRelease, bundle: AttackBundle) -> int:
        """Write the release's canonical rows; return how many were new.

        The whole release lands in ONE transaction, including the optional
        activation switch, so an operator can never observe a release that is
        half-inserted or half-activated.
        """
        now = self._clock.utc_now()
        async with self._uow_factory() as uow:
            existing = await uow.attack_techniques.count_for_release(
                framework=command.framework, source_release=command.release
            )
            created = 0
            if existing == 0:
                records = tuple(
                    _record_of(technique, command.release, command.framework, now)
                    for technique in bundle.techniques
                )
                await uow.attack_techniques.add_many(techniques=records)
                created = len(records)
            if command.activate:
                # Section 38: switching which release is authoritative is an
                # explicit act. Rows are flagged, never deleted, so an older
                # release stays readable by its own ``source_release``.
                await uow.attack_techniques.deactivate_except(
                    framework=command.framework, source_release=command.release
                )
            await uow.commit()
        return created

    # ------------------------------------------------------------------
    # knowledge documents
    # ------------------------------------------------------------------
    async def _ingest_documents(
        self, command: ImportAttackRelease, bundle: AttackBundle
    ) -> tuple[int, int, int]:
        """Ingest one GLOBAL document per technique through the ordinary path."""
        documents_created = 0
        versions_ingested = 0
        unchanged = 0
        for technique in bundle.techniques:
            outcome = await self._ingestion.ingest(
                IngestKnowledgeDocument(
                    source_kind=SourceKind.MITRE_ATTACK,
                    external_key=technique_external_key(technique),
                    visibility=Visibility.GLOBAL,
                    title=technique_document_title(technique),
                    content=technique_document_body(technique),
                    # GLOBAL: a MITRE technique belongs to no tenant.
                    tenant_id=None,
                    language=_TECHNIQUE_LANGUAGE,
                    source_version=command.release,
                    metadata={
                        "framework": command.framework,
                        "technique_id": technique.technique_id,
                        "stix_id": technique.stix_id,
                        "tactics": list(technique.tactics),
                        "platforms": list(technique.platforms),
                    },
                )
            )
            documents_created += int(outcome.document_created)
            versions_ingested += int(outcome.version_created)
            if not outcome.version_created:
                unchanged += 1
        return documents_created, versions_ingested, unchanged


def _record_of(
    technique: AttackTechnique, release: str, framework: str, now: datetime
) -> AttackTechniqueRecord:
    """Build the canonical row for one technique.

    The content hash is the DOMAIN hash of the same body the document is ingested
    from, computed by the one hashing rule (section 8) rather than by a second
    implementation living here.
    """
    _, content_hash = normalize_and_hash(technique_document_body(technique))
    return AttackTechniqueRecord(
        id=uuid4(),
        framework=framework,
        technique_id=technique.technique_id,
        source_release=release,
        name=technique.name,
        description=technique.description,
        tactics=technique.tactics,
        platforms=technique.platforms,
        source_stix_id=technique.stix_id,
        content_hash=content_hash,
        active=True,
        created_at=now,
    )
