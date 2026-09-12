"""Mode harness for the KB-GOLDEN-V1 retrieval baseline.

The runner is deliberately ignorant of databases, embeddings, SQLAlchemy, and the
production retrieval service. It receives every retrieval behaviour through two
injected async callables:

* ``retrieve(tenant_id, query, mode) -> hits``: run one retrieval for one case in
  one mode;
* ``resolve(tenant_id, citation_id) -> bool``: re-validate one citation handle.

That is what keeps this package independently testable -- the unit tests drive it
with fakes and never touch a database -- and it is why the harness cannot quietly
become a second production path: it has no way to reach one.

What the runner DOES own is the accounting: it bounds each ranking to ``k``
documents, scores it with the pure metrics, and records the raw counts (hits
returned, citations resolved, forbidden hits, cross-tenant hits) that the
artifact needs. It also decides the hybrid verdict, honestly: a run where the
vector channel never came up reports ``NOT_RUN``, never a pass.
"""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from .corpus import (
    CATEGORY_EXACT_IDENTIFIER,
    CATEGORY_SEMANTIC_GUIDANCE,
    CORPUS_VERSION,
    SUITE_ID,
    CorpusCase,
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

#: How far HYBRID's mean Recall@k may fall below the better single-channel
#: baseline before the hybrid claim is rejected. A small tolerance is allowed
#: because fusion re-orders results and a case can legitimately trade one rank
#: position for another; anything larger would let a genuine regression pass as
#: noise.
HYBRID_RECALL_TOLERANCE = 0.02

#: Categories whose cases exist specifically to test that mixing two channels
#: finds what neither channel finds alone. A hybrid result must retrieve every
#: one of them; a miss here cannot be excused by a tolerance.
_MIXED_CATEGORIES = frozenset({CATEGORY_SEMANTIC_GUIDANCE, CATEGORY_EXACT_IDENTIFIER})


class ModeUnavailableError(RuntimeError):
    """Raised by an injected ``retrieve`` when a mode could not run at all.

    ONLY this exception is read as "the channel is unavailable". Every other
    exception propagates and fails the evaluation loudly: a genuine defect must
    not be silently re-labelled as an unavailable channel and reported as a
    ``NOT_RUN`` gate.
    """


class EvalMode(enum.StrEnum):
    """Which candidate generators the caller asked for.

    A local copy of the production enum's values. The evaluation boundary forbids
    this package from importing the production retrieval contract, so the wheel is
    not reinvented here -- only its three names are restated, and only so the
    injected callable and the artifact can agree on a spelling.
    """

    LEXICAL_ONLY = "LEXICAL_ONLY"
    VECTOR_ONLY = "VECTOR_ONLY"
    HYBRID = "HYBRID"


class HybridGate(enum.StrEnum):
    """The honest verdict on whether hybrid retrieval earned its place."""

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_RUN = "NOT_RUN"


@dataclass(frozen=True)
class EvalQuery:
    """One case expressed as the retrieval question it is.

    Mirrors the shape of a caller's query (topic, bounded context terms, limit)
    and adds the case id so the injected retriever can key its own fixtures
    without the runner having to pass a corpus object across the boundary.
    """

    case_id: str
    tenant_id: str
    topic: str
    context_terms: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class RetrievedHit:
    """One hit as the injected retriever reports it.

    Every field is DATA. ``excerpt`` in particular is document text: a poisoned
    document's excerpt is exactly as untrusted as any other, and it confers no
    authority -- the only thing that can make a hit credible is the injected
    resolver re-validating its citation, which the runner does for every hit.

    ``resolved`` is the retriever's own claim. The runner does not trust it and
    reports the resolver's answer instead; the claim is kept only so a
    disagreement shows up as a count.
    """

    citation_id: str
    document_key: str
    document_id: str
    document_version_id: str
    chunk_ordinal: int
    title: str
    excerpt: str
    source_kind: str
    language: str
    source_version: str | None
    owning_tenant_id: str | None
    resolved: bool


RetrieveFn = Callable[[str, EvalQuery, str], Awaitable[Sequence[RetrievedHit]]]
ResolveFn = Callable[[str, str], Awaitable[bool]]


@dataclass(frozen=True)
class CaseHit:
    """A returned hit reduced to what the artifact may carry."""

    document_key: str
    citation_id: str
    excerpt: str
    resolved: bool


@dataclass(frozen=True)
class CaseResult:
    """One case, one mode: the bounded ranking, the metrics, and the raw counts.

    ``ranked_document_keys`` is the document-level ranking bounded to ``k``, which
    is what the ranking metrics score. The leakage and forbidden counts are
    deliberately computed over ALL returned hits instead of the top ``k``: a
    cross-tenant leak at rank six is still a leak.
    """

    case_id: str
    category: str
    tenant_id: str
    ranked_document_keys: tuple[str, ...]
    returned_document_keys: tuple[str, ...]
    top_document_key: str | None
    first_relevant_document_key: str | None
    hits: tuple[CaseHit, ...]
    hits_returned: int
    resolved_hits: int
    claim_mismatches: int
    relevant_document_keys: tuple[str, ...]
    forbidden_document_keys: tuple[str, ...]
    recall_at_k: float | None
    reciprocal_rank: float | None
    ndcg_at_k: float
    citation_resolution_rate: float
    cross_tenant_leakage_count: int
    forbidden_retrieval_count: int
    excluded_from_means: bool


@dataclass(frozen=True)
class ModeResult:
    """One mode's results across the whole case set."""

    mode: str
    k: int
    available: bool
    unavailable_reason: str | None
    case_results: tuple[CaseResult, ...]
    cases_scored: int
    cases_excluded: int
    mean_recall_at_k: float | None
    mean_reciprocal_rank: float | None
    mean_ndcg_at_k: float | None
    hits_returned: int
    resolved_hits: int
    citation_resolution_rate: float
    claim_mismatches: int
    cross_tenant_leakage_count: int
    forbidden_retrieval_count: int


@dataclass(frozen=True)
class SuiteResult:
    """Every requested mode plus the hybrid verdict for the suite."""

    suite_id: str
    corpus_version: str
    k: int
    case_count: int
    modes: tuple[ModeResult, ...]
    hybrid_gate: str
    hybrid_gate_detail: str

    def mode_result(self, mode: EvalMode | str) -> ModeResult | None:
        """Return the result for ``mode``, or ``None`` when it was not run."""
        wanted = str(mode)
        for result in self.modes:
            if result.mode == wanted:
                return result
        return None


async def run_mode(
    *,
    cases: Sequence[CorpusCase],
    retrieve: RetrieveFn,
    resolve: ResolveFn,
    mode: EvalMode | str,
    k: int = 5,
) -> ModeResult:
    """Run every case in one mode and return the scored results.

    A mode that raises :class:`ModeUnavailableError` yields an unavailable
    ``ModeResult`` with no case results, which is what the hybrid gate reads as
    ``NOT_RUN``. Cases are run sequentially: a ranking baseline is not a latency
    benchmark, and a sequential loop makes the recorded order of the raw counts
    deterministic.
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    resolved_mode = EvalMode(mode)
    try:
        results = tuple(
            [
                await _run_case(case, retrieve=retrieve, resolve=resolve, mode=resolved_mode, k=k)
                for case in cases
            ]
        )
    except ModeUnavailableError as exc:
        return ModeResult(
            mode=resolved_mode.value,
            k=k,
            available=False,
            unavailable_reason=str(exc),
            case_results=(),
            cases_scored=0,
            cases_excluded=0,
            mean_recall_at_k=None,
            mean_reciprocal_rank=None,
            mean_ndcg_at_k=None,
            hits_returned=0,
            resolved_hits=0,
            citation_resolution_rate=0.0,
            claim_mismatches=0,
            cross_tenant_leakage_count=0,
            forbidden_retrieval_count=0,
        )
    return _summarize(resolved_mode, k, results)


async def run_suite(
    *,
    cases: Sequence[CorpusCase],
    retrieve: RetrieveFn,
    resolve: ResolveFn,
    suite_id: str = SUITE_ID,
    corpus_version: str = CORPUS_VERSION,
    modes: Sequence[EvalMode | str] = (
        EvalMode.LEXICAL_ONLY,
        EvalMode.VECTOR_ONLY,
        EvalMode.HYBRID,
    ),
    k: int = 5,
) -> SuiteResult:
    """Run every requested mode and compute the hybrid verdict."""
    if k < 1:
        raise ValueError("k must be >= 1")
    ordered = tuple(EvalMode(mode) for mode in modes)
    mode_results = tuple(
        [
            await run_mode(cases=cases, retrieve=retrieve, resolve=resolve, mode=mode, k=k)
            for mode in ordered
        ]
    )
    verdict, detail = hybrid_verdict(mode_results, requested=ordered)
    return SuiteResult(
        suite_id=suite_id,
        corpus_version=corpus_version,
        k=k,
        case_count=len(cases),
        modes=mode_results,
        hybrid_gate=verdict.value,
        hybrid_gate_detail=detail,
    )


def hybrid_verdict(
    modes: Sequence[ModeResult], *, requested: Sequence[EvalMode]
) -> tuple[HybridGate, str]:
    """Return the hybrid gate verdict and the comparison behind it.

    The gate is a measurement, not an assertion. It reports ``PASS`` only when
    HYBRID's mean Recall@k is within :data:`HYBRID_RECALL_TOLERANCE` of the
    better single-channel baseline AND every mixed case was retrieved by HYBRID;
    it reports ``NOT_RUN`` whenever the vector channel could not run, because
    there is nothing to compare against; and it reports ``FAIL`` with the concrete
    numbers otherwise. There is no path that produces ``PASS`` without a number.
    """
    if EvalMode.HYBRID not in requested:
        return (
            HybridGate.NOT_RUN,
            "hybrid mode was not requested, so there is no hybrid ranking to gate",
        )
    if EvalMode.VECTOR_ONLY not in requested:
        return (
            HybridGate.NOT_RUN,
            "the vector channel was excluded from this run, so the hybrid comparison is undefined",
        )
    hybrid = _find_mode(modes, EvalMode.HYBRID)
    vector = _find_mode(modes, EvalMode.VECTOR_ONLY)
    lexical = _find_mode(modes, EvalMode.LEXICAL_ONLY)
    if hybrid is None or not hybrid.available:
        reason = hybrid.unavailable_reason if hybrid is not None else "no hybrid result produced"
        return HybridGate.NOT_RUN, f"the hybrid channel could not run: {reason}"
    if vector is None or not vector.available:
        reason = vector.unavailable_reason if vector is not None else "no vector result produced"
        return HybridGate.NOT_RUN, f"the vector channel could not run: {reason}"

    hybrid_recall = hybrid.mean_recall_at_k
    baselines = [
        result.mean_recall_at_k
        for result in (lexical, vector)
        if result is not None and result.available and result.mean_recall_at_k is not None
    ]
    if hybrid_recall is None or not baselines:
        return HybridGate.NOT_RUN, "no scored case was available to compare the channels on"
    better = max(baselines)

    mixed = [result for result in hybrid.case_results if result.category in _MIXED_CATEGORIES]
    missing = [
        result.case_id
        for result in mixed
        if not result.excluded_from_means and (result.recall_at_k or 0.0) <= 0.0
    ]
    if missing:
        return (
            HybridGate.FAIL,
            f"hybrid retrieved none of the relevant documents for {len(missing)} mixed case(s): "
            + ", ".join(missing[:3]),
        )
    if hybrid_recall + HYBRID_RECALL_TOLERANCE < better:
        return (
            HybridGate.FAIL,
            f"hybrid mean recall@{hybrid.k} {hybrid_recall:.3f} is more than "
            f"{HYBRID_RECALL_TOLERANCE:.2f} below the better single-channel baseline {better:.3f}",
        )
    return (
        HybridGate.PASS,
        f"hybrid mean recall@{hybrid.k} {hybrid_recall:.3f} against the better single-channel "
        f"baseline {better:.3f} (tolerance {HYBRID_RECALL_TOLERANCE:.2f}); "
        f"{len(mixed)} mixed case(s) retrieved",
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _run_case(
    case: CorpusCase,
    *,
    retrieve: RetrieveFn,
    resolve: ResolveFn,
    mode: EvalMode,
    k: int,
) -> CaseResult:
    query = EvalQuery(
        case_id=case.case_id,
        tenant_id=case.tenant_id,
        topic=case.topic,
        context_terms=case.context_terms,
        limit=case.expected_limit,
    )
    raw_hits = tuple(await retrieve(case.tenant_id, query, mode.value))

    hits: list[CaseHit] = []
    resolutions: list[bool] = []
    claim_mismatches = 0
    for hit in raw_hits:
        # The resolver, not the retriever, decides whether a citation resolves.
        confirmed = await resolve(case.tenant_id, hit.citation_id)
        if hit.resolved and not confirmed:
            claim_mismatches += 1
        resolutions.append(confirmed)
        hits.append(
            CaseHit(
                document_key=hit.document_key,
                citation_id=hit.citation_id,
                excerpt=hit.excerpt,
                resolved=confirmed,
            )
        )

    returned_keys = tuple(hit.document_key for hit in raw_hits)
    ranked = bounded_document_keys(returned_keys, k=k)
    relevant = case.relevant_document_keys
    return CaseResult(
        case_id=case.case_id,
        category=case.category,
        tenant_id=case.tenant_id,
        ranked_document_keys=ranked,
        returned_document_keys=returned_keys,
        top_document_key=ranked[0] if ranked else None,
        first_relevant_document_key=_first_relevant(ranked, relevant),
        hits=tuple(hits),
        hits_returned=len(raw_hits),
        resolved_hits=sum(1 for resolved in resolutions if resolved),
        claim_mismatches=claim_mismatches,
        relevant_document_keys=relevant,
        forbidden_document_keys=case.forbidden_document_keys,
        recall_at_k=recall_at_k(ranked, relevant, k=k),
        reciprocal_rank=reciprocal_rank(ranked, relevant),
        ndcg_at_k=ndcg_at_k(ranked, relevant, k=k),
        citation_resolution_rate=citation_resolution_rate(resolutions),
        cross_tenant_leakage_count=cross_tenant_leakage_count(
            [hit.owning_tenant_id for hit in raw_hits], query_tenant_id=case.tenant_id
        ),
        forbidden_retrieval_count=forbidden_retrieval_count(
            returned_keys, case.forbidden_document_keys
        ),
        excluded_from_means=not relevant,
    )


def _first_relevant(ranked: Sequence[str], relevant: Sequence[str]) -> str | None:
    wanted = set(relevant)
    for key in ranked:
        if key in wanted:
            return key
    return None


def _summarize(mode: EvalMode, k: int, results: tuple[CaseResult, ...]) -> ModeResult:
    """Aggregate case results into one mode summary.

    The mode-level citation rate is computed from the TOTALS rather than as the
    mean of the per-case rates: a case with one hit must not carry the same weight
    as a case with five.
    """
    scored = [result for result in results if not result.excluded_from_means]
    total_hits = sum(result.hits_returned for result in results)
    total_resolved = sum(result.resolved_hits for result in results)
    return ModeResult(
        mode=mode.value,
        k=k,
        available=True,
        unavailable_reason=None,
        case_results=results,
        cases_scored=len(scored),
        cases_excluded=len(results) - len(scored),
        mean_recall_at_k=_mean(
            [result.recall_at_k for result in scored if result.recall_at_k is not None]
        ),
        mean_reciprocal_rank=_mean(
            [
                result.reciprocal_rank
                for result in scored
                if result.reciprocal_rank is not None
            ]
        ),
        mean_ndcg_at_k=_mean([result.ndcg_at_k for result in scored]),
        hits_returned=total_hits,
        resolved_hits=total_resolved,
        citation_resolution_rate=(total_resolved / total_hits) if total_hits else 0.0,
        claim_mismatches=sum(result.claim_mismatches for result in results),
        cross_tenant_leakage_count=sum(
            result.cross_tenant_leakage_count for result in results
        ),
        forbidden_retrieval_count=sum(result.forbidden_retrieval_count for result in results),
    )


def _mean(values: Sequence[float]) -> float | None:
    """Mean of the scored values, or ``None`` when nothing was scored."""
    if not values:
        return None
    return sum(values) / len(values)


def _find_mode(modes: Sequence[ModeResult], mode: EvalMode) -> ModeResult | None:
    for result in modes:
        if result.mode == mode.value:
            return result
    return None
