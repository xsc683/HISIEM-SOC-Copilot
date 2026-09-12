"""Pure, deterministic retrieval-quality metrics for the KB-GOLDEN-V1 baseline.

There is no LLM judge anywhere in this module, and that is a design choice rather
than an omission. Scoring a ranking is an arithmetic question with a checkable
answer; routing it through a model would make the baseline non-reproducible and
would let a model's opinion masquerade as a measurement. Every function here is a
pure function of its arguments: no randomness, no clock, no environment, no I/O.
A recorded ranking can therefore be re-scored at any time and will produce the
same numbers.

Two conventions are load-bearing:

* **Exclusion-only cases return ``None``, never 0.0.** A RETIRED_EXCLUSION case
  asks "this document must not come back" and has an empty relevant set. Its
  recall and reciprocal rank are UNDEFINED, so they are ``None`` and the case is
  dropped from the mean instead of being scored as a failure it cannot have.
* **Metrics are computed over a document-level ranking.** Each document key
  counts once, at its first (best) rank: the production pipeline caps a document
  at two chunks, so the third occurrence of the same key would otherwise inflate
  recall without retrieving anything new.

``ndcg_at_k`` is deliberately secondary. Binary-gain nDCG over a document-level
ranking is a strictly finer view of what recall already reports, and the artifact
exposes it for diagnosis rather than as a headline number.
"""

from __future__ import annotations

from collections.abc import Sequence

#: ``ln(2)`` to double precision. The ``math`` module is NOT on the evaluation
#: import surface (see ``tests/architecture/test_evaluation_boundary.py``), and
#: nDCG needs a base-2 logarithm, so :func:`_log2` is implemented here instead of
#: imported. The value is a mathematical constant, not an environment read.
_LN2 = 0.6931471805599453


def _log2(value: int) -> float:
    """Return ``log2(value)`` for a positive integer, without ``math``.

    The argument is halved into ``[1, 2)`` while counting the exponent, then the
    ``atanh`` series ``ln(m) = 2 * (z + z^3/3 + z^5/5 + ...)`` with
    ``z = (m - 1) / (m + 1)`` is summed. Over ``m in [1, 2)`` the ratio is at
    most ``1/3``, so the series reaches double-precision agreement in far fewer
    terms than the loop bound allows. Deterministic and allocation-free.
    """
    if value <= 0:
        raise ValueError("log2 is undefined for non-positive values")
    exponent = 0
    mantissa = float(value)
    while mantissa >= 2.0:
        mantissa /= 2.0
        exponent += 1
    while mantissa < 1.0:
        mantissa *= 2.0
        exponent -= 1
    z = (mantissa - 1.0) / (mantissa + 1.0)
    z_squared = z * z
    term = z
    total = 0.0
    for divisor in range(1, 41, 2):
        total += term / divisor
        term *= z_squared
    return exponent + (2.0 * total / _LN2)


def bounded_document_keys(
    ranked_document_keys: Sequence[str], *, k: int
) -> tuple[str, ...]:
    """Return the first ``k`` distinct document keys, in rank order.

    Deduplication happens here rather than in every metric so that "the ranking"
    means one thing: a document is present once, at its best rank.
    """
    if k < 0:
        raise ValueError("k must be >= 0")
    bounded: list[str] = []
    for key in ranked_document_keys:
        if key not in bounded:
            bounded.append(key)
        if len(bounded) >= k:
            break
    return tuple(bounded)


def recall_at_k(
    ranked_document_keys: Sequence[str], relevant: Sequence[str], *, k: int
) -> float | None:
    """Fraction of relevant documents present in the top ``k``.

    Returns ``None`` when ``relevant`` is empty: the case is a pure exclusion
    case, recall is undefined, and the case-level aggregator must exclude it from
    the mean rather than score it as either a success or a failure.
    """
    wanted = set(relevant)
    if not wanted:
        return None
    top_k = set(bounded_document_keys(ranked_document_keys, k=k))
    return len(wanted & top_k) / len(wanted)


def reciprocal_rank(
    ranked_document_keys: Sequence[str], relevant: Sequence[str]
) -> float | None:
    """``1 / rank`` of the first relevant document, or ``0.0`` when none is found.

    Returns ``None`` when ``relevant`` is empty, for the same reason as
    :func:`recall_at_k`. Unlike recall this metric is unbounded in ``k`` by
    design: it answers "how far down the page did the responder have to look".
    """
    wanted = set(relevant)
    if not wanted:
        return None
    seen: set[str] = set()
    rank = 0
    for key in ranked_document_keys:
        if key in seen:
            continue
        seen.add(key)
        rank += 1
        if key in wanted:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    ranked_document_keys: Sequence[str], relevant: Sequence[str], *, k: int
) -> float:
    """Binary-gain nDCG@k over the document-level ranking.

    ``DCG = sum(1 / log2(rank + 1))`` over relevant documents inside the top
    ``k``; ``IDCG`` is the same sum for the ideal ranking, which holds
    ``min(|relevant|, k)`` relevant documents in the first positions. Returns
    ``0.0`` when ``IDCG`` is zero (no relevant documents), so a pure exclusion
    case contributes nothing rather than dividing by zero.
    """
    wanted = set(relevant)
    if not wanted:
        return 0.0
    dcg = 0.0
    for rank, key in enumerate(
        bounded_document_keys(ranked_document_keys, k=k), start=1
    ):
        if key in wanted:
            dcg += 1.0 / _log2(rank + 1)
    ideal_depth = min(len(wanted), k)
    if ideal_depth <= 0:
        return 0.0
    idcg = 0.0
    for rank in range(1, ideal_depth + 1):
        idcg += 1.0 / _log2(rank + 1)
    return dcg / idcg


def citation_resolution_rate(resolutions: Sequence[bool]) -> float:
    """Fraction of returned hits whose citation resolved through the resolver.

    ``0.0`` when nothing was returned. That is intentionally not a vacuous
    ``1.0``: a run that resolved no citations has demonstrated nothing, and the
    artifact reports the hit count beside the rate so a zero from an empty result
    set stays distinguishable from a zero from a broken resolver.
    """
    if not resolutions:
        return 0.0
    return sum(1 for resolved in resolutions if resolved) / len(resolutions)


def cross_tenant_leakage_count(
    hit_owning_tenant_ids: Sequence[str | None], *, query_tenant_id: str
) -> int:
    """Number of hits owned by neither the querying tenant nor GLOBAL.

    ``None`` ownership means a GLOBAL document, which is readable by every
    tenant and can never be leakage. Any other value that is not the querying
    tenant is a scope violation and is counted per HIT, not per document, so a
    repeated leak is visible as a repeated number.
    """
    leaked = 0
    for owning in hit_owning_tenant_ids:
        if owning is None or owning == query_tenant_id:
            continue
        leaked += 1
    return leaked


def forbidden_retrieval_count(
    ranked_document_keys: Sequence[str], forbidden: Sequence[str]
) -> int:
    """Number of returned hits whose document key is explicitly forbidden.

    Counted over the returned hits (not deduplicated): the artefact is meant to
    show how much forbidden material came back, and a document that appears three
    times came back three times.
    """
    blocked = set(forbidden)
    return sum(1 for key in ranked_document_keys if key in blocked)
