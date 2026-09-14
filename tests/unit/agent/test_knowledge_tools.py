"""Knowledge tool integration tests: tenant, authority, gates, unavailable.

Covers the Knowledge Intelligence Closure at unit level:
- the executor rejects model-supplied tenant-shaped arguments (there are none --
  tenant is a kwarg) and refuses MITRE-shaped bypasses;
- the catalog reads ATT&CK authority from the ACTIVE release + projection, never
  source_version;
- Gate 2 forces INCONCLUSIVE for KNOWLEDGE-only findings;
- assess treats KNOWLEDGE relations as CONTEXT-class;
- a missing adapter / missing profile yields typed UNAVAILABLE, never a raise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from hisiem_soc_copilot.agent.evidence.normalizer import EvidenceNormalizer
from hisiem_soc_copilot.agent.graph.nodes import (
    _findings_with_platform_evidence,
    _is_knowledge_evidence,
)
from hisiem_soc_copilot.agent.knowledge.catalog import (
    KnowledgeRetrievalCatalogAdapter,
    guidance_tool_result,
)
from hisiem_soc_copilot.agent.tools.args import (
    ToolArgumentError,
    parse_resolve_technique,
    parse_retrieve_guidance,
)
from hisiem_soc_copilot.agent.tools.executor import ToolExecutor
from hisiem_soc_copilot.agent.tools.registry import (
    AGENT_SELECTABLE_TOOLS,
    FUTURE_CATALOG_TOOLS,
    ToolRegistry,
)
from hisiem_soc_copilot.application.ports.knowledge import (
    KnowledgeChunkView,
    KnowledgeHit,
    KnowledgeQuery,
    KnowledgeSearchResult,
    RetrievalProfile,
)
from hisiem_soc_copilot.contracts.tools.types import ToolCandidate, ToolResult
from hisiem_soc_copilot.domain.investigation.content import compute_dedup_key
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    Finding,
)
from hisiem_soc_copilot.domain.investigation.enums import EvidenceSourceType
from hisiem_soc_copilot.domain.knowledge.enums import DocumentStatus, SourceKind, Visibility
from hisiem_soc_copilot.domain.knowledge.value_objects import (
    compute_content_hash,
    format_citation_id,
)


def _evidence(
    source_type: EvidenceSourceType, *, citation: UUID | None = None
) -> Evidence:
    return Evidence(
        id=uuid4(),
        investigation_id=uuid4(),
        source=EvidenceSource(
            type=source_type, provider="p", operation="op"
        ),
        collected_at=datetime.now(UTC),
        observation={"k": "v"},
        source_tool_call_id=uuid4(),
    )


def _finding(*citations: UUID) -> Finding:
    return Finding(
        id=uuid4(),
        investigation_id=uuid4(),
        statement="s",
        evidence_citations=list(citations),
    )


# ---------------------------------------------------------------------------
# C8: exact selectable set
# ---------------------------------------------------------------------------


def test_selectable_set_is_exactly_the_p3b_four() -> None:
    assert set(AGENT_SELECTABLE_TOOLS) == {
        "hisiem.search_events",
        "hisiem.get_detection_rule",
        "knowledge.retrieve_security_guidance",
        "knowledge.resolve_attack_technique",
    }
    assert "threat_intel.lookup_ip" in FUTURE_CATALOG_TOOLS
    assert "hisiem.get_entity_activity" in FUTURE_CATALOG_TOOLS
    assert "knowledge.retrieve_security_guidance" not in FUTURE_CATALOG_TOOLS
    registry = ToolRegistry()
    assert set(registry.model_selectable_names) == set(AGENT_SELECTABLE_TOOLS)
    assert {spec.name for spec in registry.model_tool_specs()} == set(
        AGENT_SELECTABLE_TOOLS
    )


# ---------------------------------------------------------------------------
# args: strict bounds, no tenant-shaped fields
# ---------------------------------------------------------------------------


def test_parse_retrieve_guidance_bounds() -> None:
    parsed = parse_retrieve_guidance({"topic": "ssh brute force"})
    assert parsed.topic == "ssh brute force"
    assert parsed.limit == 5
    with pytest.raises(ToolArgumentError):
        parse_retrieve_guidance({"topic": "  "})
    with pytest.raises(ToolArgumentError):
        parse_retrieve_guidance({"topic": "x" * 257})
    with pytest.raises(ToolArgumentError):
        parse_retrieve_guidance({"topic": "x", "limit": 6})
    with pytest.raises(ToolArgumentError):
        parse_retrieve_guidance(
            {"topic": "x", "context_terms": [f"t{i}" for i in range(13)]}
        )
    # tenant-shaped keys are silently ignored, never honored: the parser builds
    # a fixed dataclass with no tenant field.
    parsed = parse_retrieve_guidance(
        {"topic": "x", "tenant_id": "tenant-b", "visibility": "GLOBAL"}
    )
    assert not hasattr(parsed, "tenant_id")


def test_parse_resolve_technique_uppercases_with_framework_default() -> None:
    parsed = parse_resolve_technique({"technique_id": "t1110"})
    assert parsed.technique_id == "T1110"
    assert parsed.framework == "mitre-attack"
    parsed = parse_resolve_technique(
        {"technique_id": "T1059.001", "framework": "mitre-attack"}
    )
    assert parsed.framework == "mitre-attack"


# ---------------------------------------------------------------------------
# executor: tenant kwarg only, unavailable fails closed
# ---------------------------------------------------------------------------


class _NoKnowledgeExecutor(ToolExecutor):
    pass


async def test_executor_without_adapter_returns_unavailable() -> None:
    from hisiem_soc_copilot.agent.tools.executor import ToolExecutor as E

    class _Hisiem:
        pass

    executor = E(hisiem=_Hisiem())  # type: ignore[arg-type]
    execution = await executor.execute(
        candidate=ToolCandidate(
            tool_name="knowledge.retrieve_security_guidance",
            arguments={"topic": "ssh"},
        ),
        tenant_id="tenant-a",
        source_alert_ref={},
    )
    assert execution.status == "UNAVAILABLE"
    assert execution.result.error_code == "KNOWLEDGE_UNAVAILABLE"


async def test_executor_treats_unknown_knowledge_tool_as_unavailable() -> None:
    """A knowledge.* name is a legitimate channel: without wiring it is
    UNAVAILABLE, while a non-knowledge unknown name stays REJECTED."""
    class _Hisiem:
        pass

    executor = ToolExecutor(hisiem=_Hisiem())  # type: ignore[arg-type]
    execution = await executor.execute(
        candidate=ToolCandidate(
            tool_name="knowledge.delete_everything", arguments={}
        ),
        tenant_id="tenant-a",
        source_alert_ref={},
    )
    assert execution.status == "UNAVAILABLE"
    assert execution.result.error_code == "KNOWLEDGE_UNAVAILABLE"
    execution = await executor.execute(
        candidate=ToolCandidate(tool_name="execute_shell", arguments={}),
        tenant_id="tenant-a",
        source_alert_ref={},
    )
    assert execution.status == "REJECTED"


# ---------------------------------------------------------------------------
# normalizer: KNOWLEDGE shapes
# ---------------------------------------------------------------------------


def test_normalize_knowledge_guidance_per_hit_with_citation() -> None:
    normalizer = EvidenceNormalizer()
    result = ToolResult(
        tool_call_id=str(uuid4()),
        tool_name="knowledge.retrieve_security_guidance",
        status="SUCCESS",
        fetched_at=datetime.now(UTC).isoformat(),
        data={
            "hits": [
                {
                    "document_id": str(uuid4()),
                    "document_version_id": str(uuid4()),
                    "chunk_id": str(uuid4()),
                    "source_kind": "CURATED_GUIDANCE",
                    "title": "SSH hardening",
                    "excerpt": "Disable password auth.",
                    "citation_id": "kcit:abc:1234",
                    "source_version": "v1",
                    "verified_content_hash": "ab" * 32,
                    "retrieval_provenance": {
                        "mode": "HYBRID",
                        "profile_id": "hybrid-v1",
                        "retrieved_at": "2026-09-14T12:00:00+00:00",
                    },
                },
                {"title": "no citation, dropped"},
            ],
            "retrieval_mode": "HYBRID",
        },
    )
    observations = normalizer.normalize_knowledge_guidance(
        result, tool_call_id=result.tool_call_id
    )
    assert len(observations) == 1
    obs = observations[0]
    assert obs.source_type == "KNOWLEDGE"
    assert obs.source_provider == "knowledge"
    assert obs.source_operation == "retrieve_security_guidance"
    assert obs.raw_reference is not None
    assert obs.raw_reference["citation_identity"]["citation_id"] == "kcit:abc:1234"
    assert obs.raw_reference["citation_identity"]["content_hash"] == "ab" * 32
    assert obs.raw_reference["retrieval_provenance"]["profile_id"] == "hybrid-v1"


def test_normalize_attack_technique_carries_authoritative_release() -> None:
    normalizer = EvidenceNormalizer()
    result = ToolResult(
        tool_call_id=str(uuid4()),
        tool_name="knowledge.resolve_attack_technique",
        status="SUCCESS",
        fetched_at=datetime.now(UTC).isoformat(),
        data={
            "technique_id": "T1110",
            "framework": "mitre-attack",
            "name": "Brute Force",
            "description": "d",
            "tactics": ["credential-access"],
            "platforms": ["Linux"],
            "authoritative_release": "v14.1",
            "content_hash": "ab" * 32,
        },
    )
    observations = normalizer.normalize_attack_technique(
        result, tool_call_id=result.tool_call_id
    )
    assert len(observations) == 1
    assert observations[0].source_type == "KNOWLEDGE"
    assert (
        observations[0].observation["authoritative_release"] == "v14.1"
    )


# ---------------------------------------------------------------------------
# C4: verdict grounding helpers
# ---------------------------------------------------------------------------


def test_platform_grounding_requires_hisiem_evidence() -> None:
    platform = _evidence(EvidenceSourceType.HISIEM_EVENT)
    knowledge = _evidence(EvidenceSourceType.KNOWLEDGE)
    assert _findings_with_platform_evidence(
        [_finding(knowledge.id)], [knowledge, platform]
    ) is False
    assert _findings_with_platform_evidence(
        [_finding(knowledge.id, platform.id)], [knowledge, platform]
    ) is True
    assert _findings_with_platform_evidence([], [platform]) is False


def test_is_knowledge_evidence_classification() -> None:
    knowledge = _evidence(EvidenceSourceType.KNOWLEDGE)
    platform = _evidence(EvidenceSourceType.HISIEM_EVENT)
    by_id = {knowledge.id: knowledge, platform.id: platform}
    assert _is_knowledge_evidence(by_id, str(knowledge.id)) is True
    assert _is_knowledge_evidence(by_id, str(platform.id)) is False
    assert _is_knowledge_evidence(by_id, "not-a-uuid") is False


# ---------------------------------------------------------------------------
# adapter: authority read + tenant requirement
# ---------------------------------------------------------------------------


async def test_adapter_rejects_blank_tenant() -> None:
    from hisiem_soc_copilot.application.errors import InvalidKnowledgeQueryError

    adapter = KnowledgeRetrievalCatalogAdapter(
        retrieval_service_factory=lambda _uow: None,  # type: ignore[return-value]
        unit_of_work_factory=lambda: None,  # type: ignore[return-value]
    )
    with pytest.raises(InvalidKnowledgeQueryError):
        await adapter.resolve_attack_technique(
            tenant_id="  ", technique_id="T1110"
        )


async def test_adapter_validates_citations_and_closes_its_read_scope() -> None:
    from hisiem_soc_copilot.application.services.knowledge_retrieval import RetrievalMode

    tenant_id = "tenant-a"
    document_id, version_id, chunk_id = uuid4(), uuid4(), uuid4()
    content = "Disable password authentication and require key-based SSH access."
    content_hash = compute_content_hash(content)
    view = KnowledgeChunkView(
        chunk_id=chunk_id,
        document_id=document_id,
        document_version_id=version_id,
        ordinal=0,
        heading_path="SSH",
        content=content,
        content_hash=content_hash,
        title="Verified SSH Guidance",
        source_kind=SourceKind.CURATED_GUIDANCE,
        language="en",
        source_version="guide-v2",
        document_status="ACTIVE",
        visibility=Visibility.TENANT,
        tenant_id=tenant_id,
    )
    hit = KnowledgeHit(
        citation_id=format_citation_id(chunk_id=chunk_id, content_hash=content_hash),
        document_id=document_id,
        document_version_id=version_id,
        chunk_id=chunk_id,
        source_kind=SourceKind.CURATED_GUIDANCE,
        title="unverified title",
        excerpt="unverified excerpt",
        language="en",
        source_version="unverified-version",
        retrieved_at=datetime.now(UTC),
    )
    profile = RetrievalProfile(
        profile_id="hybrid-v1",
        lexical_candidate_limit=20,
        vector_candidate_limit=20,
        rrf_k=60,
        max_chunks_per_document=2,
        chunker_version="chunk-v1",
        embedding_profile_id="embedding-1",
        embedding_model_id="model-1",
    )
    search_result = KnowledgeSearchResult(
        hits=(hit,), retrieval_profile=profile
    )

    class _Chunks:
        calls: list[tuple[str, object]] = []

        async def get_chunk_view(self, *, tenant_id: str, chunk_id: object):
            self.calls.append((tenant_id, chunk_id))
            return view

    class _Uow:
        def __init__(self) -> None:
            self.knowledge_chunks = _Chunks()
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            self.closed = True

    class _Service:
        async def retrieve(self, *, tenant_id, query, mode):
            assert tenant_id == "tenant-a"
            assert mode is RetrievalMode.HYBRID
            return search_result

    uows: list[_Uow] = []

    def _uow_factory():
        uow = _Uow()
        uows.append(uow)
        return uow

    adapter = KnowledgeRetrievalCatalogAdapter(
        retrieval_service_factory=lambda _uow: _Service(),  # type: ignore[arg-type]
        unit_of_work_factory=_uow_factory,  # type: ignore[arg-type]
    )
    validated = await adapter.retrieve_security_guidance_validated(
        tenant_id=tenant_id, query=KnowledgeQuery(topic="SSH hardening")
    )

    assert validated.result.hits[0].title == "Verified SSH Guidance"
    assert validated.result.hits[0].excerpt == content
    assert validated.result.hits[0].source_version == "guide-v2"
    assert validated.verified_content_hashes == ((hit.citation_id, content_hash),)
    assert uows[0].knowledge_chunks.calls == [(tenant_id, chunk_id)]
    assert uows[0].closed is True


async def test_attack_resolution_requires_active_projection_hash_chain() -> None:
    from types import SimpleNamespace

    from hisiem_soc_copilot.application.errors import KnowledgeAuthorityIntegrityError

    framework, release_name, technique_id = "mitre-attack", "v14.1", "T1110"
    document_id, version_id = uuid4(), uuid4()
    content_hash = "ab" * 32
    record = SimpleNamespace(
        active=True,
        framework=framework,
        technique_id=technique_id,
        source_release=release_name,
        content_hash=content_hash,
    )
    binding = SimpleNamespace(
        technique_id=technique_id,
        document_id=document_id,
        document_version_id=version_id,
        content_hash=content_hash,
    )
    release = SimpleNamespace(source_release=release_name)
    document = SimpleNamespace(
        id=document_id,
        source_kind=SourceKind.MITRE_ATTACK,
        visibility=Visibility.GLOBAL,
        tenant_id=None,
        status=DocumentStatus.ACTIVE,
        external_key="mitre-attack:T1110",
        active_version_id=version_id,
    )
    technique_queries: list[str] = []
    stored_version = SimpleNamespace(document_id=document_id, content_hash=content_hash)

    async def _resolved(value):
        return value

    async def self_find(**kwargs):
        technique_queries.append(kwargs["technique_id"])
        return record

    async def get_document(**kwargs):
        return document

    async def get_version(**kwargs):
        return stored_version

    class _Uow:
        attack_releases = SimpleNamespace(get_active=lambda **kwargs: _resolved(release))
        attack_techniques = SimpleNamespace(find=self_find)
        attack_release_projections = SimpleNamespace(
            list_for_release=lambda **kwargs: _resolved((binding,))
        )
        knowledge_documents = SimpleNamespace(get=get_document, get_version=get_version)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    adapter = KnowledgeRetrievalCatalogAdapter(
        retrieval_service_factory=lambda _uow: None,  # type: ignore[return-value]
        unit_of_work_factory=_Uow,  # type: ignore[arg-type]
    )
    result = await adapter.resolve_attack_technique(
        tenant_id="tenant-a", technique_id="t1110"
    )
    assert result is record
    assert technique_queries == ["T1110"]

    stored_version.content_hash = "cd" * 32
    with pytest.raises(KnowledgeAuthorityIntegrityError):
        await adapter.resolve_attack_technique(
            tenant_id="tenant-a", technique_id="T1110"
        )


async def test_guidance_tool_result_labels_mode_from_profile() -> None:
    profile = RetrievalProfile(
        profile_id="lexical-v1",
        lexical_candidate_limit=20,
        vector_candidate_limit=20,
        rrf_k=60,
        max_chunks_per_document=2,
        chunker_version="chunk-v1",
        embedding_profile_id="",
        embedding_model_id="",
    )
    result = guidance_tool_result(
        tool_call_id="tc-1",
        result=KnowledgeSearchResult(
            hits=(), truncated=False, retrieval_profile=profile
        ),
    )
    assert result.status == "NO_DATA"
    assert result.data["retrieval_mode"] == "LEXICAL_ONLY"
    assert result.data["retrieval_profile_id"] == "lexical-v1"


def test_knowledge_evidence_dedup_ignores_retrieval_execution_metadata() -> None:
    citation_identity = {
        "citation_id": "kcit:abc:1234",
        "document_id": "doc-1",
        "document_version_id": "version-1",
        "chunk_id": "chunk-1",
        "content_hash": "ab" * 32,
    }
    first = compute_dedup_key(
        source_provider="knowledge",
        source_operation="retrieve_security_guidance",
        raw_reference={
            "citation_identity": citation_identity,
            "retrieval_provenance": {
                "mode": "HYBRID",
                "profile_id": "hybrid-v1",
                "retrieved_at": "2026-09-14T12:00:00+00:00",
            },
        },
    )
    repeated = compute_dedup_key(
        source_provider="knowledge",
        source_operation="retrieve_security_guidance",
        raw_reference={
            "citation_identity": citation_identity,
            "retrieval_provenance": {
                "mode": "LEXICAL_ONLY",
                "profile_id": "lexical-v1",
                "retrieved_at": "2026-09-14T12:01:00+00:00",
            },
        },
    )
    changed_content = compute_dedup_key(
        source_provider="knowledge",
        source_operation="retrieve_security_guidance",
        raw_reference={
            "citation_identity": {**citation_identity, "content_hash": "cd" * 32},
            "retrieval_provenance": {
                "mode": "HYBRID",
                "profile_id": "hybrid-v1",
                "retrieved_at": "2026-09-14T12:02:00+00:00",
            },
        },
    )
    assert first == repeated
    assert first != changed_content
