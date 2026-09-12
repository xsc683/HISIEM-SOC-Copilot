"""Write-only retrieval-quality artifact for the KB-GOLDEN-V1 baseline.

The artifact is a MEASUREMENT RECORD. It is deliberately write-only and
deliberately narrow, because a baseline is only useful if it can be handed to
someone else without handing them anything else along with it.

It must NEVER contain, and no caller may add:

* embedding vectors or any part of a vector (they are model-derived bulk data,
  they are not needed to re-score a ranking, and they are large enough to turn a
  baseline file into a payload);
* credentials, API keys, tokens, or connection strings of any kind;
* environment variables or host paths;
* raw HTTP requests or responses from any provider;
* full document bodies -- only bounded excerpts (at most
  ``_EXCERPT_LIMIT`` characters) may appear, which is what ``_bounded_excerpt``
  exists to guarantee.

What it does contain is exactly the arithmetic: per-mode metrics, the raw counts
that produced them, the bounded document rankings, and the hybrid verdict with
the comparison behind it. Anyone holding the file can recompute every number in
it, which is the point.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .runner import HYBRID_RECALL_TOLERANCE, CaseResult, ModeResult, SuiteResult

SCHEMA_VERSION = "knowledge-retrieval-eval/v1"

#: Hard ceiling on every excerpt the artifact carries. The production pipeline
#: bounds excerpts too; this is a second, independent bound so that a future
#: change upstream cannot widen what the artifact stores.
_EXCERPT_LIMIT = 240


def _bounded_excerpt(text: str) -> str:
    """Return at most ``_EXCERPT_LIMIT`` characters of ``text``, ellipsis included.

    Whitespace is collapsed first so a multi-line chunk cannot smuggle extra
    structure into a single-line field. The ellipsis is counted against the limit,
    so the bound is a real bound and not one the marker can push past.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= _EXCERPT_LIMIT:
        return collapsed
    return collapsed[: _EXCERPT_LIMIT - len("...")].rstrip() + "..."


def _case_entry(mode: str, result: CaseResult) -> dict[str, object]:
    return {
        "mode": mode,
        "case_id": result.case_id,
        "category": result.category,
        "tenant_id": result.tenant_id,
        "ranked_document_keys": list(result.ranked_document_keys),
        "top_document_key": result.top_document_key,
        "first_relevant_document_key": result.first_relevant_document_key,
        "metrics": {
            "recall_at_k": result.recall_at_k,
            "reciprocal_rank": result.reciprocal_rank,
            "ndcg_at_k": result.ndcg_at_k,
            "excluded_from_means": result.excluded_from_means,
        },
        "counts": {
            "hits_returned": result.hits_returned,
            "resolved_hits": result.resolved_hits,
            "claim_mismatches": result.claim_mismatches,
            "citation_resolution_rate": result.citation_resolution_rate,
            "cross_tenant_leakage_count": result.cross_tenant_leakage_count,
            "forbidden_retrieval_count": result.forbidden_retrieval_count,
        },
        "hits": [
            {
                "document_key": hit.document_key,
                "citation_id": hit.citation_id,
                "excerpt": _bounded_excerpt(hit.excerpt),
                "resolved": hit.resolved,
            }
            for hit in result.hits
        ],
    }


def _mode_entry(result: ModeResult) -> dict[str, object]:
    return {
        "available": result.available,
        "unavailable_reason": result.unavailable_reason,
        "k": result.k,
        "cases_scored": result.cases_scored,
        "cases_excluded": result.cases_excluded,
        "mean_recall_at_k": result.mean_recall_at_k,
        "mean_reciprocal_rank": result.mean_reciprocal_rank,
        "mean_ndcg_at_k": result.mean_ndcg_at_k,
        "hits_returned": result.hits_returned,
        "resolved_hits": result.resolved_hits,
        "citation_resolution_rate": result.citation_resolution_rate,
        "claim_mismatches": result.claim_mismatches,
        "cross_tenant_leakage_count": result.cross_tenant_leakage_count,
        "forbidden_retrieval_count": result.forbidden_retrieval_count,
    }


def build_artifact(
    *,
    suite: SuiteResult,
    retrieval_profile: Mapping[str, object],
    embedding_profile: Mapping[str, object],
) -> dict[str, object]:
    """Build the artifact mapping from a completed suite.

    ``retrieval_profile`` and ``embedding_profile`` are supplied by the caller as
    plain mappings. The evaluation boundary forbids this package from importing
    the production profile types, and it is also the right split: the profile
    descriptors are an INPUT to the measurement, so the harness records what it
    was told rather than reaching for a configuration of its own.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "suite_id": suite.suite_id,
        "corpus_version": suite.corpus_version,
        "retrieval_profile": dict(retrieval_profile),
        "embedding_profile": dict(embedding_profile),
        "case_count": suite.case_count,
        "modes": {mode.mode: _mode_entry(mode) for mode in suite.modes},
        "citation_integrity": {
            "by_mode": {
                mode.mode: {
                    "hits_returned": mode.hits_returned,
                    "resolved_hits": mode.resolved_hits,
                    "citation_resolution_rate": mode.citation_resolution_rate,
                    "claim_mismatches": mode.claim_mismatches,
                }
                for mode in suite.modes
            },
            "hits_returned": sum(mode.hits_returned for mode in suite.modes),
            "resolved_hits": sum(mode.resolved_hits for mode in suite.modes),
            "claim_mismatches": sum(mode.claim_mismatches for mode in suite.modes),
        },
        "tenant_leakage": {
            "by_mode": {
                mode.mode: mode.cross_tenant_leakage_count for mode in suite.modes
            },
            "total": sum(mode.cross_tenant_leakage_count for mode in suite.modes),
        },
        "forbidden_retrieval": {
            "by_mode": {
                mode.mode: mode.forbidden_retrieval_count for mode in suite.modes
            },
            "total": sum(mode.forbidden_retrieval_count for mode in suite.modes),
        },
        "hybrid_gate": {
            "verdict": suite.hybrid_gate,
            "detail": suite.hybrid_gate_detail,
            "recall_tolerance": HYBRID_RECALL_TOLERANCE,
        },
        "per_case": [
            _case_entry(mode.mode, result)
            for mode in suite.modes
            for result in mode.case_results
        ],
    }


def write_artifact(
    path: str | Path, artifact: Mapping[str, object], *, overwrite: bool = False
) -> Path:
    """Write ``artifact`` as pretty, sorted, UTF-8 JSON and return the path.

    Refuses to replace an existing file unless ``overwrite=True``: a baseline is
    evidence, and silently replacing evidence is how two people end up quoting
    different numbers for the same suite.

    The file is opened with an explicit ``newline="\\n"`` so the bytes are
    identical on Windows and POSIX checkouts; a baseline that hashes differently
    per platform is not a baseline.

    The parent directory is created if absent. The default output directory
    (``.eval-runs/knowledge``) does not exist on a fresh checkout, and failing
    there would throw away a completed run -- every ingestion and every
    embedding call -- at the last possible step, the most expensive place to
    fail.
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not overwrite:
        raise FileExistsError(
            f"artifact already exists at {target}; pass overwrite=True to replace it"
        )
    payload = json.dumps(dict(artifact), indent=2, sort_keys=True, ensure_ascii=False)
    with target.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload + "\n")
    return target
