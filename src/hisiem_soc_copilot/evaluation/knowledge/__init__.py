"""KB-GOLDEN-V1 retrieval-quality baseline: fixture, metrics, harness, artifact.

The package is deliberately self-contained. It imports nothing from the
production layers (``domain``, ``application``, ``infrastructure``, ``bootstrap``)
and receives all retrieval behaviour through injected async callables, so the
suite can be run and re-scored without a database, a model, or a network. The
architecture test ``tests/architecture/test_evaluation_boundary.py`` enforces
that surface; this package must never become a second production path.
"""

from __future__ import annotations

from .artifact import SCHEMA_VERSION, build_artifact, write_artifact
from .corpus import (
    CASES,
    CATEGORIES,
    CATEGORY_ATTACK_QUERY,
    CATEGORY_CROSS_TENANT_DENIED,
    CATEGORY_EXACT_IDENTIFIER,
    CATEGORY_NEAR_MISS_WRONG_DOC,
    CATEGORY_PROMPT_INJECTION_POISON,
    CATEGORY_RETIRED_EXCLUSION,
    CATEGORY_SCOPE_COMPETITION,
    CATEGORY_SEMANTIC_GUIDANCE,
    CATEGORY_TENANT_RUNBOOK,
    CORPUS,
    CORPUS_VERSION,
    POISONED_DOCUMENT_KEYS,
    PROMPT_INJECTION_MARKERS,
    RETIRED_DOCUMENT_KEYS,
    SUITE_ID,
    TENANTS,
    CorpusCase,
    CorpusDocument,
)
from .metrics import (
    bounded_document_keys,
    citation_resolution_rate,
    cross_tenant_leakage_count,
    forbidden_retrieval_count,
    ndcg_at_k,
    recall_at_k,
    reciprocal_rank,
)
from .runner import (
    HYBRID_RECALL_TOLERANCE,
    CaseHit,
    CaseResult,
    EvalMode,
    EvalQuery,
    HybridGate,
    ModeResult,
    ModeUnavailableError,
    ResolveFn,
    RetrievedHit,
    RetrieveFn,
    SuiteResult,
    hybrid_verdict,
    run_mode,
    run_suite,
)

__all__ = [
    "CASES",
    "CATEGORIES",
    "CATEGORY_ATTACK_QUERY",
    "CATEGORY_CROSS_TENANT_DENIED",
    "CATEGORY_EXACT_IDENTIFIER",
    "CATEGORY_NEAR_MISS_WRONG_DOC",
    "CATEGORY_PROMPT_INJECTION_POISON",
    "CATEGORY_RETIRED_EXCLUSION",
    "CATEGORY_SCOPE_COMPETITION",
    "CATEGORY_SEMANTIC_GUIDANCE",
    "CATEGORY_TENANT_RUNBOOK",
    "CORPUS",
    "CORPUS_VERSION",
    "HYBRID_RECALL_TOLERANCE",
    "POISONED_DOCUMENT_KEYS",
    "PROMPT_INJECTION_MARKERS",
    "RETIRED_DOCUMENT_KEYS",
    "SCHEMA_VERSION",
    "SUITE_ID",
    "TENANTS",
    "CaseHit",
    "CaseResult",
    "CorpusCase",
    "CorpusDocument",
    "EvalMode",
    "EvalQuery",
    "HybridGate",
    "ModeResult",
    "ModeUnavailableError",
    "ResolveFn",
    "RetrievedHit",
    "RetrieveFn",
    "SuiteResult",
    "bounded_document_keys",
    "build_artifact",
    "citation_resolution_rate",
    "cross_tenant_leakage_count",
    "forbidden_retrieval_count",
    "hybrid_verdict",
    "ndcg_at_k",
    "recall_at_k",
    "reciprocal_rank",
    "run_mode",
    "run_suite",
    "write_artifact",
]
