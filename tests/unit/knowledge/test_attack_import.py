"""AttackImportHandler tests over a hand-written STIX bundle and in-memory doubles.

The bundle below is written out by hand in this module -- no fixture file, no
download, no upstream dump -- so what a release contains is visible in the test
rather than hidden behind a 100MB artefact.

The doubles are LOCAL to this module: an in-memory canonical-technique store, the
stores the REAL KnowledgeIngestionHandler needs, and a recording wrapper around
the REAL STIX parser. Ingestion is deliberately NOT faked: the importer's whole
job is to hand each technique to the ordinary ingestion use case, so the tests
run that use case for real and assert on the documents that actually land.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from hisiem_soc_copilot.application.commands.knowledge import ImportAttackRelease
from hisiem_soc_copilot.application.errors import (
    ApplicationError,
    AttackReleaseContentConflictError,
    AttackReleaseProjectionIncompleteError,
    AttackReleaseProjectionInvalidBindingError,
    AttackReleaseProjectionMissingVersionError,
)
from hisiem_soc_copilot.application.handlers.attack_import import (
    DEFAULT_MAX_BUNDLE_BYTES,
    AttackImportHandler,
    AttackImportLimits,
)
from hisiem_soc_copilot.application.handlers.knowledge import KnowledgeIngestionHandler
from hisiem_soc_copilot.application.ports.attack import FRAMEWORK, AttackBundle, AttackTechnique
from hisiem_soc_copilot.application.ports.embedding import (
    EmbeddingBatch,
    EmbeddingProfileDescriptor,
    EmbeddingVector,
)
from hisiem_soc_copilot.application.ports.knowledge import (
    ATTACK_RELEASE_STATUS_ACTIVE,
    ATTACK_RELEASE_STATUS_INACTIVE,
    AttackImportOutcome,
    AttackReleaseProjectionRecord,
    AttackReleaseRecord,
    AttackTechniqueRecord,
    ChunkEmbeddingRecord,
    ChunkProjectionState,
    EmbeddingProfileRecord,
    KnowledgeChunkView,
    KnowledgeContentChunkRecord,
    LexicalCandidate,
    VectorCandidate,
)
from hisiem_soc_copilot.application.services.attack_projection import (
    technique_document_body,
    technique_document_title,
    technique_external_key,
)
from hisiem_soc_copilot.application.services.attack_release_fingerprint import (
    fingerprint_from_records,
)
from hisiem_soc_copilot.domain.knowledge.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from hisiem_soc_copilot.domain.knowledge.enums import (
    DocumentStatus,
    SourceKind,
    Visibility,
)
from hisiem_soc_copilot.domain.knowledge.errors import InvalidMitreBundleError
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    CHUNK_GENERATION_INITIAL,
    compute_content_hash,
    normalize_and_hash,
    normalize_knowledge_content,
)
from hisiem_soc_copilot.infrastructure.knowledge.attack_source import MitreStixAttackSource
from hisiem_soc_copilot.infrastructure.knowledge.chunker_port import StructureAwareChunker

T0 = datetime(2026, 9, 12, 10, 0, 0, tzinfo=UTC)
RELEASE = "v15.1"

PROVIDER = "openai-compatible"
MODEL_ID = "text-embedding-test"
DIMENSION = 4

T1110_STIX_ID = "attack-pattern--11111111-1111-4111-8111-111111111111"
T1059_STIX_ID = "attack-pattern--22222222-2222-4222-8222-222222222222"
T1430_STIX_ID = "attack-pattern--33333333-3333-4333-8333-333333333333"
T1111_STIX_ID = "attack-pattern--44444444-4444-4444-8444-444444444444"
T1625_STIX_ID = "attack-pattern--55555555-5555-4555-8555-555555555555"

T1110_DESCRIPTION = "Adversaries may use brute force to obtain credentials."
T1059_DESCRIPTION = "Adversaries may abuse command and script interpreters."


# ---------------------------------------------------------------------------
# the hand-written STIX 2.1 bundle
# ---------------------------------------------------------------------------


def _attack_pattern(
    *,
    stix_id: str,
    technique_id: str,
    name: str,
    description: str,
    tactics: tuple[str, ...] = (),
    platforms: tuple[str, ...] = (),
    domains: tuple[str, ...] = ("enterprise-attack",),
    revoked: bool = False,
    deprecated: bool = False,
) -> dict[str, Any]:
    """One Enterprise attack-pattern object, as MITRE ships them."""
    pattern: dict[str, Any] = {
        "type": "attack-pattern",
        "id": stix_id,
        "name": name,
        "description": description,
        "external_references": [
            {
                "source_name": "mitre-attack",
                "external_id": technique_id,
                "url": f"https://attack.mitre.org/techniques/{technique_id}",
            }
        ],
        "kill_chain_phases": [
            {"kill_chain_name": "mitre-attack", "phase_name": tactic} for tactic in tactics
        ],
        "x_mitre_platforms": list(platforms),
    }
    if domains:
        pattern["x_mitre_domains"] = list(domains)
    if revoked:
        pattern["revoked"] = True
    if deprecated:
        pattern["x_mitre_deprecated"] = True
    return pattern


def _bundle(*objects: dict[str, Any], spec_version: str = "2.1") -> bytes:
    return json.dumps(
        {
            "type": "bundle",
            "id": "bundle--00000000-0000-4000-8000-000000000000",
            "spec_version": spec_version,
            "objects": list(objects),
        }
    ).encode("utf-8")


def _brute_force_object() -> dict[str, Any]:
    return _attack_pattern(
        stix_id=T1110_STIX_ID,
        technique_id="T1110",
        name="Brute Force",
        description=T1110_DESCRIPTION,
        tactics=("credential-access",),
        platforms=("Windows", "Linux"),
    )


def _command_shell_object() -> dict[str, Any]:
    return _attack_pattern(
        stix_id=T1059_STIX_ID,
        technique_id="T1059",
        name="Command and Scripting Interpreter",
        description=T1059_DESCRIPTION,
        tactics=("execution",),
        platforms=("Linux",),
    )


def _revoked_object() -> dict[str, Any]:
    return _attack_pattern(
        stix_id=T1430_STIX_ID,
        technique_id="T1430",
        name="Location Tracking",
        description="Withdrawn by MITRE.",
        revoked=True,
    )


def _deprecated_object() -> dict[str, Any]:
    return _attack_pattern(
        stix_id=T1111_STIX_ID,
        technique_id="T1111",
        name="Two-Factor Authentication Interception",
        description="Superseded by MITRE.",
        deprecated=True,
    )


def _mobile_object() -> dict[str, Any]:
    return _attack_pattern(
        stix_id=T1625_STIX_ID,
        technique_id="T1625",
        name="Implant Internal Image",
        description="Mobile-only technique.",
        domains=("mobile-attack",),
    )


def _identity_object() -> dict[str, Any]:
    return {
        "type": "identity",
        "id": "identity--66666666-6666-4666-8666-666666666666",
        "name": "MITRE ATT&CK",
    }


def _reordered_bundle() -> bytes:
    """The same release, with every object reversed and every JSON key sorted.

    Real MITRE exports are not byte-stable: object order and key order move
    between builds. A release that fingerprinted its INPUT BYTES could therefore
    never be re-imported, so this fixture is the adversarial version of "the same
    content in a different file" (brief section 2.2).
    """
    objects: list[dict[str, Any]] = [
        _brute_force_object(),
        _revoked_object(),
        _command_shell_object(),
        _deprecated_object(),
        _mobile_object(),
        _identity_object(),
    ]
    serialized = ",".join(json.dumps(obj, sort_keys=True) for obj in reversed(objects))
    return (
        '{"type":"bundle","spec_version":"2.1","objects":[' + serialized + "]}"
    ).encode("utf-8")


def _full_bundle() -> bytes:
    """Two in-scope techniques plus one object of each out-of-scope kind."""
    return _bundle(
        _brute_force_object(),
        _revoked_object(),
        _command_shell_object(),
        _deprecated_object(),
        _mobile_object(),
        _identity_object(),
    )


def _brute_force_technique() -> AttackTechnique:
    """The application-level value the bundle's T1110 object must produce."""
    return AttackTechnique(
        technique_id="T1110",
        stix_id=T1110_STIX_ID,
        name="Brute Force",
        description=T1110_DESCRIPTION,
        tactics=("credential-access",),
        platforms=("Linux", "Windows"),
    )


def _command_shell_technique() -> AttackTechnique:
    return AttackTechnique(
        technique_id="T1059",
        stix_id=T1059_STIX_ID,
        name="Command and Scripting Interpreter",
        description=T1059_DESCRIPTION,
        tactics=("execution",),
        platforms=("Linux",),
    )


#: Every id the full bundle must report as skipped, sorted the way the parser
#: reports them: a revoked technique, a deprecated one, and a mobile-domain one.
EXPECTED_SKIPPED = ("T1111", "T1430", "T1625")


# ---------------------------------------------------------------------------
# in-memory doubles
# ---------------------------------------------------------------------------


class FakeClock:
    """A frozen clock: canonical rows must carry a reproducible created_at."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def utc_now(self) -> datetime:
        return self._now


class FakeEmbeddingProvider:
    """A deterministic EmbeddingProvider double for the real ingestion handler."""

    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self._descriptor = EmbeddingProfileDescriptor(
            provider=PROVIDER,
            model_id=MODEL_ID,
            dimension=DIMENSION,
            distance_metric="COSINE",
            normalization="NONE",
            profile_version=1,
        )
        self.calls: list[tuple[str, ...]] = []
        #: Simulates a crash part-way through the document projection (brief
        #: section 2.4) by failing the Nth ``embed_documents`` call.
        self._fail_on_call = fail_on_call

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        return self._descriptor

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls.append(tuple(texts))
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise RuntimeError("embedding provider died mid-projection")
        return EmbeddingBatch(
            descriptor=self._descriptor,
            vectors=tuple(
                EmbeddingVector(
                    values=tuple(0.5 for _ in range(DIMENSION)),
                    descriptor=self._descriptor,
                    index=index,
                )
                for index in range(len(texts))
            ),
        )

    async def embed_query(self, text: str) -> EmbeddingVector:
        raise NotImplementedError("import never embeds a query")


class FakeEventLedger:
    """Records what ingestion appended; the importer itself appends nothing."""

    def __init__(self) -> None:
        self.appended: list[tuple[object, int]] = []

    async def append(
        self,
        event: object,
        *,
        aggregate_revision: int,
        available_at: datetime | None = None,
    ) -> None:
        self.appended.append((event, aggregate_revision))


class RecordingParser:
    """The REAL STIX reader, wrapped so a test can assert it was never called.

    A parser that was not invoked is the entire point of the oversized-payload
    test: refusing to parse is a different guarantee from failing to parse.
    """

    def __init__(self) -> None:
        self._source = MitreStixAttackSource()
        self.payloads: list[bytes] = []

    @property
    def calls(self) -> int:
        return len(self.payloads)

    def parse(self, payload: bytes) -> AttackBundle:
        self.payloads.append(payload)
        return self._source.parse(payload)


class FakeKnowledgeDocumentRepository:
    """In-memory document/version store for the real ingestion handler."""

    def __init__(self) -> None:
        self.documents: dict[UUID, KnowledgeDocument] = {}
        self.versions: dict[UUID, KnowledgeDocumentVersion] = {}
        self.added: list[UUID] = []
        self.saved: list[UUID] = []
        self.version_writes: list[UUID] = []

    async def get(self, *, tenant_id: str, document_id: UUID) -> KnowledgeDocument | None:
        document = self.documents.get(document_id)
        if document is None or not document.is_visible_to(tenant_id):
            return None
        return document

    async def find_by_external_key(
        self,
        *,
        tenant_id: str | None,
        source_kind: SourceKind,
        external_key: str,
        visibility: Visibility,
    ) -> KnowledgeDocument | None:
        for document in self.documents.values():
            if (
                document.tenant_id == tenant_id
                and document.source_kind is source_kind
                and document.external_key == external_key
                and document.visibility is visibility
            ):
                return document
        return None

    async def add(self, *, document: KnowledgeDocument) -> None:
        self.added.append(document.id)
        self.documents[document.id] = document

    async def save(self, *, document: KnowledgeDocument) -> None:
        self.saved.append(document.id)
        self.documents[document.id] = document

    async def next_version_number(self, *, document_id: UUID) -> int:
        numbers = [
            version.version
            for version in self.versions.values()
            if version.document_id == document_id
        ]
        return max(numbers, default=0) + 1

    async def add_version(self, *, version: KnowledgeDocumentVersion) -> None:
        self.version_writes.append(version.id)
        self.versions[version.id] = version

    async def find_version_by_content_hash(
        self, *, document_id: UUID, content_hash: str
    ) -> KnowledgeDocumentVersion | None:
        for version in self.versions.values():
            if version.document_id == document_id and version.content_hash == content_hash:
                return version
        return None

    async def get_version(
        self, *, tenant_id: str, document_version_id: UUID
    ) -> KnowledgeDocumentVersion | None:
        return self.versions.get(document_version_id)

    async def list_versions(
        self, *, tenant_id: str, document_id: UUID
    ) -> tuple[KnowledgeDocumentVersion, ...]:
        return tuple(
            version
            for version in self.versions.values()
            if version.document_id == document_id
        )


class FakeKnowledgeChunkRepository:
    """In-memory chunk store, split exactly as the two tables are.

    Content chunks (the immutable citation targets) and embeddings (the
    rebuildable projection) live in SEPARATE maps, and ``projection_state`` is
    DERIVED from them rather than scripted -- so a document that was chunked but
    not embedded reads as un-projected here, just as it does in SQL.
    """

    def __init__(self) -> None:
        self.chunks: dict[UUID, KnowledgeContentChunkRecord] = {}
        self.embeddings: dict[tuple[UUID, UUID], ChunkEmbeddingRecord] = {}
        self.added_batches: list[tuple[KnowledgeContentChunkRecord, ...]] = []
        self.added_embedding_batches: list[tuple[ChunkEmbeddingRecord, ...]] = []
        self.deleted_embeddings: list[tuple[UUID, int]] = []
        self.deleted_profiles: list[tuple[UUID, UUID]] = []

    # -- test-side helpers -------------------------------------------------

    def generation_of(self, document_version_id: UUID) -> int:
        generations = [
            chunk.generation
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        ]
        return max(generations) if generations else CHUNK_GENERATION_INITIAL

    def rows_for_version(
        self, document_version_id: UUID, generation: int | None = None
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        if generation is None:
            generation = self.generation_of(document_version_id)
        return tuple(
            sorted(
                (
                    chunk
                    for chunk in self.chunks.values()
                    if chunk.document_version_id == document_version_id
                    and chunk.generation == generation
                ),
                key=lambda chunk: chunk.ordinal,
            )
        )

    def profiles_for(self, chunk: KnowledgeContentChunkRecord) -> tuple[UUID, ...]:
        return tuple(
            sorted(
                embedding_profile_id
                for (chunk_id, embedding_profile_id) in self.embeddings
                if chunk_id == chunk.id
            )
        )

    # -- KnowledgeChunkRepository -----------------------------------------

    async def add_content_chunks(
        self, *, chunks: Sequence[KnowledgeContentChunkRecord]
    ) -> None:
        self.added_batches.append(tuple(chunks))
        for chunk in chunks:
            self.chunks[chunk.id] = chunk

    async def add_embeddings(self, *, embeddings: Sequence[ChunkEmbeddingRecord]) -> None:
        self.added_embedding_batches.append(tuple(embeddings))
        for embedding in embeddings:
            self.embeddings[
                (embedding.content_chunk_id, embedding.embedding_profile_id)
            ] = embedding

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        return len(self.rows_for_version(document_version_id))

    async def projection_state(self, *, document_version_id: UUID) -> ChunkProjectionState:
        rows = self.rows_for_version(document_version_id)
        if not rows:
            return ChunkProjectionState(None, None, 0)
        per_profile: dict[UUID, int] = {}
        for row in rows:
            for profile_id in self.profiles_for(row):
                per_profile[profile_id] = per_profile.get(profile_id, 0) + 1
        covering = [
            profile_id for profile_id, count in per_profile.items() if count == len(rows)
        ]
        return ChunkProjectionState(
            embedding_profile_id=covering[0] if len(covering) == 1 else None,
            chunker_version=rows[0].chunker_version,
            chunk_count=len(rows),
            generation=rows[0].generation,
            embedding_count=sum(per_profile.values()),
        )

    async def list_content_chunks(
        self, *, document_version_id: UUID, generation: int
    ) -> tuple[KnowledgeContentChunkRecord, ...]:
        return self.rows_for_version(document_version_id, generation)

    async def delete_embeddings_for_version_generation(
        self, *, document_version_id: UUID, generation: int
    ) -> int:
        """Drop ONE generation's EMBEDDING rows; content chunks are untouched."""
        self.deleted_embeddings.append((document_version_id, generation))
        chunk_ids = {
            chunk.id for chunk in self.rows_for_version(document_version_id, generation)
        }
        removed = [key for key in self.embeddings if key[0] in chunk_ids]
        for key in removed:
            del self.embeddings[key]
        return len(removed)

    async def delete_embeddings_for_profile(
        self, *, document_version_id: UUID, embedding_profile_id: UUID
    ) -> int:
        self.deleted_profiles.append((document_version_id, embedding_profile_id))
        chunk_ids = {
            chunk.id
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        }
        removed = [
            key
            for key in self.embeddings
            if key[0] in chunk_ids and key[1] == embedding_profile_id
        ]
        for key in removed:
            del self.embeddings[key]
        return len(removed)

    async def lexical_candidates(
        self, *, tenant_id: str, search_terms: Sequence[str], limit: int
    ) -> tuple[LexicalCandidate, ...]:
        raise NotImplementedError("import never searches")

    async def vector_candidates(
        self,
        *,
        tenant_id: str,
        embedding_profile_id: UUID,
        query_vector: Sequence[float],
        limit: int,
    ) -> tuple[VectorCandidate, ...]:
        raise NotImplementedError("import never searches")

    async def get_chunk_view(
        self, *, tenant_id: str, chunk_id: UUID
    ) -> KnowledgeChunkView | None:
        raise NotImplementedError("import never reads a chunk view")


class FakeEmbeddingProfileRepository:
    """In-memory embedding-profile store with at most one ACTIVE row."""

    def __init__(self) -> None:
        self.profiles: list[EmbeddingProfileRecord] = []

    async def get_active(self) -> EmbeddingProfileRecord | None:
        for profile in self.profiles:
            if profile.status == "ACTIVE":
                return profile
        return None

    async def find_by_identity(
        self,
        *,
        provider: str,
        model_id: str,
        dimension: int,
        distance_metric: str,
        normalization: str,
        profile_version: int,
    ) -> EmbeddingProfileRecord | None:
        for profile in self.profiles:
            if (
                profile.provider,
                profile.model_id,
                profile.dimension,
                profile.distance_metric,
                profile.normalization,
                profile.profile_version,
            ) == (
                provider,
                model_id,
                dimension,
                distance_metric,
                normalization,
                profile_version,
            ):
                return profile
        return None

    async def add(self, *, profile: EmbeddingProfileRecord) -> None:
        self.profiles.append(profile)

    async def retire_active(self, *, retired_at: datetime) -> None:
        self.profiles = [
            replace(profile, status="RETIRED", retired_at=retired_at)
            if profile.status == "ACTIVE"
            else profile
            for profile in self.profiles
        ]


class FakeAttackReleaseRepository:
    """In-memory ``attack_release`` store that ENFORCES the authority invariant.

    The real table carries a per-framework partial unique index, and that index is
    what makes "at most one authoritative release per framework" a database fact
    rather than a handler convention (brief section 2.1). Reproducing the rule in
    the double means a handler that could produce two authoritative releases fails
    here instead of only under the real constraint.
    """

    def __init__(self) -> None:
        self.releases: list[AttackReleaseRecord] = []
        self.pinned: list[tuple[str, str, str]] = []
        self.activations: list[tuple[str, str]] = []

    # -- test-side helpers -------------------------------------------------

    def get(self, *, framework: str, source_release: str) -> AttackReleaseRecord | None:
        for release in self.releases:
            if release.framework == framework and release.source_release == source_release:
                return release
        return None

    def active_for(self, framework: str) -> tuple[AttackReleaseRecord, ...]:
        return tuple(
            release
            for release in self.releases
            if release.framework == framework and release.is_active
        )

    # -- AttackReleaseRepository -------------------------------------------

    async def find(
        self, *, framework: str, source_release: str
    ) -> AttackReleaseRecord | None:
        return self.get(framework=framework, source_release=source_release)

    async def get_active(self, *, framework: str) -> AttackReleaseRecord | None:
        active = self.active_for(framework)
        return active[0] if active else None

    async def list_for_framework(
        self, *, framework: str
    ) -> tuple[AttackReleaseRecord, ...]:
        return tuple(
            release for release in self.releases if release.framework == framework
        )

    async def list_active(self) -> tuple[AttackReleaseRecord, ...]:
        return tuple(release for release in self.releases if release.is_active)

    async def add(self, *, release: AttackReleaseRecord) -> None:
        self.releases.append(release)
        self._assert_single_active()

    async def pin_fingerprint(
        self, *, framework: str, source_release: str, content_fingerprint: str
    ) -> None:
        self.pinned.append((framework, source_release, content_fingerprint))
        self._replace(
            framework=framework,
            source_release=source_release,
            content_fingerprint=content_fingerprint,
        )

    async def activate(self, *, framework: str, source_release: str, now: datetime) -> None:
        """Activate one release and deactivate the framework's others, in one pass."""
        self.activations.append((framework, source_release))
        self.releases = [
            replace(release, status=ATTACK_RELEASE_STATUS_ACTIVE, activated_at=now)
            if release.framework == framework and release.source_release == source_release
            else replace(release, status=ATTACK_RELEASE_STATUS_INACTIVE)
            if release.framework == framework
            else release
            for release in self.releases
        ]
        self._assert_single_active()

    def _replace(
        self, *, framework: str, source_release: str, content_fingerprint: str
    ) -> None:
        self.releases = [
            replace(release, content_fingerprint=content_fingerprint)
            if release.framework == framework and release.source_release == source_release
            else release
            for release in self.releases
        ]

    def _assert_single_active(self) -> None:
        seen: set[str] = set()
        for release in self.releases:
            if not release.is_active:
                continue
            assert release.framework not in seen, (
                "uq_attack_release_single_active: two ACTIVE releases for framework "
                f"{release.framework!r}"
            )
            seen.add(release.framework)


class FakeAttackTechniqueRepository:
    """In-memory canonical-technique store for the pinned releases."""

    def __init__(self) -> None:
        self.rows: list[AttackTechniqueRecord] = []
        self.add_many_calls: list[tuple[AttackTechniqueRecord, ...]] = []
        self.active_calls: list[tuple[str, str, bool]] = []

    def for_release(self, source_release: str) -> tuple[AttackTechniqueRecord, ...]:
        return tuple(row for row in self.rows if row.source_release == source_release)

    async def find(
        self, *, framework: str, technique_id: str, source_release: str
    ) -> AttackTechniqueRecord | None:
        for row in self.rows:
            if (
                row.framework == framework
                and row.technique_id == technique_id
                and row.source_release == source_release
            ):
                return row
        return None

    async def add_many(self, *, techniques: Sequence[AttackTechniqueRecord]) -> None:
        self.add_many_calls.append(tuple(techniques))
        self.rows.extend(techniques)

    async def count_for_release(self, *, framework: str, source_release: str) -> int:
        return sum(
            1
            for row in self.rows
            if row.framework == framework and row.source_release == source_release
        )

    async def list_for_release(
        self, *, framework: str, source_release: str
    ) -> tuple[AttackTechniqueRecord, ...]:
        """Rows of one release, ordered as SQL orders them: (technique_id, stix_id)."""
        return tuple(
            sorted(
                (
                    row
                    for row in self.rows
                    if row.framework == framework and row.source_release == source_release
                ),
                key=lambda row: (row.technique_id, row.source_stix_id),
            )
        )

    async def set_active_for_release(
        self, *, framework: str, source_release: str, active: bool
    ) -> int:
        """Mirror ONE release's authority onto its rows; return the rows touched."""
        self.active_calls.append((framework, source_release, active))
        updated: list[AttackTechniqueRecord] = []
        matched = 0
        for row in self.rows:
            if row.framework == framework and row.source_release == source_release:
                updated.append(replace(row, active=active))
                matched += 1
            else:
                updated.append(row)
        self.rows = updated
        return matched


class FakeAttackReleaseProjectionRepository:
    """In-memory ``attack_release_projection`` store for the staged bindings.

    It models the REAL semantics, because two of the closure's guarantees are
    statements about this table rather than about the handler:

    * ``record_many`` DEDUPES on ``(framework, source_release, technique_id)``
      and leaves an already-staged row alone. The binding names the version this
      release actually staged, so a retry that re-derived a different version
      must not replace it -- that would make re-activating an older release
      re-derive a projection instead of restoring the one it staged (brief
      section 2.6), and would silently move a pointer during staging.
    * ``missing_techniques`` is derived from the CANONICAL rows, not from the
      bindings. An empty binding table therefore reads as "every technique is
      missing" rather than as "nothing to check" -- which is exactly what makes
      a release whose staging never ran un-activatable.

    Unlike the database, this double does not model ROLLBACK (see the module's
    ``FakeUnitOfWork``): ``fail_after`` simulates a crash BETWEEN the ingest
    loop and the bindings transaction, which is a state a real crash genuinely
    leaves behind in the database. It deliberately does NOT simulate a crash in
    the middle of the bindings transaction, because the real one is a single
    transaction that either commits every binding or none.
    """

    def __init__(
        self,
        attack_techniques: FakeAttackTechniqueRepository,
        documents: FakeKnowledgeDocumentRepository,
    ) -> None:
        #: The two stores this projection derives its answers FROM: the canonical
        #: rows (for "which techniques are unbound") and the documents (for
        #: "which bound documents retrieval cannot serve"). It holds the real
        #: stores rather than copies, so a retired document reads as retired here
        #: immediately.
        self._techniques = attack_techniques
        self._documents = documents
        self.bindings: list[AttackReleaseProjectionRecord] = []
        #: Recorded so a test can assert the cutover took its per-framework lock
        #: before it validated anything (brief section 2.9).
        self.locked: list[str] = []
        #: Test switch: raise DURING the ingest loop for this release, leaving
        #: no bindings behind -- a crash before the staging transaction.
        self.fail_after: str | None = None

    # -- test-side helpers -------------------------------------------------

    def for_release(
        self, source_release: str, *, framework: str = FRAMEWORK
    ) -> tuple[AttackReleaseProjectionRecord, ...]:
        return tuple(
            sorted(
                (
                    binding
                    for binding in self.bindings
                    if binding.framework == framework
                    and binding.source_release == source_release
                ),
                key=lambda binding: binding.technique_id,
            )
        )

    def for_technique(
        self,
        source_release: str,
        technique_id: str,
        *,
        framework: str = FRAMEWORK,
    ) -> AttackReleaseProjectionRecord | None:
        for binding in self.bindings:
            if (
                binding.framework == framework
                and binding.source_release == source_release
                and binding.technique_id == technique_id
            ):
                return binding
        return None


    def inject_binding(self, binding: AttackReleaseProjectionRecord) -> None:
        """Add a binding row no ``record_many`` call would ever write.

        Test-only, and the one route to a projection that is broken in a way
        STAGING cannot repair: ``record_many`` re-derives a MISSING binding on the
        next import, but it never deletes or rewrites a row, so a stale extra row
        survives. That is what makes the cutover's preconditions observable.
        """
        self.bindings.append(binding)

    # -- AttackReleaseProjectionRepository ---------------------------------

    async def record_many(
        self, *, projections: Sequence[AttackReleaseProjectionRecord]
    ) -> None:
        if projections and projections[0].source_release == self.fail_after:
            raise RuntimeError("died mid-projection")
        for projection in projections:
            if (
                projection.framework,
                projection.source_release,
                projection.technique_id,
            ) in self._keys():
                # ``ON CONFLICT DO NOTHING``: the existing binding stands, so a
                # retry can never replace which version a release staged.
                continue
            self.bindings.append(projection)

    async def list_for_release(
        self, *, framework: str, source_release: str
    ) -> tuple[AttackReleaseProjectionRecord, ...]:
        return self.for_release(source_release, framework=framework)

    async def count_for_release(self, *, framework: str, source_release: str) -> int:
        return len(self.for_release(source_release, framework=framework))

    async def missing_techniques(
        self, *, framework: str, source_release: str
    ) -> tuple[str, ...]:
        """Driven by the CANONICAL rows: a technique the release does not have."""
        bound = {binding.technique_id for binding in self.for_release(source_release)}
        canonical = {
            row.technique_id
            for row in self._techniques.rows
            if row.framework == framework and row.source_release == source_release
        }
        return tuple(sorted(canonical - bound))

    async def unusable_documents(
        self, *, framework: str, source_release: str
    ) -> tuple[str, ...]:
        """Bound documents normal retrieval cannot serve, by their external key."""
        unusable: list[str] = []
        for binding in self.for_release(source_release, framework=framework):
            document = self._documents.documents.get(binding.document_id)
            if document is None or document.status is not DocumentStatus.ACTIVE:
                unusable.append(
                    document.external_key if document is not None else str(binding.document_id)
                )
        return tuple(sorted(unusable))

    async def lock_framework(self, *, framework: str) -> None:
        """Record the call; the serialization itself is a database fact."""
        self.locked.append(framework)

    # -- internals ---------------------------------------------------------

    def _keys(self) -> set[tuple[str, str, str]]:
        return {
            (binding.framework, binding.source_release, binding.technique_id)
            for binding in self.bindings
        }


class FakeRuntime:
    """Every store the importer touches, plus the transaction counters."""

    def __init__(self) -> None:
        self.attack_techniques = FakeAttackTechniqueRepository()
        self.attack_releases = FakeAttackReleaseRepository()
        self.documents = FakeKnowledgeDocumentRepository()
        # The projection store derives its answers from the canonical rows and
        # the documents, so it reads the SAME two stores rather than copies.
        self.attack_release_projections = FakeAttackReleaseProjectionRepository(
            self.attack_techniques, self.documents
        )
        self.chunks = FakeKnowledgeChunkRepository()
        self.profiles = FakeEmbeddingProfileRepository()
        self.events = FakeEventLedger()
        self.uows_opened = 0
        self.commits = 0

    def factory(self) -> FakeUnitOfWork:
        self.uows_opened += 1
        return FakeUnitOfWork(self)


class FakeUnitOfWork:
    """One fake transaction exposing the stores by their protocol attribute names.

    Writes land immediately: this double does not model ROLLBACK, because the
    importer's atomicity claims are about ORDERING (the bundle is parsed before
    any transaction is opened, and a rejection never opens one), and those are
    asserted from ``uows_opened`` and the stores being untouched.
    """

    def __init__(self, runtime: FakeRuntime) -> None:
        self._runtime = runtime
        self.knowledge_documents = runtime.documents
        self.knowledge_chunks = runtime.chunks
        self.embedding_profiles = runtime.profiles
        self.attack_techniques = runtime.attack_techniques
        self.attack_releases = runtime.attack_releases
        self.attack_release_projections = runtime.attack_release_projections
        self.events = runtime.events

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def commit(self) -> None:
        self._runtime.commits += 1

    async def rollback(self) -> None:
        return None

    async def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _provider() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


def _ingestion(
    runtime: FakeRuntime, *, provider: FakeEmbeddingProvider | None = None
) -> KnowledgeIngestionHandler:
    """The REAL ingestion use case, so imported documents are really written.

    Built with the ATT&CK projection capability, exactly as the container's
    ``attack_projection_ingestion_handler`` builds it: the importer stages
    through a handler that may write MITRE_ATTACK, while the ordinary factory
    used by every other caller may not.
    """
    return KnowledgeIngestionHandler(
        unit_of_work_factory=runtime.factory,
        embedding_provider=provider if provider is not None else _provider(),
        chunker=StructureAwareChunker(),
        clock=FakeClock(T0),
        allowed_source_kinds=frozenset({SourceKind.MITRE_ATTACK}),
    )


def _importer(
    *,
    runtime: FakeRuntime,
    ingestion: KnowledgeIngestionHandler | None = None,
    parser: RecordingParser | None = None,
    limits: AttackImportLimits | None = None,
) -> AttackImportHandler:
    return AttackImportHandler(
        unit_of_work_factory=runtime.factory,
        parser=parser if parser is not None else RecordingParser(),
        ingestion=ingestion if ingestion is not None else _ingestion(runtime),
        clock=FakeClock(T0),
        limits=limits,
    )


def _command(
    *,
    payload: bytes | None = None,
    release: str = RELEASE,
    framework: str = FRAMEWORK,
    activate: bool = False,
) -> ImportAttackRelease:
    return ImportAttackRelease(
        release=release,
        payload=payload if payload is not None else _full_bundle(),
        framework=framework,
        activate=activate,
    )


async def _document_for(runtime: FakeRuntime, technique_id: str) -> KnowledgeDocument:
    document = await runtime.documents.find_by_external_key(
        tenant_id=None,
        source_kind=SourceKind.MITRE_ATTACK,
        external_key=technique_external_key(_technique_of(technique_id)),
        visibility=Visibility.GLOBAL,
    )
    assert document is not None, f"no document for {technique_id}"
    return document


def _technique_of(technique_id: str) -> AttackTechnique:
    if technique_id == "T1110":
        return _brute_force_technique()
    if technique_id == "T1059":
        return _command_shell_technique()
    raise AssertionError(f"unknown fixture technique {technique_id}")


def _version_of(runtime: FakeRuntime, document: KnowledgeDocument) -> KnowledgeDocumentVersion:
    """The version RETRIEVAL serves: whatever ``active_version_id`` points at.

    There is no retrieval service in these unit tests, so this pointer IS the
    retrieval answer: a real search joins the document, keeps it only while its
    ``active_version_id`` points at the version, and reads that version's
    ``normalized_content``. Every "retrieval serves X" claim below is therefore
    asserted as ``_version_of(...).normalized_content == X``.
    """
    assert document.active_version_id is not None
    version = runtime.documents.versions[document.active_version_id]
    return version


def _staged_version_of(
    runtime: FakeRuntime, source_release: str, technique_id: str
) -> KnowledgeDocumentVersion:
    """The version a release STAGED for one technique, read from its binding.

    Staging and serving are different facts: an INACTIVE release has a binding
    (and therefore a fully projected immutable version) while retrieval still
    serves the older release's version. The binding is the only way to observe
    what a staged release will serve once it is activated.
    """
    binding = _binding_of(runtime, source_release, technique_id)
    version = runtime.documents.versions[binding.document_version_id]
    assert version.content_hash == binding.content_hash
    return version


def _binding_of(
    runtime: FakeRuntime, source_release: str, technique_id: str
) -> AttackReleaseProjectionRecord:
    binding = runtime.attack_release_projections.for_technique(
        source_release, technique_id
    )
    assert binding is not None, f"no binding for {source_release}/{technique_id}"
    return binding


def _active_version_id_of(runtime: FakeRuntime, technique_id: str) -> UUID | None:
    """The live pointer for one technique document, read synchronously.

    A pointer is a plain field of an in-memory aggregate, so reading it does not
    need a transaction -- which matters because the invariant assertions collect
    the pointers before asserting on them.
    """
    return _document_sync(runtime, technique_id).active_version_id


def _document_sync(runtime: FakeRuntime, technique_id: str) -> KnowledgeDocument:
    for document in runtime.documents.documents.values():
        if document.external_key == technique_external_key(_technique_of(technique_id)):
            return document
    raise AssertionError(f"no document for {technique_id}")


def _revised_bundle() -> bytes:
    """v14.1's bundle with T1110's upstream content REVISED; T1059 is identical."""
    return _bundle(
        _attack_pattern(
            stix_id=T1110_STIX_ID,
            technique_id="T1110",
            name="Brute Force",
            description="Revised upstream description.",
            tactics=("credential-access",),
            platforms=("Linux", "Windows"),
        ),
        _command_shell_object(),
    )


# ---------------------------------------------------------------------------
# the bundle itself: what the parser is expected to see
# ---------------------------------------------------------------------------


def test_the_hand_written_bundle_parses_to_the_expected_techniques() -> None:
    """Pin the fixture, so every other test here is anchored to real input."""
    bundle = RecordingParser().parse(_full_bundle())

    assert bundle.spec_version == "2.1"
    assert bundle.techniques == (_command_shell_technique(), _brute_force_technique())
    assert bundle.skipped_ids == EXPECTED_SKIPPED
    assert bundle.total_attack_patterns == 5


# ---------------------------------------------------------------------------
# one import: canonical rows and documents
# ---------------------------------------------------------------------------


async def test_an_import_writes_canonical_rows_for_the_pinned_release() -> None:
    runtime = FakeRuntime()
    parser = RecordingParser()
    handler = _importer(runtime=runtime, parser=parser)

    outcome = await handler.import_release(_command(activate=True))

    assert isinstance(outcome, AttackImportOutcome)
    assert outcome.release == RELEASE
    assert outcome.release_created is True
    assert outcome.release_active is True
    assert outcome.techniques_parsed == 2
    assert outcome.techniques_created == 2
    assert outcome.documents_created == 2
    assert outcome.versions_ingested == 2
    assert outcome.unchanged == 0
    assert outcome.skipped == EXPECTED_SKIPPED
    assert parser.calls == 1

    rows = runtime.attack_techniques.for_release(RELEASE)
    assert {row.technique_id for row in rows} == {"T1059", "T1110"}
    brute_force = await runtime.attack_techniques.find(
        framework=FRAMEWORK, technique_id="T1110", source_release=RELEASE
    )
    assert brute_force is not None
    assert brute_force.framework == FRAMEWORK
    assert brute_force.source_release == RELEASE
    assert brute_force.name == "Brute Force"
    assert brute_force.description == T1110_DESCRIPTION
    assert brute_force.tactics == ("credential-access",)
    assert brute_force.platforms == ("Linux", "Windows")
    assert brute_force.source_stix_id == T1110_STIX_ID
    # The row's flag MIRRORS the owning release's authority; it is not an
    # independent source of it (brief section 2.3).
    assert brute_force.active is True
    assert brute_force.created_at == T0

    release = runtime.attack_releases.get(framework=FRAMEWORK, source_release=RELEASE)
    assert release is not None
    assert release.is_active is True
    assert release.technique_count == 2
    assert release.activated_at == T0
    assert release.content_fingerprint == outcome.content_fingerprint


async def test_each_technique_becomes_a_global_mitre_attack_document() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    await handler.import_release(_command())

    for technique in (_brute_force_technique(), _command_shell_technique()):
        document = await _document_for(runtime, technique.technique_id)
        assert document.source_kind is SourceKind.MITRE_ATTACK
        assert document.visibility is Visibility.GLOBAL
        # GLOBAL means tenant-less: a MITRE technique belongs to no tenant.
        assert document.tenant_id is None
        assert document.external_key == f"mitre-attack:{technique.technique_id}"
        assert document.title == technique_document_title(technique)

        # The importance of the assertion below is that it is about a STAGED
        # release: the import did not activate, so ``active_version_id``/the live
        # pointer is still None. Nothing is serving this release yet; what exists
        # is its immutable, fully projected version, reachable through its binding.
        assert document.active_version_id is None
        # (``activate=True`` is exercised by scenario 1 below, which asserts the
        # pointer DOES move.)
        version = _staged_version_of(runtime, RELEASE, technique.technique_id)
        assert version.source_version == RELEASE
        assert version.language == "en"
        assert version.title == technique_document_title(technique)
        assert version.normalized_content == technique_document_body(technique)
        assert version.metadata == {
            "framework": FRAMEWORK,
            "technique_id": technique.technique_id,
            "stix_id": technique.stix_id,
            "tactics": list(technique.tactics),
            "platforms": list(technique.platforms),
        }


async def test_the_canonical_hash_is_the_domain_hash_of_the_document_body() -> None:
    """Regression: one hashing rule, not two.

    The canonical row and the document version describe the SAME bytes, so they
    must carry the same content hash. Computing the row's hash with an
    implementation local to the importer would let the two drift apart and
    silently re-key every technique in the corpus.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    await handler.import_release(_command())

    technique = _brute_force_technique()
    _, domain_hash = normalize_and_hash(technique_document_body(technique))
    row = await runtime.attack_techniques.find(
        framework=FRAMEWORK, technique_id=technique.technique_id, source_release=RELEASE
    )
    assert row is not None
    assert row.content_hash == domain_hash

    version = _staged_version_of(runtime, RELEASE, technique.technique_id)
    assert version.content_hash == domain_hash
    assert row.content_hash == version.content_hash


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------


async def test_reimporting_the_same_release_and_bundle_changes_nothing() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    first = await handler.import_release(_command())
    rows_after_first = runtime.attack_techniques.rows
    versions_after_first = dict(runtime.documents.versions)

    second = await handler.import_release(_command())

    assert second.techniques_parsed == first.techniques_parsed == 2
    assert second.techniques_created == 0
    assert second.documents_created == 0
    assert second.versions_ingested == 0
    assert second.unchanged == 2
    assert await runtime.attack_techniques.count_for_release(
        framework=FRAMEWORK, source_release=RELEASE
    ) == 2
    assert runtime.attack_techniques.rows == rows_after_first
    assert runtime.documents.versions == versions_after_first
    # Nothing was added a second time: exactly one write batch, at import time.
    assert len(runtime.attack_techniques.add_many_calls) == 1


async def test_reimporting_a_changed_bundle_conflicts_and_mutates_nothing() -> None:
    """A pinned release is IMMUTABLE: different content under one name is refused.

    The release name is a claim about content. Letting a second, different bundle
    be imported as "v15.1" would silently rewrite what every citation naming that
    release means, so the import fails closed -- and, because the check runs
    before the first write, it leaves the canonical rows, the release record, the
    documents, the versions, the content chunks and the embedding projections
    exactly as they were (brief sections 2.2/2.3).
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    first = await handler.import_release(_command(activate=True))

    rows_before = list(runtime.attack_techniques.rows)
    releases_before = list(runtime.attack_releases.releases)
    documents_before = dict(runtime.documents.documents)
    versions_before = dict(runtime.documents.versions)
    chunks_before = dict(runtime.chunks.chunks)
    embeddings_before = dict(runtime.chunks.embeddings)
    commits_before = runtime.commits
    add_many_before = len(runtime.attack_techniques.add_many_calls)
    chunk_batches_before = len(runtime.chunks.added_batches)

    edited = _bundle(
        _attack_pattern(
            stix_id=T1110_STIX_ID,
            technique_id="T1110",
            name="Brute Force",
            description="Revised upstream description.",
            tactics=("credential-access",),
            platforms=("Linux",),
        ),
        _command_shell_object(),
    )

    with pytest.raises(AttackReleaseContentConflictError) as excinfo:
        await handler.import_release(_command(payload=edited, activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_CONTENT_CONFLICT"
    assert "a pinned release is immutable" in str(excinfo.value)
    # Both fingerprints are named (they are hashes, not content), so an operator
    # can tell "the bundle genuinely changed" from "two files were both v15.1".
    assert first.content_fingerprint in str(excinfo.value)

    assert runtime.attack_techniques.rows == rows_before
    assert runtime.attack_releases.releases == releases_before
    assert runtime.documents.documents == documents_before
    assert runtime.documents.versions == versions_before
    assert runtime.chunks.chunks == chunks_before
    assert runtime.chunks.embeddings == embeddings_before
    assert len(runtime.attack_techniques.add_many_calls) == add_many_before
    assert len(runtime.chunks.added_batches) == chunk_batches_before
    # The refusal happened inside the first transaction and never reached ingest,
    # so nothing was committed at all -- not even a no-op.
    assert runtime.commits == commits_before


async def test_changed_content_under_a_staged_release_is_a_new_version_but_not_the_active_one() -> None:  # noqa: E501
    """A STAGED release projects its content without moving what retrieval serves.

    This test used to assert that ``_version_of(document)`` -- which reads
    ``active_version_id`` -- equalled the REVISED content, i.e. that importing a
    release with the default ``activate=False`` had ALREADY changed the live
    retrieval answer. That is the defect this closure exists to close: v15.1 was
    neither authoritative nor activated, yet retrieval served its content.

    Staging and serving are now separate facts, so this asserts both halves: the
    new version IS created and IS bound to v15.1, while the live pointer still
    points at v14.1's version -- retrieval keeps serving v14.1's content until a
    cutover makes v15.1 authoritative (brief sections 2.4-2.7).
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    rows_before = runtime.attack_techniques.for_release("v14.1")
    document = await _document_for(runtime, "T1110")
    original_version_id = document.active_version_id
    assert original_version_id is not None
    assert "Revised upstream description." not in _version_of(
        runtime, document
    ).normalized_content

    outcome = await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle())
    )

    assert outcome.techniques_created == 2
    assert outcome.versions_ingested == 1
    assert outcome.unchanged == 1

    # The new version exists, is fully projected, and v15.1's binding names it.
    staged = _staged_version_of(runtime, "v15.1", "T1110")
    assert staged.version == 2
    assert "Revised upstream description." in staged.normalized_content
    assert staged.id != original_version_id

    # ...and retrieval does NOT serve it. ``active_version_id`` IS the retrieval
    # answer here (there is no retrieval service in these unit tests): the live
    # pointer is untouched, so v14.1's content is still what a search would read.
    assert document.active_version_id == original_version_id
    assert _version_of(runtime, document).normalized_content == technique_document_body(
        _brute_force_technique()
    )
    staged_release = runtime.attack_releases.get(
        framework=FRAMEWORK, source_release="v15.1"
    )
    assert staged_release is not None
    assert staged_release.is_active is False
    # The older release is untouched: rows are never rewritten by a later import.
    assert runtime.attack_techniques.for_release("v14.1") == rows_before


# ---------------------------------------------------------------------------
# activate: which release is authoritative (section 38)
# ---------------------------------------------------------------------------


async def test_activate_flips_the_other_releases_rows_inactive_without_deleting_them() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    await handler.import_release(_command(release="v14.1", activate=True))
    await handler.import_release(_command(release="v15.1", activate=True))

    previous = runtime.attack_techniques.for_release("v14.1")
    current = runtime.attack_techniques.for_release("v15.1")
    assert len(previous) == 2
    assert len(current) == 2
    assert all(row.active is False for row in previous)
    assert all(row.active is True for row in current)
    # Authority is a fact about the RELEASE, and switching it is one operation.
    assert [
        release.source_release for release in runtime.attack_releases.active_for(FRAMEWORK)
    ] == ["v15.1"]
    switched_off = runtime.attack_releases.get(
        framework=FRAMEWORK, source_release="v14.1"
    )
    assert switched_off is not None
    assert switched_off.is_active is False
    assert runtime.attack_releases.activations == [
        (FRAMEWORK, "v14.1"),
        (FRAMEWORK, "v15.1"),
    ]
    # Flagged, never deleted: the older release is still readable by its own
    # source_release, which is what makes the switch reversible.
    assert await runtime.attack_techniques.count_for_release(
        framework=FRAMEWORK, source_release="v14.1"
    ) == 2
    # The row flag is re-asserted from the release on every import rather than
    # left to whatever happened to be written, so the two cannot disagree.
    assert runtime.attack_techniques.active_calls == [
        (FRAMEWORK, "v14.1", True),
        (FRAMEWORK, "v14.1", False),
        (FRAMEWORK, "v15.1", True),
    ]


async def test_a_new_release_reuses_the_existing_documents() -> None:
    """Technique documents are release-independent; only the rows carry it.

    The body does not mention the release, so the same technique at a new
    release hashes identically and ingestion converges instead of writing a
    duplicate version. Re-importing a release is therefore cheap.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1"))

    outcome = await handler.import_release(_command(release="v15.1"))

    assert outcome.techniques_created == 2
    assert outcome.documents_created == 0
    assert outcome.versions_ingested == 0
    assert outcome.unchanged == 2
    assert len(runtime.documents.documents) == 2
    assert len(runtime.documents.versions) == 2


async def test_importing_without_activate_leaves_the_authoritative_release_alone() -> None:
    """The backfill path can never produce a SECOND authoritative release.

    ``activate=False`` writes a release whose rows are all INACTIVE: importing a
    new release is not the same act as promoting it, so an operator can stage
    v15.1 and switch later without a window in which two releases are
    authoritative (brief sections 2.1/10).
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))

    outcome = await handler.import_release(_command(release="v15.1"))

    assert outcome.techniques_created == 2
    assert outcome.release_created is True
    assert outcome.release_active is False

    authoritative = runtime.attack_releases.active_for(FRAMEWORK)
    assert [release.source_release for release in authoritative] == ["v14.1"]
    staged = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v15.1")
    assert staged is not None
    assert staged.is_active is False
    assert staged.activated_at is None

    assert all(row.active for row in runtime.attack_techniques.for_release("v14.1"))
    assert all(not row.active for row in runtime.attack_techniques.for_release("v15.1"))
    # Authority is now written ONLY by the cutover, so v15.1 -- which never
    # reached one -- contributes no mirror call at all: its rows were written
    # ``active=False`` by ``add_many`` in the first place, and the registration
    # half deliberately no longer writes the mirror (brief section 2.3).
    assert runtime.attack_techniques.active_calls == [(FRAMEWORK, "v14.1", True)]
    assert runtime.attack_releases.activations == [(FRAMEWORK, "v14.1")]

    # The assertion this test conspicuously lacked: an INACTIVE release must not
    # change what RETRIEVAL serves. T1059 is byte-identical under both releases,
    # so its version is REUSED -- which is precisely why the live pointer is the
    # thing that has to be asserted, and why the old test could not see the bug.
    live = _active_version_id_of(runtime, "T1110")
    assert _version_of(runtime, await _document_for(runtime, "T1110")).normalized_content == (
        technique_document_body(_brute_force_technique())
    )
    # The staged release DOES have a complete projection: it just is not live.
    # The binding names the very version that is serving, because the content is
    # identical -- so the pointer being unchanged is the ONLY observable
    # difference between "staged" and "authoritative".
    assert _binding_of(runtime, "v15.1", "T1110").document_version_id == live

    # A re-run of the same backfill import converges: already registered, already
    # projected, so no version is written and retrieval is still untouched.
    versions_before = dict(runtime.documents.versions)
    rerun = await handler.import_release(_command(release="v15.1"))
    assert rerun.release_created is False
    assert rerun.versions_ingested == 0
    assert rerun.unchanged == 2
    assert runtime.documents.versions == versions_before
    assert runtime.attack_releases.activations == [(FRAMEWORK, "v14.1")]


async def test_activating_an_already_active_release_does_not_restamp_it() -> None:
    """When a release became authoritative must not drift on an idempotent re-run."""
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(activate=True))
    before = runtime.attack_releases.get(framework=FRAMEWORK, source_release=RELEASE)
    assert before is not None
    assert before.activated_at == T0

    later = FakeClock(datetime(2026, 9, 13, 10, 0, 0, tzinfo=UTC))
    second = AttackImportHandler(
        unit_of_work_factory=runtime.factory,
        parser=RecordingParser(),
        ingestion=_ingestion(runtime),
        clock=later,
    )
    outcome = await second.import_release(_command(activate=True))

    assert outcome.release_created is False
    assert outcome.release_active is True
    after = runtime.attack_releases.get(framework=FRAMEWORK, source_release=RELEASE)
    assert after == before
    assert runtime.attack_releases.activations == [(FRAMEWORK, RELEASE)]


async def test_a_semantically_identical_but_reordered_bundle_is_the_same_release() -> None:
    """The fingerprint is over canonical CONTENT, never over the file's bytes.

    MITRE's own exports are not byte-stable (object order and JSON key order move
    between builds), so a release that hashed its input bytes could not be
    re-imported at all. Here every object is reversed and every JSON key sorted,
    and the import must still converge (brief sections 2.2/10).
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    first = await handler.import_release(_command(activate=True))

    reordered = _reordered_bundle()
    assert reordered != _full_bundle()

    second = await handler.import_release(_command(payload=reordered, activate=True))

    assert second.content_fingerprint == first.content_fingerprint
    assert second.techniques_created == 0
    assert second.versions_ingested == 0
    assert second.unchanged == 2
    release = runtime.attack_releases.get(framework=FRAMEWORK, source_release=RELEASE)
    assert release is not None
    assert release.content_fingerprint == first.content_fingerprint
    assert len(runtime.attack_releases.releases) == 1
    assert len(runtime.attack_techniques.add_many_calls) == 1


async def test_an_adopted_release_is_verified_against_stored_rows_and_then_pinned() -> None:
    """A release adopted by the migration has no fingerprint until one is pinned.

    Pinning must record a fact that was VERIFIED, so the handler re-derives the
    fingerprint from the rows the database actually stores through the same
    canonical function -- the incoming bundle is never trusted to describe what
    is already there (brief section 2.2).
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))

    # What the migration leaves behind: canonical rows, an authoritative release,
    # and NO fingerprint, because the rows predate fingerprinting.
    runtime.attack_releases._replace(
        framework=FRAMEWORK, source_release="v14.1", content_fingerprint=None
    )
    assert runtime.attack_releases.get(
        framework=FRAMEWORK, source_release="v14.1"
    ).content_fingerprint is None  # type: ignore[union-attr]

    outcome = await handler.import_release(_command(release="v14.1"))

    assert runtime.attack_releases.pinned == [
        (FRAMEWORK, "v14.1", outcome.content_fingerprint)
    ]
    pinned = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v14.1")
    assert pinned is not None
    assert pinned.content_fingerprint == outcome.content_fingerprint
    # A fingerprint derived from STORED rows, not from the bundle's own bytes.
    assert outcome.content_fingerprint == fingerprint_from_records(
        framework=FRAMEWORK,
        source_release="v14.1",
        techniques=runtime.attack_techniques.for_release("v14.1"),
    )


async def test_an_adopted_release_whose_stored_rows_disagree_is_refused() -> None:
    """Adoption is a verification, not a rubber stamp."""
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    runtime.attack_releases._replace(
        framework=FRAMEWORK, source_release="v14.1", content_fingerprint=None
    )
    rows_before = list(runtime.attack_techniques.rows)

    edited = _bundle(
        _attack_pattern(
            stix_id=T1110_STIX_ID,
            technique_id="T1110",
            name="Brute Force",
            description="Revised upstream description.",
            tactics=("credential-access",),
            platforms=("Linux", "Windows"),
        ),
        _command_shell_object(),
    )

    with pytest.raises(AttackReleaseContentConflictError) as excinfo:
        await handler.import_release(_command(release="v14.1", payload=edited))

    assert excinfo.value.code == "ATTACK_RELEASE_CONTENT_CONFLICT"
    assert runtime.attack_releases.pinned == []
    assert runtime.attack_techniques.rows == rows_before


async def test_a_crash_between_the_canonical_rows_and_the_documents_converges_on_retry() -> None:
    """The canonical half commits FIRST, so a crash leaves a repairable release.

    A release whose documents are only partly projected must be fixable by
    re-running the same release: the canonical rows are not duplicated, no
    content conflict is raised (the stored rows ARE this bundle), and the
    documents complete. The reverse write order would leave documents belonging
    to no canonical release, which no retry could repair (brief section 2.4).
    """
    runtime = FakeRuntime()
    crashing = FakeEmbeddingProvider(fail_on_call=2)
    handler = _importer(runtime=runtime, ingestion=_ingestion(runtime, provider=crashing))

    with pytest.raises(RuntimeError, match="died mid-projection"):
        await handler.import_release(_command(activate=True))

    # The canonical half IS committed -- and the release is NOT authoritative,
    # because the cutover never ran. That is the SAFE state: a release whose
    # projection is partial must not be claiming authority over retrieval.
    assert (
        await runtime.attack_techniques.count_for_release(
            framework=FRAMEWORK, source_release=RELEASE
        )
        == 2
    )
    release = runtime.attack_releases.get(framework=FRAMEWORK, source_release=RELEASE)
    assert release is not None
    assert release.is_active is False
    assert release.activated_at is None
    assert release.content_fingerprint is not None
    # ...and the projection is genuinely partial.
    assert len(runtime.documents.documents) == 1
    assert len(runtime.attack_techniques.add_many_calls) == 1

    outcome = await _importer(runtime=runtime).import_release(_command(activate=True))

    assert outcome.techniques_created == 0
    assert outcome.documents_created == 1
    # One document was already staged AND committed before the crash, so the
    # retry converges on it; only T1110 contributes a new version.
    assert outcome.versions_ingested == 1
    assert outcome.unchanged == 1
    assert outcome.release_active is True
    assert len(runtime.documents.documents) == 2
    converged_release = runtime.attack_releases.get(
        framework=FRAMEWORK, source_release=RELEASE
    )
    assert converged_release is not None
    assert converged_release.is_active is True
    assert (
        await runtime.attack_techniques.count_for_release(
            framework=FRAMEWORK, source_release=RELEASE
        )
        == 2
    )
    # No duplicate canonical write, no second release, and no false conflict.
    assert len(runtime.attack_techniques.add_many_calls) == 1
    assert len(runtime.attack_releases.releases) == 1
    assert runtime.attack_releases.pinned == []


# ---------------------------------------------------------------------------
# release authority AND the retrieval projection, together (closure-2)
#
# Authority and "what retrieval serves" are two facts, and every test here
# asserts BOTH. Asserting only the release row is what let an inactive release
# move the live pointer unnoticed; asserting only the pointer cannot tell which
# release is authoritative.
# ---------------------------------------------------------------------------


def _authority_state(runtime: FakeRuntime) -> tuple[str, ...]:
    """Every ACTIVE release of the framework, as a tuple of release names."""
    return tuple(
        release.source_release for release in runtime.attack_releases.active_for(FRAMEWORK)
    )


async def test_scenario_1_an_activated_release_is_authoritative_and_serves_its_own_content() -> None:  # noqa: E501
    """Scenario 1: v14.1 imported with ``activate=True``.

    Both halves move together: v14.1 is the framework's ACTIVE release AND the
    live pointer resolves to the version v14.1 staged.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    outcome = await handler.import_release(_command(release="v14.1", activate=True))

    assert outcome.release_active is True
    assert _authority_state(runtime) == ("v14.1",)
    release = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v14.1")
    assert release is not None
    assert release.is_active is True
    assert release.activated_at == T0
    binding = _binding_of(runtime, "v14.1", "T1110")
    document = await _document_for(runtime, "T1110")
    assert document.active_version_id == binding.document_version_id
    assert _version_of(runtime, document).normalized_content == technique_document_body(
        _brute_force_technique()
    )


async def test_scenario_2_staging_a_changed_release_moves_neither_half() -> None:
    """Scenario 2: v15.1 staged with changed content while v14.1 stays live.

    v15.1's canonical rows are written, its new document version is created and
    bound -- and NOTHING retrieval can observe changes.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    live_before = _active_version_id_of(runtime, "T1110")

    outcome = await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle())
    )

    # Half one: canonical authority -- v14.1 stays ACTIVE, v15.1 is INACTIVE.
    assert outcome.release_active is False
    assert _authority_state(runtime) == ("v14.1",)
    staged = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v15.1")
    assert staged is not None
    assert staged.is_active is False
    assert staged.activated_at is None
    # Half two: retrieval -- the live pointer is byte-identical to before.
    assert _active_version_id_of(runtime, "T1110") == live_before
    live_document = await _document_for(runtime, "T1110")
    assert _version_of(runtime, live_document).normalized_content == technique_document_body(
        _brute_force_technique()
    )
    # The binding exists and names the NEW version -- which is simply not what
    # retrieval is serving yet.
    binding = _binding_of(runtime, "v15.1", "T1110")
    assert binding.document_version_id != live_before
    assert "Revised upstream description." in _staged_version_of(
        runtime, "v15.1", "T1110"
    ).normalized_content


async def test_scenario_3_activating_the_staged_release_moves_both_halves() -> None:
    """Scenario 3: activating v15.1 flips authority AND the affected pointers.

    This is the cutover. Every document the release bound now serves the version
    v15.1 staged, in the same transaction as the authority flip.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    await handler.import_release(_command(release="v15.1", payload=_revised_bundle()))

    activated = await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle(), activate=True)
    )

    assert activated.release_active is True
    assert _authority_state(runtime) == ("v15.1",)
    previous = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v14.1")
    assert previous is not None
    assert previous.is_active is False
    # Retrieval now serves v15.1's projection, for the CHANGED technique...
    assert (
        _active_version_id_of(runtime, "T1110")
        == _binding_of(runtime, "v15.1", "T1110").document_version_id
    )
    changed = await _document_for(runtime, "T1110")
    assert "Revised upstream description." in _version_of(runtime, changed).normalized_content
    # ...and for the UNCHANGED one, whose version is reused.
    assert (
        _active_version_id_of(runtime, "T1059")
        == _binding_of(runtime, "v15.1", "T1059").document_version_id
    )
    # The cutover takes the per-framework lock before validating anything. This
    # scenario drives two of them (the import and the re-run), so the assertion
    # is on the property rather than on a total the setup would have to mirror.
    assert runtime.attack_release_projections.locked
    assert set(runtime.attack_release_projections.locked) == {FRAMEWORK}


async def test_scenario_4_reactivating_the_older_release_restores_its_own_version() -> None:
    """Scenario 4: re-activating v14.1 restores the version v14.1 STAGED.

    This is why the binding table exists. v14.1's T1110 version is now a
    HISTORICAL version (v15.1 staged a newer one), and re-deriving "what v14.1
    should serve" from content hashes could not distinguish the two. The binding
    names the exact version, so the pointer is restored to that exact id --
    asserted as an id, not merely as content.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    t1110_version = _binding_of(runtime, "v14.1", "T1110").document_version_id
    t1059_version = _binding_of(runtime, "v14.1", "T1059").document_version_id

    await handler.import_release(_command(release="v15.1", payload=_revised_bundle()))
    await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle(), activate=True)
    )
    assert _active_version_id_of(runtime, "T1110") != t1110_version

    restored = await handler.import_release(_command(release="v14.1", activate=True))

    assert restored.release_active is True
    assert _authority_state(runtime) == ("v14.1",)
    replaced = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v15.1")
    assert replaced is not None
    assert replaced.is_active is False
    assert _active_version_id_of(runtime, "T1110") == t1110_version
    assert _active_version_id_of(runtime, "T1059") == t1059_version
    document = await _document_for(runtime, "T1110")
    assert _version_of(runtime, document).id == t1110_version
    assert _version_of(runtime, document).version == 1
    assert "Revised upstream description." not in _version_of(
        runtime, document
    ).normalized_content


async def test_scenario_5_identical_content_is_reused_but_every_release_keeps_its_binding() -> None:
    """Scenario 5: two releases carrying byte-identical content.

    The IMMUTABLE version is shared -- reuse is correct, the content is the same
    -- but the CLAIM "this release projected this version" is per release. So
    there are two binding rows naming one version, and after activating v15.1 the
    binding that names the live pointer is v15.1's. That provenance fact cannot
    live on the version's own ``source_version``, which records only which
    release happened to CREATE the row.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    version_id = _binding_of(runtime, "v14.1", "T1110").document_version_id

    outcome = await handler.import_release(_command(release="v15.1"))

    assert outcome.versions_ingested == 0
    assert outcome.unchanged == 2
    assert _active_version_id_of(runtime, "T1110") == version_id
    v141 = _binding_of(runtime, "v14.1", "T1110")
    v151 = _binding_of(runtime, "v15.1", "T1110")
    assert v141.document_version_id == v151.document_version_id == version_id
    assert (v141.framework, v141.source_release) == (FRAMEWORK, "v14.1")
    assert (v151.framework, v151.source_release) == (FRAMEWORK, "v15.1")
    assert v141.id != v151.id
    # ONE immutable version row for T1110 (T1059 reuses its own too), TWO binding
    # rows per release naming it.
    assert len(runtime.attack_release_projections.for_release("v14.1")) == 2
    assert len(runtime.attack_release_projections.for_release("v15.1")) == 2
    assert (
        sum(
            1
            for version in runtime.documents.versions.values()
            if version.normalized_content == technique_document_body(_brute_force_technique())
        )
        == 1
    )

    await handler.import_release(_command(release="v15.1", activate=True))

    assert _authority_state(runtime) == ("v15.1",)
    live = _active_version_id_of(runtime, "T1110")
    assert live == version_id
    assert _binding_of(runtime, "v15.1", "T1110").document_version_id == live


async def test_scenario_6_a_crash_mid_staging_leaves_the_live_release_untouched_then_converges() -> None:  # noqa: E501
    """Scenario 6: the projection transaction never happened.

    ``fail_after`` halts the staging before the bindings are recorded, which is
    the state a crash genuinely leaves behind (the bindings transaction commits
    all of them or none). Nothing about v14.1 moves: it is still the ACTIVE
    release and still serving its own version. Re-running the import converges to
    v15.1 ACTIVE with a complete projection.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    live = _active_version_id_of(runtime, "T1110")
    previous_release = runtime.attack_releases.get(
        framework=FRAMEWORK, source_release="v14.1"
    )
    runtime.attack_release_projections.fail_after = "v15.1"

    with pytest.raises(RuntimeError, match="died mid-projection"):
        await handler.import_release(_command(release="v15.1", payload=_revised_bundle()))

    # The bindings transaction is atomic: the crash left NONE of v15.1's.
    assert runtime.attack_release_projections.for_release("v15.1") == ()
    assert len(runtime.attack_release_projections.for_release("v14.1")) == 2
    # The release RECORD is committed before staging begins, so v15.1 exists;
    # what the crash prevented is its bindings AND its authority. v14.1 is
    # exactly the release it was.
    assert runtime.attack_releases.get(
        framework=FRAMEWORK, source_release="v14.1"
    ) == previous_release
    assert _authority_state(runtime) == ("v14.1",)
    incomplete = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v15.1")
    assert incomplete is not None
    assert incomplete.is_active is False
    assert _active_version_id_of(runtime, "T1110") == live
    untouched = await _document_for(runtime, "T1110")
    # The live version is still exactly v14.1's, with exactly v14.1's content:
    # the staging wrote nothing retrieval can observe and moved no pointer.
    assert _version_of(runtime, untouched).content_hash == _binding_of(
        runtime, "v14.1", "T1110"
    ).content_hash
    assert "Revised upstream description." not in _version_of(
        runtime, untouched
    ).normalized_content
    # Staging ran before the crash, so the immutable version rows it wrote are
    # committed: the crash lost only the bindings transaction and the cutover.
    assert len(runtime.documents.versions) >= len(runtime.documents.documents)

    runtime.attack_release_projections.fail_after = None
    converged = await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle(), activate=True)
    )

    assert converged.release_active is True
    assert _authority_state(runtime) == ("v15.1",)
    assert (
        await runtime.attack_release_projections.count_for_release(
            framework=FRAMEWORK, source_release="v15.1"
        )
        == 2
    )
    assert (
        await runtime.attack_release_projections.missing_techniques(
            framework=FRAMEWORK, source_release="v15.1"
        )
        == ()
    )
    assert (
        _active_version_id_of(runtime, "T1110")
        == _binding_of(runtime, "v15.1", "T1110").document_version_id
    )
    recovered = await _document_for(runtime, "T1110")
    assert "Revised upstream description." in _version_of(runtime, recovered).normalized_content


async def test_scenario_7_a_failure_inside_the_cutover_leaves_both_halves_unchanged() -> None:
    """Scenario 7: validation fails, so nothing is mutated at all.

    v15.1's projection is not complete, so the release cannot be made
    authoritative. The refusal is raised by the cutover's PRE-MUTATION
    precondition, so the release authority, the mirror and every live pointer are
    exactly as they were.

    The database half of this guarantee is the rollback at ``__aexit__``; this
    double writes immediately and does NOT model rollback, so what is asserted
    here is the ordering half -- nothing at all is written before the refusal.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    live = _binding_of(runtime, "v14.1", "T1110").document_version_id
    live_t1059 = _binding_of(runtime, "v14.1", "T1059").document_version_id
    await handler.import_release(_command(release="v15.1"))
    assert _active_version_id_of(runtime, "T1110") == live

    # Break v15.1's projection so that it is genuinely INCOMPLETE -- an extra row
    # bound to a technique the release does not have, which staging can never
    # repair because ``record_many`` only ever appends missing rows. The cutover
    # checks this FIRST, before any mutation, which is what makes "nothing was
    # written" observable in a double that does not model ROLLBACK.
    solid = _binding_of(runtime, "v15.1", "T1110")
    runtime.attack_release_projections.inject_binding(
        replace(solid, technique_id="T9999")
    )
    # Every canonical technique IS bound, so ``missing_techniques`` is empty: the
    # defect is the staged COUNT disagreeing with the release's own declaration.
    assert await runtime.attack_release_projections.missing_techniques(
        framework=FRAMEWORK, source_release="v15.1"
    ) == ()
    assert (
        await runtime.attack_release_projections.count_for_release(
            framework=FRAMEWORK, source_release="v15.1"
        )
        == 3
    )

    releases_before = list(runtime.attack_releases.releases)
    rows_before = list(runtime.attack_techniques.rows)
    bindings_before = list(runtime.attack_release_projections.bindings)
    add_many_before = len(runtime.attack_techniques.add_many_calls)
    uows_before = runtime.uows_opened
    active_calls_before = list(runtime.attack_techniques.active_calls)
    activations_before = list(runtime.attack_releases.activations)

    with pytest.raises(AttackReleaseProjectionIncompleteError) as excinfo:
        await handler.import_release(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INCOMPLETE"
    assert "3 of 2 canonical techniques are staged" in str(excinfo.value)
    # Authority: untouched. No release changed status, no mirror row was rewritten
    # and no activation was recorded -- the refusal happened before any of them.
    assert runtime.attack_releases.releases == releases_before
    assert _authority_state(runtime) == ("v14.1",)
    assert runtime.attack_releases.activations == activations_before
    assert runtime.attack_techniques.rows == rows_before
    assert len(runtime.attack_techniques.add_many_calls) == add_many_before
    assert runtime.attack_techniques.active_calls == active_calls_before
    # Retrieval: untouched -- both live pointers still name v14.1's versions and
    # the projection is byte-for-byte the one that was there before the attempt.
    assert _active_version_id_of(runtime, "T1110") == live
    assert _active_version_id_of(runtime, "T1059") == live_t1059
    assert runtime.attack_release_projections.bindings == bindings_before
    # The cutover transaction WAS opened -- the validation ran inside it, and only
    # then was the refusal raised -- and it committed nothing.
    assert runtime.uows_opened > uows_before


async def test_scenario_8_every_pointer_follows_whichever_release_ends_up_authoritative() -> None:
    """Scenario 8: two activations, exactly one winner, no mixed state.

    ``lock_framework`` is a no-op here, so this does not MODEL database
    concurrency -- it asserts the INVARIANT the lock exists to preserve, on the
    final state of two back-to-back cutovers: exactly one ACTIVE release per
    framework, and EVERY live pointer of the framework matching the winner's own
    binding rather than a leftover of the loser's.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))
    await handler.import_release(_command(release="v15.1", payload=_revised_bundle()))

    await handler.import_release(_command(release="v14.1", activate=True))
    await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle(), activate=True)
    )

    # Exactly one ACTIVE release, and the loser stays readable by its own name.
    active = runtime.attack_releases.active_for(FRAMEWORK)
    assert len(active) == 1
    assert active[0].source_release == "v15.1"
    assert active[0].is_active is True
    loser = runtime.attack_releases.get(framework=FRAMEWORK, source_release="v14.1")
    assert loser is not None
    assert loser.is_active is False

    # Every live pointer belongs to the WINNER's binding: no mixed state.
    winner = _authority_state(runtime)[0]
    assert winner == "v15.1"
    cutovers = 3  # every import above that carried authority intent
    for technique_id in ("T1059", "T1110"):
        assert (
            _active_version_id_of(runtime, technique_id)
            == _binding_of(runtime, winner, technique_id).document_version_id
        )
    # ...and the two releases genuinely staged different versions for the
    # technique whose content changed, so this is not a vacuous equality.
    assert (
        _binding_of(runtime, "v15.1", "T1110").document_version_id
        != _binding_of(runtime, "v14.1", "T1110").document_version_id
    )

    # The mirror agrees with the release for every canonical row of the framework.
    for row in runtime.attack_techniques.rows:
        assert row.active is (row.source_release == winner)
    # Every cutover took the framework's lock. The double does not serialize them,
    # and it does not have to -- the invariant above is what the lock protects.
    assert runtime.attack_release_projections.locked == [FRAMEWORK] * cutovers


async def test_scenario_9_the_same_release_with_different_content_conflicts_and_mutates_nothing() -> None:  # noqa: E501
    """Scenario 9: the pinned-release immutability rule, unchanged by the closure.

    Re-importing a release NAME with different content is refused with
    ``ATTACK_RELEASE_CONTENT_CONFLICT`` -- and because the check runs before the
    first write, the canonical rows, the knowledge documents, the bindings and
    the release authority are all exactly as they were.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(
        _command(release="v15.1", payload=_revised_bundle(), activate=True)
    )

    rows_before = list(runtime.attack_techniques.rows)
    releases_before = list(runtime.attack_releases.releases)
    documents_before = dict(runtime.documents.documents)
    versions_before = dict(runtime.documents.versions)
    bindings_before = list(runtime.attack_release_projections.bindings)
    live = _active_version_id_of(runtime, "T1110")
    commits_before = runtime.commits

    with pytest.raises(AttackReleaseContentConflictError) as excinfo:
        await handler.import_release(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_CONTENT_CONFLICT"
    assert runtime.attack_techniques.rows == rows_before
    assert runtime.attack_releases.releases == releases_before
    assert runtime.documents.documents == documents_before
    assert runtime.documents.versions == versions_before
    assert runtime.attack_release_projections.bindings == bindings_before
    assert _active_version_id_of(runtime, "T1110") == live
    assert runtime.commits == commits_before



# ---------------------------------------------------------------------------
# skipped objects are reported, never silently dropped
# ---------------------------------------------------------------------------


async def test_revoked_deprecated_and_out_of_scope_techniques_are_skipped_and_reported() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    outcome = await handler.import_release(_command())

    # Reported: sorted, deduped, and naming the ATT&CK id rather than the STIX id.
    assert outcome.skipped == EXPECTED_SKIPPED
    assert len(set(outcome.skipped)) == len(outcome.skipped) == 3
    assert T1430_STIX_ID not in outcome.skipped
    # The rest imported: skipping is per-object, not all-or-nothing.
    assert outcome.techniques_parsed == 2
    assert {row.technique_id for row in runtime.attack_techniques.rows} == {"T1059", "T1110"}
    assert len(runtime.documents.documents) == 2


async def test_a_bundle_whose_techniques_are_all_skipped_imports_nothing() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    outcome = await handler.import_release(
        _command(payload=_bundle(_revoked_object(), _deprecated_object(), _mobile_object()))
    )

    assert outcome.techniques_parsed == 0
    assert outcome.skipped == EXPECTED_SKIPPED
    assert runtime.attack_techniques.rows == []
    assert runtime.documents.documents == {}


async def test_an_empty_bundle_is_imported_as_an_empty_release() -> None:
    """An empty release is legal here: the parser does not decide policy."""
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    outcome = await handler.import_release(_command(payload=_bundle()))

    assert outcome.techniques_parsed == 0
    assert outcome.techniques_created == 0
    assert outcome.skipped == ()
    assert runtime.attack_techniques.rows == []
    assert runtime.uows_opened == 1


# ---------------------------------------------------------------------------
# rejections: bad input never reaches the stores
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("release", ["", "   ", "\n\t"])
async def test_an_empty_or_whitespace_release_is_rejected(release: str) -> None:
    runtime = FakeRuntime()
    parser = RecordingParser()
    handler = _importer(runtime=runtime, parser=parser)

    with pytest.raises(ApplicationError) as excinfo:
        await handler.import_release(_command(release=release))

    assert "pinned release name" in str(excinfo.value)
    assert parser.calls == 0
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []


async def test_an_unsupported_framework_is_rejected_before_the_bundle_is_read() -> None:
    runtime = FakeRuntime()
    parser = RecordingParser()
    handler = _importer(runtime=runtime, parser=parser)

    with pytest.raises(ApplicationError) as excinfo:
        await handler.import_release(_command(framework="mitre-car"))

    assert "unsupported ATT&CK framework" in str(excinfo.value)
    assert parser.calls == 0
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []
    assert runtime.documents.documents == {}


async def test_an_oversized_bundle_is_rejected_and_never_parsed() -> None:
    runtime = FakeRuntime()
    parser = RecordingParser()
    handler = _importer(
        runtime=runtime, parser=parser, limits=AttackImportLimits(max_bundle_bytes=16)
    )
    payload = _full_bundle()
    assert len(payload) > 16

    with pytest.raises(ApplicationError) as excinfo:
        await handler.import_release(_command(payload=payload))

    assert "refusing to parse it" in str(excinfo.value)
    # Refusing to parse is the guarantee: the parser was never invoked at all.
    assert parser.payloads == []
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []
    assert runtime.documents.documents == {}


def test_the_default_bundle_bound_is_the_documented_one() -> None:
    assert AttackImportLimits().max_bundle_bytes == DEFAULT_MAX_BUNDLE_BYTES


async def test_a_malformed_bundle_is_rejected_before_any_row_is_written() -> None:
    runtime = FakeRuntime()
    parser = RecordingParser()
    handler = _importer(runtime=runtime, parser=parser)

    with pytest.raises(InvalidMitreBundleError) as excinfo:
        await handler.import_release(_command(payload=b'{"type": "bundle",'))

    assert "not valid JSON" in str(excinfo.value)
    assert parser.calls == 1
    # Atomicity: the parse happens BEFORE the first transaction is opened, so a
    # bad bundle cannot leave a half-imported release behind.
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []
    assert runtime.documents.documents == {}
    assert runtime.chunks.chunks == {}


async def test_a_payload_that_is_not_a_bundle_is_rejected() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    payload = json.dumps({"type": "report", "objects": []}).encode("utf-8")

    with pytest.raises(InvalidMitreBundleError) as excinfo:
        await handler.import_release(_command(payload=payload))

    assert "must be 'bundle'" in str(excinfo.value)
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []


async def test_a_non_2x_spec_version_is_rejected() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    with pytest.raises(InvalidMitreBundleError) as excinfo:
        await handler.import_release(
            _command(payload=_bundle(_brute_force_object(), spec_version="1.2"))
        )

    assert "spec_version" in str(excinfo.value)
    assert runtime.uows_opened == 0
    assert runtime.attack_techniques.rows == []


async def test_no_tenant_scoped_document_is_ever_created_by_an_import() -> None:
    """Every document an import creates is GLOBAL and tenant-less.

    MITRE techniques are shared reference material, so a TENANT-scoped one would
    be invisible to every other tenant -- and would also mean the importer had
    invented a tenant id the operator never supplied.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)

    await handler.import_release(_command(release="v14.1"))
    await handler.import_release(_command(release="v15.1", activate=True))

    assert runtime.documents.documents
    for document in runtime.documents.documents.values():
        assert document.source_kind is SourceKind.MITRE_ATTACK
        assert document.visibility is Visibility.GLOBAL
        assert document.tenant_id is None
        assert document.external_key.startswith("mitre-attack:")

# ---------------------------------------------------------------------------
# invalid bindings: the row exists, the relationship does not (closure-3)
# ---------------------------------------------------------------------------

async def _staged_v15(runtime: FakeRuntime, handler: AttackImportHandler) -> None:
    """v14.1 ACTIVE and serving, v15.1 staged -- the shared setup for §28."""
    await handler.import_release(_command(release="v14.1", activate=True))
    await handler.import_release(_command(release="v15.1"))


def _zero_mutation_snapshot(runtime: FakeRuntime) -> object:
    """Capture everything the cutover must leave untouched on refusal."""
    return (
        list(runtime.attack_releases.releases),
        list(runtime.attack_techniques.rows),
        list(runtime.attack_release_projections.bindings),
        list(runtime.attack_releases.activations),
        list(runtime.attack_techniques.active_calls),
        _authority_state(runtime),
    )


def _assert_zero_mutation(runtime: FakeRuntime, before: object) -> None:
    (
        releases,
        rows,
        bindings,
        activations,
        active_calls,
        authority,
    ) = before  # type: ignore[misc]
    assert runtime.attack_releases.releases == releases
    assert runtime.attack_techniques.rows == rows
    assert runtime.attack_release_projections.bindings == bindings
    assert runtime.attack_releases.activations == activations
    assert runtime.attack_techniques.active_calls == active_calls
    assert _authority_state(runtime) == authority


async def test_a_binding_pointing_at_another_documents_version_is_refused() -> None:
    """``version.document_id != binding.document_id`` is not a projection.

    T1110's binding is rewritten to name T1059's version. The cutover must fail
    with ATTACK_RELEASE_PROJECTION_INVALID_BINDING -- not MISSING_VERSION,
    because both rows exist -- and leave authority and every pointer untouched.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)
    live = _active_version_id_of(runtime, "T1110")

    v1059 = _binding_of(runtime, "v15.1", "T1059")
    broken = replace(
        _binding_of(runtime, "v15.1", "T1110"),
        document_version_id=v1059.document_version_id,
    )
    runtime.attack_release_projections.bindings = [
        binding
        for binding in runtime.attack_release_projections.bindings
        if not (
            binding.source_release == "v15.1" and binding.technique_id == "T1110"
        )
    ]
    runtime.attack_release_projections.inject_binding(broken)
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionInvalidBindingError) as excinfo:
        await handler.import_release(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"
    _assert_zero_mutation(runtime, before)
    assert _active_version_id_of(runtime, "T1110") == live


async def test_a_binding_pointing_at_a_non_mitre_document_is_refused() -> None:
    """The projection target must be a MITRE_ATTACK document.

    The bound T1110 document's ``source_kind`` is rewritten to CURATED_GUIDANCE.
    Retrieval would otherwise serve a runbook while claiming v15.1 authority.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)
    live = _active_version_id_of(runtime, "T1110")

    document = await _document_for(runtime, "T1110")
    document.source_kind = SourceKind.CURATED_GUIDANCE
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionInvalidBindingError) as excinfo:
        await handler.import_release(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"
    _assert_zero_mutation(runtime, before)
    assert _active_version_id_of(runtime, "T1110") == live


async def test_a_binding_pointing_at_a_tenant_document_is_refused() -> None:
    """A TENANT document is unresolvable from the GLOBAL cutover -- fail closed.

    The cutover resolves through tenant ``None``, and reads are scope-filtered
    (a P3-A tenant-isolation invariant this closure does not touch). A TENANT
    document therefore does not resolve, and the refusal is MISSING_VERSION
    rather than INVALID_BINDING. That is still fail-closed with zero mutation;
    the code distinguishes "the row cannot be read" from "the relationship is
    invalid", and this is the former.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)
    live = _active_version_id_of(runtime, "T1110")

    document = await _document_for(runtime, "T1110")
    document.visibility = Visibility.TENANT
    document.tenant_id = "tenant-a"
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionMissingVersionError) as excinfo:
        await handler._cutover(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_MISSING_VERSION"
    _assert_zero_mutation(runtime, before)
    assert document.active_version_id == live


async def test_a_binding_with_the_wrong_external_key_is_refused() -> None:
    """T1110's binding must name the ``mitre-attack:T1110`` document.

    Pointing it at the T1059 document's external key fails even though every
    row involved exists and is individually well-formed. Runs the cutover
    directly: a full re-import would re-stage around the mutation instead of
    testing the cutover's own validation.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)

    document = await _document_for(runtime, "T1110")
    live = document.active_version_id
    document.external_key = "mitre-attack:T1059"
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionInvalidBindingError) as excinfo:
        await handler._cutover(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"
    _assert_zero_mutation(runtime, before)
    assert document.active_version_id == live


async def test_a_binding_whose_hash_chain_is_broken_is_refused() -> None:
    """canonical == binding == version, all three links.

    Breaks the binding-to-version link by replacing the staged version with one
    carrying different content (and its own valid hash) for the same document.
    Same code, same zero-mutation guarantee. Runs the cutover directly so
    re-staging cannot converge around the break first.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)
    live = _active_version_id_of(runtime, "T1110")

    binding = _binding_of(runtime, "v15.1", "T1110")
    staged_version = runtime.documents.versions[binding.document_version_id]
    runtime.documents.versions[binding.document_version_id] = replace(
        staged_version,
        normalized_content=staged_version.normalized_content + "\nExtra.\n",
        content_hash=compute_content_hash(
            normalize_knowledge_content(
                staged_version.normalized_content + "\nExtra.\n"
            )
        ),
    )
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionInvalidBindingError) as excinfo:
        await handler._cutover(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"
    _assert_zero_mutation(runtime, before)
    assert _active_version_id_of(runtime, "T1110") == live


async def test_a_bound_version_with_no_chunks_is_not_authoritative() -> None:
    """A version row existing is not the same as a version being retrievable.

    v15.1's T1110 version has its content chunks deleted AFTER staging, so the
    cutover -- run directly, because a full re-import would re-stage the chunks
    back -- must refuse with INCOMPLETE rather than make the framework
    authoritative for content retrieval cannot return.
    """
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await _staged_v15(runtime, handler)
    live = _active_version_id_of(runtime, "T1110")

    binding = _binding_of(runtime, "v15.1", "T1110")
    runtime.chunks.chunks = {
        chunk_id: chunk
        for chunk_id, chunk in runtime.chunks.chunks.items()
        if chunk.document_version_id != binding.document_version_id
    }
    before = _zero_mutation_snapshot(runtime)

    with pytest.raises(AttackReleaseProjectionIncompleteError) as excinfo:
        await handler._cutover(_command(release="v15.1", activate=True))

    assert excinfo.value.code == "ATTACK_RELEASE_PROJECTION_INCOMPLETE"
    _assert_zero_mutation(runtime, before)
    assert _active_version_id_of(runtime, "T1110") == live
