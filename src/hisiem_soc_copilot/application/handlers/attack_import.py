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

Ordering and failure: the canonical rows and the release record are committed
first, in one short transaction, and the documents follow one at a time. A crash
between the two leaves a complete release whose documents are partially ingested
-- which a re-run converges, because both halves are idempotent. The reverse order
would leave documents belonging to no canonical release, which no re-run could
repair.

Release integrity (brief section 2) adds three rules on top of that:

* Authority is a property of the RELEASE, not of each technique row. At most one
  release per framework is ACTIVE, enforced by a per-framework partial unique
  index, so ``activate=False`` cannot leave two authoritative releases behind.
* A pinned release is IMMUTABLE. Each release carries a deterministic fingerprint
  of its canonical technique collection; the same release with the same
  fingerprint converges, and the same release with different content fails closed
  with ``ATTACK_RELEASE_CONTENT_CONFLICT`` before anything is written.
* The technique rows' ``active`` flag MIRRORS the release's authority and is never
  an independent source of it, so the two cannot disagree after a switch.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from ...domain.knowledge.enums import SourceKind, Visibility
from ...domain.knowledge.value_objects import normalize_and_hash
from ..commands.knowledge import ImportAttackRelease, IngestKnowledgeDocument
from ..errors import ApplicationError, AttackReleaseContentConflictError
from ..ports.attack import FRAMEWORK, AttackBundle, AttackBundleParser, AttackTechnique
from ..ports.clock import ClockPort
from ..ports.knowledge import (
    ATTACK_RELEASE_STATUS_INACTIVE,
    AttackImportOutcome,
    AttackReleaseRecord,
    AttackTechniqueRecord,
)
from ..ports.unit_of_work import UnitOfWork
from ..services.attack_projection import (
    technique_document_body,
    technique_document_title,
    technique_external_key,
)
from ..services.attack_release_fingerprint import fingerprint_from_records
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

        outcome = await self._persist_release(command, bundle)
        documents_created, versions_ingested, unchanged = await self._ingest_documents(
            command, bundle
        )

        return AttackImportOutcome(
            release=command.release,
            techniques_parsed=len(bundle.techniques),
            techniques_created=outcome.created,
            documents_created=documents_created,
            versions_ingested=versions_ingested,
            unchanged=unchanged,
            skipped=bundle.skipped_ids,
            release_active=outcome.release_active,
            content_fingerprint=outcome.content_fingerprint,
            release_created=outcome.release_created,
        )

    # ------------------------------------------------------------------
    # canonical technique rows + release authority
    # ------------------------------------------------------------------
    async def _persist_release(
        self, command: ImportAttackRelease, bundle: AttackBundle
    ) -> _ReleaseOutcome:
        """Write the release and its canonical rows; return what changed.

        The whole release lands in ONE transaction, including the optional
        activation switch, so an operator can never observe a release that is
        half-inserted or half-activated.

        The immutability check runs BEFORE the first mutation: a release name whose
        existing content differs from this bundle is refused outright, so a
        rejected import leaves the canonical rows, the knowledge documents, the
        document versions, the active release and the embedding projections all
        exactly as they were (brief sections 2.2/2.3).
        """
        now = self._clock.utc_now()
        framework = command.framework
        release = command.release

        async with self._uow_factory() as uow:
            # Every READ happens before the first write, so a refused import leaves
            # the canonical rows, the knowledge documents, the document versions,
            # the active release and the embedding projections exactly as they were
            # (brief sections 2.2/2.3).
            existing = await uow.attack_releases.find(
                framework=framework, source_release=release
            )
            others = tuple(
                item
                for item in await uow.attack_releases.list_for_framework(
                    framework=framework
                )
                if item.source_release != release
            )

            active = command.activate or (
                existing is not None and existing.is_active
            )
            # The canonical rows are built BEFORE the fingerprint, because the
            # fingerprint is defined over exactly these rows. One function computes
            # each row's content hash and one function fingerprints the collection,
            # so the release fingerprint and the document projected from those rows
            # cannot describe different content (brief section 2.3).
            records = tuple(
                _record_of(technique, release, framework, now, active=active)
                for technique in bundle.techniques
            )
            fingerprint = fingerprint_from_records(
                framework=framework,
                source_release=release,
                techniques=records,
            )
            if existing is not None:
                # Read-only verification: adopting a pre-fingerprint release must
                # re-derive what is STORED, never trust the incoming bundle.
                await self._verify_pinned_release(
                    uow, existing, fingerprint=fingerprint, framework=framework
                )

            release_created = False
            if existing is None:
                # Inserted INACTIVE and activated by an explicit UPDATE afterwards,
                # so activating can never race the per-framework partial unique
                # index (which forbids two ACTIVE releases at COMMIT).
                await uow.attack_releases.add(
                    release=AttackReleaseRecord(
                        id=uuid4(),
                        framework=framework,
                        source_release=release,
                        content_fingerprint=fingerprint,
                        status=ATTACK_RELEASE_STATUS_INACTIVE,
                        technique_count=len(bundle.techniques),
                        created_at=now,
                        activated_at=None,
                    )
                )
                release_created = True

            # Only a real transition is written: re-importing an already-active
            # release must not restamp its activation time, or "when did this
            # become authoritative" would drift on every idempotent re-run.
            transitions_to_active = active and (
                existing is None or not existing.is_active
            )

            created = 0
            if existing is None or (
                await uow.attack_techniques.count_for_release(
                    framework=framework, source_release=release
                )
                == 0
            ):
                # The very same rows the fingerprint was taken over.
                await uow.attack_techniques.add_many(techniques=records)
                created = len(records)

            # The technique rows' flag is a MIRROR of release authority, so it is
            # re-asserted on every path rather than left to depend on how the rows
            # happened to be written.
            #
            # Only an import that LEAVES this release authoritative may clear the
            # framework's others. Staging a release (``activate=False``) is not the
            # same act as promoting it, and deposing the current release as a side
            # effect of importing a bundle an operator has not adopted would be a
            # silent authority change (brief section 2.1).
            if active:
                for other in others:
                    await uow.attack_techniques.set_active_for_release(
                        framework=framework,
                        source_release=other.source_release,
                        active=False,
                    )
            await uow.attack_techniques.set_active_for_release(
                framework=framework, source_release=release, active=active
            )
            if transitions_to_active:
                await uow.attack_releases.activate(
                    framework=framework, source_release=release, now=now
                )

            await uow.commit()

        return _ReleaseOutcome(
            created=created,
            release_created=release_created,
            release_active=active,
            content_fingerprint=fingerprint,
        )

    async def _verify_pinned_release(
        self,
        uow: UnitOfWork,
        existing: AttackReleaseRecord,
        *,
        fingerprint: str,
        framework: str,
    ) -> None:
        """Fail closed unless this bundle IS the already-pinned release.

        An adopted release (fingerprint ``None``, written before this table
        existed) is verified against what the database actually stores and then
        pinned; a pinned release is compared directly. Both paths read only, so a
        conflict leaves zero mutations behind.
        """
        if existing.content_fingerprint is None:
            stored = await uow.attack_techniques.list_for_release(
                framework=framework, source_release=existing.source_release
            )
            stored_fingerprint = fingerprint_from_records(
                framework=framework,
                source_release=existing.source_release,
                techniques=stored,
            )
            if stored_fingerprint != fingerprint:
                raise AttackReleaseContentConflictError(
                    _conflict_message(existing, fingerprint)
                )
            await uow.attack_releases.pin_fingerprint(
                framework=framework,
                source_release=existing.source_release,
                content_fingerprint=fingerprint,
            )
            return
        if existing.content_fingerprint != fingerprint:
            raise AttackReleaseContentConflictError(
                _conflict_message(existing, fingerprint)
            )

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


@dataclass(frozen=True)
class _ReleaseOutcome:
    """What ``_persist_release`` did, for the caller's report."""

    created: int
    release_created: bool
    release_active: bool
    content_fingerprint: str


def _conflict_message(existing: AttackReleaseRecord, fingerprint: str) -> str:
    """Refusal text that names the release without leaking either content set.

    The two fingerprints are safe to print (they are hashes, not content) and are
    the only way an operator can tell "someone genuinely changed the bundle" from
    "two different files were both labelled v14.1".
    """
    return (
        f"ATT&CK release {existing.source_release!r} is already pinned for "
        f"framework {existing.framework!r} with content fingerprint "
        f"{existing.content_fingerprint or 'UNKNOWN'}, but this bundle hashes to "
        f"{fingerprint}; a pinned release is immutable, so the import was "
        "refused without changing any canonical row or knowledge document"
    )


def _record_of(
    technique: AttackTechnique,
    release: str,
    framework: str,
    now: datetime,
    *,
    active: bool,
) -> AttackTechniqueRecord:
    """Build the canonical row for one technique.

    The content hash is the DOMAIN hash of the same body the document is ingested
    from, computed by the one hashing rule (section 8) rather than by a second
    implementation living here. It is also the field the release fingerprint binds,
    so the canonical row and the projected document version are demonstrably the
    same content -- the projection is a view of this row, not an independent
    authority (brief section 2.3).

    ``active`` mirrors the OWNING RELEASE's authority, which the caller has
    already resolved; a technique row never decides for itself whether it is
    authoritative.
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
        active=active,
        created_at=now,
    )
