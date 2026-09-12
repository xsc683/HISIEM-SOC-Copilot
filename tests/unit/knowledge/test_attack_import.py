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
from hisiem_soc_copilot.application.errors import ApplicationError
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
    AttackImportOutcome,
    AttackTechniqueRecord,
    ChunkProjectionState,
    EmbeddingProfileRecord,
    KnowledgeChunkRecord,
    KnowledgeChunkView,
    LexicalCandidate,
    VectorCandidate,
)
from hisiem_soc_copilot.application.services.attack_projection import (
    technique_document_body,
    technique_document_title,
    technique_external_key,
)
from hisiem_soc_copilot.domain.knowledge.entities import (
    KnowledgeDocument,
    KnowledgeDocumentVersion,
)
from hisiem_soc_copilot.domain.knowledge.enums import SourceKind, Visibility
from hisiem_soc_copilot.domain.knowledge.errors import InvalidMitreBundleError
from hisiem_soc_copilot.domain.knowledge.value_objects import normalize_and_hash
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

    def __init__(self) -> None:
        self._descriptor = EmbeddingProfileDescriptor(
            provider=PROVIDER,
            model_id=MODEL_ID,
            dimension=DIMENSION,
            distance_metric="COSINE",
            normalization="NONE",
            profile_version=1,
        )
        self.calls: list[tuple[str, ...]] = []

    @property
    def descriptor(self) -> EmbeddingProfileDescriptor:
        return self._descriptor

    async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
        self.calls.append(tuple(texts))
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
    """In-memory chunk store; the projection state is DERIVED from its rows."""

    def __init__(self) -> None:
        self.chunks: dict[UUID, KnowledgeChunkRecord] = {}
        self.added_batches: list[tuple[KnowledgeChunkRecord, ...]] = []
        self.deleted_versions: list[UUID] = []

    def rows_for_version(self, document_version_id: UUID) -> tuple[KnowledgeChunkRecord, ...]:
        return tuple(
            sorted(
                (
                    chunk
                    for chunk in self.chunks.values()
                    if chunk.document_version_id == document_version_id
                ),
                key=lambda chunk: chunk.ordinal,
            )
        )

    async def add_many(self, *, chunks: Sequence[KnowledgeChunkRecord]) -> None:
        self.added_batches.append(tuple(chunks))
        for chunk in chunks:
            self.chunks[chunk.id] = chunk

    async def count_for_version(self, *, document_version_id: UUID) -> int:
        return len(self.rows_for_version(document_version_id))

    async def projection_state(self, *, document_version_id: UUID) -> ChunkProjectionState:
        rows = self.rows_for_version(document_version_id)
        if not rows:
            return ChunkProjectionState(
                embedding_profile_id=None, chunker_version=None, chunk_count=0
            )
        return ChunkProjectionState(
            embedding_profile_id=rows[0].embedding_profile_id,
            chunker_version=rows[0].chunker_version,
            chunk_count=len(rows),
        )

    async def delete_for_version(self, *, document_version_id: UUID) -> None:
        self.deleted_versions.append(document_version_id)
        for chunk_id in [
            chunk.id
            for chunk in self.chunks.values()
            if chunk.document_version_id == document_version_id
        ]:
            del self.chunks[chunk_id]

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


class FakeAttackTechniqueRepository:
    """In-memory canonical-technique store for the pinned releases."""

    def __init__(self) -> None:
        self.rows: list[AttackTechniqueRecord] = []
        self.add_many_calls: list[tuple[AttackTechniqueRecord, ...]] = []
        self.deactivate_calls: list[tuple[str, str]] = []

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

    async def deactivate_except(self, *, framework: str, source_release: str) -> int:
        self.deactivate_calls.append((framework, source_release))
        changed = 0
        updated: list[AttackTechniqueRecord] = []
        for row in self.rows:
            if (
                row.framework == framework
                and row.source_release != source_release
                and row.active
            ):
                updated.append(replace(row, active=False))
                changed += 1
            else:
                updated.append(row)
        self.rows = updated
        return changed


class FakeRuntime:
    """Every store the importer touches, plus the transaction counters."""

    def __init__(self) -> None:
        self.attack_techniques = FakeAttackTechniqueRepository()
        self.documents = FakeKnowledgeDocumentRepository()
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
    """The REAL ingestion use case, so imported documents are really written."""
    return KnowledgeIngestionHandler(
        unit_of_work_factory=runtime.factory,
        embedding_provider=provider if provider is not None else _provider(),
        chunker=StructureAwareChunker(),
        clock=FakeClock(T0),
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
    assert document.active_version_id is not None
    version = runtime.documents.versions[document.active_version_id]
    return version


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

    outcome = await handler.import_release(_command())

    assert isinstance(outcome, AttackImportOutcome)
    assert outcome.release == RELEASE
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
    assert brute_force.active is True
    assert brute_force.created_at == T0


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

        version = _version_of(runtime, document)
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

    document = await _document_for(runtime, technique.technique_id)
    version = _version_of(runtime, document)
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


async def test_reimporting_a_changed_bundle_adds_a_version_not_a_second_row() -> None:
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command())

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
    outcome = await handler.import_release(_command(payload=edited))

    assert outcome.techniques_created == 0
    assert outcome.versions_ingested == 1
    assert outcome.unchanged == 1
    assert await runtime.attack_techniques.count_for_release(
        framework=FRAMEWORK, source_release=RELEASE
    ) == 2
    document = await _document_for(runtime, "T1110")
    version = _version_of(runtime, document)
    assert version.version == 2
    assert "Revised upstream description." in version.normalized_content
    # The canonical row is INSERT-ONLY: a release that already has rows is never
    # rewritten, so it keeps the description (and hash) it was first imported
    # with while the document moves on. Re-importing a release does not refresh
    # its canonical rows -- correcting them would need an explicit re-import of
    # the release under a new name.
    row = await runtime.attack_techniques.find(
        framework=FRAMEWORK, technique_id="T1110", source_release=RELEASE
    )
    assert row is not None
    assert row.description == T1110_DESCRIPTION
    _, domain_hash = normalize_and_hash(technique_document_body(_brute_force_technique()))
    assert row.content_hash == domain_hash
    assert row.content_hash != version.content_hash


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
    # Flagged, never deleted: the older release is still readable by its own
    # source_release, which is what makes the switch reversible.
    assert await runtime.attack_techniques.count_for_release(
        framework=FRAMEWORK, source_release="v14.1"
    ) == 2
    assert runtime.attack_techniques.deactivate_calls == [
        (FRAMEWORK, "v14.1"),
        (FRAMEWORK, "v15.1"),
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
    runtime = FakeRuntime()
    handler = _importer(runtime=runtime)
    await handler.import_release(_command(release="v14.1", activate=True))

    outcome = await handler.import_release(_command(release="v15.1"))

    assert outcome.techniques_created == 2
    # The backfill added rows and never touched the switch.
    assert runtime.attack_techniques.deactivate_calls == [(FRAMEWORK, "v14.1")]
    assert all(row.active for row in runtime.attack_techniques.for_release("v14.1"))
    assert all(row.active for row in runtime.attack_techniques.for_release("v15.1"))


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
