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

from ...domain.knowledge.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from ...domain.knowledge.enums import DocumentStatus, SourceKind, Visibility
from ...domain.knowledge.value_objects import normalize_and_hash
from ..commands.knowledge import ImportAttackRelease, IngestKnowledgeDocument
from ..errors import (
    ApplicationError,
    AttackReleaseContentConflictError,
    AttackReleaseProjectionIncompleteError,
    AttackReleaseProjectionInvalidBindingError,
    AttackReleaseProjectionMissingVersionError,
)
from ..ports.attack import FRAMEWORK, AttackBundle, AttackBundleParser, AttackTechnique
from ..ports.clock import ClockPort
from ..ports.knowledge import (
    ATTACK_RELEASE_STATUS_INACTIVE,
    AttackImportOutcome,
    AttackReleaseProjectionRecord,
    AttackReleaseRecord,
    AttackTechniqueRecord,
)
from ..ports.unit_of_work import UnitOfWork
from ..services.attack_projection import (
    technique_document_body,
    technique_document_title,
    technique_external_key,
    technique_external_key_for_id,
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
        documents_created, versions_ingested, unchanged = await self._stage_documents(
            command, bundle
        )
        if outcome.wants_authority:
            # The ONLY transition that makes a release authoritative, and the only
            # thing that moves a document pointer. It runs after the projection is
            # complete and does both in ONE transaction, so canonical authority and
            # what retrieval serves switch together or not at all -- there is no
            # window in which they disagree (brief section 2.7).
            await self._cutover(command)

        return AttackImportOutcome(
            release=command.release,
            techniques_parsed=len(bundle.techniques),
            techniques_created=outcome.created,
            documents_created=documents_created,
            versions_ingested=versions_ingested,
            unchanged=unchanged,
            skipped=bundle.skipped_ids,
            release_active=outcome.wants_authority,
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
            # "This import HAS the authority intent" -- it does not itself make
            # the release authoritative. Activation is a separate, atomic
            # transition that runs only once the projection is complete (brief
            # sections 2.5/2.7).
            wants_authority = command.activate or (
                existing is not None and existing.is_active
            )
            # The mirror records the release's CURRENT STORED authority, never the
            # import's intent: a release under registration is INACTIVE until the
            # cutover makes it otherwise, so its rows must not claim authority
            # early (brief section 2.3).
            stored_authority = existing is not None and existing.is_active
            # The canonical rows are built BEFORE the fingerprint, because the
            # fingerprint is defined over exactly these rows. One function computes
            # each row's content hash and one function fingerprints the collection,
            # so the release fingerprint and the document projected from those rows
            # cannot describe different content (brief section 2.3).
            records = tuple(
                _record_of(technique, release, framework, now, active=stored_authority)
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

            # The mirror and the authority flip are deliberately NOT written here.
            # They belong to the cutover, which is the only act that changes what
            # retrieval serves; doing either here would let an import that never
            # reaches the cutover leave the framework claiming an authority whose
            # content retrieval does not return (brief sections 2.3/2.7).

            await uow.commit()

        return _ReleaseOutcome(
            created=created,
            release_created=release_created,
            wants_authority=wants_authority,
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
    async def _stage_documents(
        self, command: ImportAttackRelease, bundle: AttackBundle
    ) -> tuple[int, int, int]:
        """Project each technique into knowledge WITHOUT changing retrieval.

        Every version, chunk and embedding is created by the ordinary ingestion
        path -- there is no second ingestion path to keep honest -- but with
        ``activate_version=False``, so none of it becomes the version retrieval
        serves. The binding rows written at the end record which version THIS
        release staged, which is what the cutover later moves the pointers to and
        what makes re-activating an older release restore its own projection
        rather than a re-derived one (brief sections 2.5/2.6).
        """
        documents_created = 0
        versions_ingested = 0
        unchanged = 0
        projections: list[AttackReleaseProjectionRecord] = []
        for technique in bundle.techniques:
            outcome = await self._ingestion.ingest(
                IngestKnowledgeDocument(
                    activate_version=False,
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
            projections.append(
                AttackReleaseProjectionRecord(
                    id=uuid4(),
                    framework=command.framework,
                    source_release=command.release,
                    technique_id=technique.technique_id,
                    document_id=outcome.document.id,
                    document_version_id=outcome.version.id,
                    content_hash=outcome.version.content_hash,
                    created_at=self._clock.utc_now(),
                )
            )

        # One short transaction for the whole release's bindings. Recording them
        # idempotently means a retry after a crash between here and the cutover
        # converges: the documents already staged stay, the bindings are
        # re-derived from the same content hashes, and nothing is duplicated
        # (brief section 2.8). Doing this AFTER the loop rather than per technique
        # keeps a 214-technique import to one extra transaction, and the cutover's
        # completeness check fails closed if it never ran.
        if projections:
            async with self._uow_factory() as uow:
                await uow.attack_release_projections.record_many(
                    projections=projections
                )
                await uow.commit()

        return documents_created, versions_ingested, unchanged

    # ------------------------------------------------------------------
    # the atomic cutover
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # cutover validation: relational identity + retrievability
    # ------------------------------------------------------------------
    @staticmethod
    def _require_binding_identity(
        *,
        framework: str,
        release: str,
        binding: AttackReleaseProjectionRecord,
        document: KnowledgeDocument,
        version: KnowledgeDocumentVersion,
    ) -> None:
        """Refuse a binding whose rows exist but whose relationship is invalid.

        Every binding names a document AND a version. A cutover that trusted the
        names without checking the relationship could point a technique's document
        at another document's version, or point a runbook at an ATT&CK release --
        and retrieval would serve it confidently either way. Each predicate below
        is one way that failure looks, and all of them run BEFORE any authority
        mutation (closure-3 sections 13-16).
        """
        expected = technique_external_key_for_id(binding.technique_id)
        if (
            version.document_id != binding.document_id
            or document.source_kind is not SourceKind.MITRE_ATTACK
            or document.visibility is not Visibility.GLOBAL
            or document.tenant_id is not None
            or document.status is not DocumentStatus.ACTIVE
            or document.external_key != expected
            or version.content_hash != binding.content_hash
        ):
            raise AttackReleaseProjectionInvalidBindingError(
                f"ATT&CK release {release!r} binds {binding.technique_id} to a "
                "document version that is not this release's projection: the "
                "version does not belong to the bound document, the document is "
                "not a GLOBAL ACTIVE MITRE_ATTACK projection target, its "
                "external key is not this technique's canonical key, or the "
                "content-hash chain canonical == binding == version is broken. "
                "Nothing was changed."
            )

    @staticmethod
    async def _require_retrievable(
        uow: UnitOfWork,
        *,
        framework: str,
        release: str,
        binding: AttackReleaseProjectionRecord,
        version: KnowledgeDocumentVersion,
        active_profile_id: object,
    ) -> None:
        """Refuse to make a release authoritative for content it cannot serve.

        A version row existing is not the same as a version being retrievable.
        Cutting authority over to a version with no chunks -- or with chunks the
        current embedding space does not cover -- would serve nothing while
        claiming everything. The projection state is REUSED from the ingestion
        path rather than re-derived in SQL, so there is exactly one definition
        of "projected" (closure-3 section 17).
        """
        del framework  # the lock scope, not a lookup key here
        state = await uow.knowledge_chunks.projection_state(
            document_version_id=version.id
        )
        if state.is_empty or (
            active_profile_id is not None and state.embedding_profile_id is None
        ):
            raise AttackReleaseProjectionIncompleteError(
                f"ATT&CK release {release!r} cannot be made authoritative: "
                f"{binding.technique_id} has no retrievable projection "
                "(no chunks, or no embedding space fully covers the current "
                "generation). Nothing was changed."
            )

    async def _cutover(self, command: ImportAttackRelease) -> None:
        """Make a release authoritative AND point retrieval at its projection.

        ONE transaction, in this order, because the order is the guarantee:

        1. take the framework's cutover lock, so two activations of the same
           framework serialize instead of interleaving their pointer writes;
        2. VALIDATE before mutating anything -- every canonical technique has a
           binding, the binding count matches the release's declared technique
           count, every bound document is still ACTIVE, and every binding's
           content hash still equals its canonical row's. A failure here raises
           with nothing written, so authority and retrieval are both untouched;
        3. flip the release authority and mirror it onto the technique rows;
        4. move each document's pointer through the domain aggregate, so the
           version-ingested audit event is emitted exactly as for an ordinary
           ingest.

        Because (3) and (4) commit together, no reader can observe the release
        authoritative while retrieval still serves the previous release -- the
        divergence this closure exists to close (brief sections 2.7/2.8).
        """
        framework = command.framework
        release = command.release
        now = self._clock.utc_now()

        async with self._uow_factory() as uow:
            await uow.attack_release_projections.lock_framework(framework=framework)

            stored = await uow.attack_releases.find(
                framework=framework, source_release=release
            )
            if stored is None:
                raise ApplicationError(
                    f"ATT&CK release {release!r} was not registered; refusing to "
                    "activate a release with no canonical rows"
                )

            missing = await uow.attack_release_projections.missing_techniques(
                framework=framework, source_release=release
            )
            staged = await uow.attack_release_projections.count_for_release(
                framework=framework, source_release=release
            )
            unusable = await uow.attack_release_projections.unusable_documents(
                framework=framework, source_release=release
            )
            if missing or staged != stored.technique_count or unusable:
                raise AttackReleaseProjectionIncompleteError(
                    _incomplete_message(
                        release=release,
                        stored=stored.technique_count,
                        staged=staged,
                        missing=missing,
                        unusable=unusable,
                    )
                )

            # The canonical hash and the version's hash are computed by the same
            # domain function over the same body, so a disagreement means the
            # projection is not of this release's content. Checked here rather
            # than trusted, and before any mutation.
            canonical = {
                row.technique_id: row.content_hash
                for row in await uow.attack_techniques.list_for_release(
                    framework=framework, source_release=release
                )
            }
            bindings = await uow.attack_release_projections.list_for_release(
                framework=framework, source_release=release
            )
            for binding in bindings:
                if canonical.get(binding.technique_id) != binding.content_hash:
                    raise AttackReleaseProjectionMissingVersionError(
                        f"ATT&CK release {release!r} has a staged projection for "
                        f"{binding.technique_id} whose content hash does not match "
                        "its canonical row; refusing to activate a release whose "
                        "projection is not of its own content"
                    )

            # Resolve AND fully validate EVERY binding against its live version
            # and document BEFORE the first mutation. Resolution can fail -- a
            # binding whose foreign key was somehow bypassed, a document that no
            # longer resolves -- and validation can fail even when every row
            # exists: the version may belong to a different document, the document
            # may not be a GLOBAL MITRE_ATTACK projection target, its external key
            # may not be this technique's canonical key, or the hash chain
            # canonical == binding == version may be broken. A refusal must leave
            # the framework's authority untouched. Relying on the transaction to
            # roll a flipped release back would make that guarantee a property of
            # the caller's UnitOfWork rather than of this method, which is not a
            # guarantee at all (closure-3 sections 13-18).
            active_profile = await uow.embedding_profiles.get_active()
            resolved: list[tuple[KnowledgeDocument, KnowledgeDocumentVersion]] = []
            for binding in bindings:
                version = await uow.knowledge_documents.get_version(
                    tenant_id=None, document_version_id=binding.document_version_id
                )
                if version is None:
                    raise AttackReleaseProjectionMissingVersionError(
                        f"ATT&CK release {release!r} binds {binding.technique_id} to "
                        "a document version that does not resolve"
                    )
                document = await uow.knowledge_documents.get(
                    tenant_id=None, document_id=binding.document_id
                )
                if document is None:
                    raise AttackReleaseProjectionMissingVersionError(
                        f"ATT&CK release {release!r} binds {binding.technique_id} to "
                        "a document that does not resolve"
                    )
                self._require_binding_identity(
                    framework=framework,
                    release=release,
                    binding=binding,
                    document=document,
                    version=version,
                )
                await self._require_retrievable(
                    uow,
                    framework=framework,
                    release=release,
                    binding=binding,
                    version=version,
                    active_profile_id=(
                        active_profile.id if active_profile is not None else None
                    ),
                )
                resolved.append((document, version))

            releases = await uow.attack_releases.list_for_framework(
                framework=framework
            )
            for other in releases:
                if other.source_release != release:
                    await uow.attack_techniques.set_active_for_release(
                        framework=framework,
                        source_release=other.source_release,
                        active=False,
                    )
            await uow.attack_techniques.set_active_for_release(
                framework=framework, source_release=release, active=True
            )
            # Only a REAL transition is written. Re-importing an already
            # authoritative release converges -- it re-asserts the mirror, moves
            # no pointer that already matches -- but restamping ``activated_at``
            # would make "when did this become authoritative" drift on every
            # idempotent re-run.
            if not stored.is_active:
                await uow.attack_releases.activate(
                    framework=framework, source_release=release, now=now
                )

            for document, version in resolved:
                if document.active_version_id == version.id:
                    # Already serving this release's projection: an idempotent
                    # re-run of an activation converges without a second write.
                    continue
                document.activate_version(
                    version_id=version.id,
                    version=version.version,
                    content_hash=version.content_hash,
                    title=version.title,
                )
                await uow.knowledge_documents.save(document=document)
                for event in document.pending_events:
                    await uow.events.append(
                        event, aggregate_revision=document.revision
                    )
                document.clear_events()

            await uow.commit()


@dataclass(frozen=True)
class _ReleaseOutcome:
    """What ``_persist_release`` did, for the caller's report."""

    created: int
    release_created: bool
    #: Whether this import has the AUTHORITY INTENT. It is not the release's
    #: status at this point -- the release is still INACTIVE until the cutover.
    wants_authority: bool
    content_fingerprint: str


def _incomplete_message(
    *,
    release: str,
    stored: int,
    staged: int,
    missing: tuple[str, ...],
    unusable: tuple[str, ...],
) -> str:
    """Refusal text naming what is missing, without dumping any content.

    Technique ids and external keys are identifiers, not knowledge, so they are
    safe to print and are the only way an operator can tell "the projection never
    finished" from "a document was retired underneath this release".
    """
    parts = [
        f"ATT&CK release {release!r} cannot be made authoritative because its "
        f"knowledge projection is not complete: {staged} of {stored} canonical "
        "techniques are staged"
    ]
    if missing:
        parts.append(f"missing projection for {', '.join(missing)}")
    if unusable:
        parts.append(
            "bound documents that can no longer serve retrieval: "
            + ", ".join(unusable)
        )
    parts.append(
        "nothing was changed; re-run the import to finish staging, or retire the "
        "release explicitly"
    )
    return "; ".join(parts)


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
