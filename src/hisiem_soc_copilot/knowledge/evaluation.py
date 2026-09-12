"""KB-GOLDEN-V1 baseline driver (brief sections 55-62).

This module is the ADAPTER between the sealed evaluation package and the
production retrieval stack. ``evaluation/knowledge`` is deliberately unable to
reach a database, a model, or a network: it receives every retrieval behaviour
through two injected async callables. Something outside it has to build those
callables, and that something is here -- around the same Application use cases
the operator CLI uses, never around a parallel path.

What that buys, concretely:

* The corpus is ingested through :class:`KnowledgeIngestionHandler`, so a baseline
  run exercises exactly the code a real ingestion does (hashing, chunking,
  embedding-profile selection, the two-phase transaction).
* Retrieval goes through ``KnowledgeRetrievalService.retrieve`` and every hit's
  citation is re-validated through ``KnowledgeCitationResolver.resolve`` -- the
  production contracts, not stand-ins for them.
* Nothing here inspects a vector, a credential, or a raw row. A hit is reduced to
  the fields the artifact is allowed to carry (section 58).

**What a run proves is not a constant of this file** (section 20). Over the
deployment's own embedding provider it measures a real retrieval system. Over the
deterministic test fixture it measures the PLUMBING -- chunking, indexing,
ranking, fusion, citation resolution -- and the vectors it uses carry no semantic
meaning at all. The two are kept apart by the artifact: the file NAME carries a
``plumbing-only`` marker, and the recorded embedding profile states which kind of
evidence the numbers are. A plumbing run is never allowed to look like a
semantic baseline.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from ..application.commands.knowledge import (
    IngestKnowledgeDocument,
    RetireKnowledgeDocument,
)
from ..application.errors import ApplicationError, KnowledgeRetrievalUnavailableError
from ..application.ports.embedding import EmbeddingProvider
from ..application.ports.knowledge import (
    CorpusChunkFact,
    KnowledgeHit,
    KnowledgeQuery,
)
from ..application.services.knowledge_retrieval import RetrievalMode
from ..bootstrap.container import Container
from ..domain.knowledge.enums import DocumentStatus, SourceKind, Visibility
from ..domain.knowledge.value_objects import (
    CHUNK_GENERATION_INITIAL,
    normalize_and_hash,
)
from ..evaluation.knowledge import (
    CASES,
    CORPUS,
    CORPUS_VERSION,
    RETIRED_DOCUMENT_KEYS,
    SUITE_ID,
    TENANTS,
    CorpusDocument,
    CorpusFact,
    CorpusMode,
    EvalMode,
    EvalQuery,
    HybridGate,
    ModeUnavailableError,
    RetrievedHit,
    build_artifact,
    corpus_identity,
    preflight_corpus,
    run_suite,
    write_artifact,
)
from .providers import (
    DETERMINISTIC_PROVIDER,
    require_embedding_provider,
    resolve_embedding_provider,
)

#: Where baselines land when the operator gives no ``--out``. Same root the other
#: evaluation runs use, in a subdirectory of its own so a knowledge artifact is
#: never confused with a GP-01 one.
ARTIFACT_SUBDIR = "knowledge"

#: Marks an artifact produced over the deterministic fixture. It goes in the FILE
#: NAME, not only inside the JSON: a file named ``...-plumbing-only.json`` cannot
#: be quoted as a semantic baseline by someone who never opened it.
PLUMBING_SUFFIX = "-plumbing-only"

#: Marks an artifact produced over a corpus the run did NOT establish was the
#: fixture (brief section 5.4). Same reasoning as :data:`PLUMBING_SUFFIX`: the
#: marker goes in the FILE NAME so the distinction survives being quoted.
OPEN_CORPUS_SUFFIX = "-open-corpus"

#: The tenant scope a retirement of a GLOBAL corpus document is issued from.
#: Retirement is not a tenant-owned act for a GLOBAL document -- any tenant scope
#: may read it, so any tenant scope may withdraw it -- and the corpus is retired
#: from a fixed, named scope rather than from whichever tenant happened to be
#: first in a list.
_RETIREMENT_SCOPE = TENANTS[0]


@dataclass(frozen=True)
class _CorpusEntry:
    """One ingested corpus document, as the harness has to describe it."""

    document_id: UUID
    document_key: str
    owning_tenant_id: str | None


async def run_knowledge_evaluation(
    container: Container,
    *,
    embedding_choice: str,
    k: int = 5,
    modes: Sequence[str] = ("LEXICAL_ONLY", "VECTOR_ONLY", "HYBRID"),
    out_dir: str | None = None,
    overwrite: bool = False,
    skip_ingest: bool = False,
    allow_embedding_profile_switch: bool = False,
    allow_ambient_corpus: bool = False,
) -> int:
    """Ingest (unless told not to), run every requested mode, write the artifact.

    Returns 0 unless the hybrid gate came back ``FAIL``. ``NOT_RUN`` is not a
    failure: it is the correct, honest verdict for a run whose vector channel had
    nothing real to compare against, and exiting non-zero for it would train
    operators to ignore the exit code.
    """
    if k < 1:
        raise ApplicationError("--k must be >= 1")
    requested = tuple(EvalMode(mode) for mode in modes)

    provider = resolve_embedding_provider(container, embedding_choice)
    plumbing_only = embedding_choice == DETERMINISTIC_PROVIDER

    if skip_ingest:
        entries = await _lookup_corpus(container)
    else:
        entries = await _ingest_corpus(
            container,
            require_embedding_provider(
                provider, what="ingesting the KB-GOLDEN-V1 corpus"
            ),
            allow_switch=allow_embedding_profile_switch,
        )
    await _retire_corpus_documents(container, entries)

    # The corpus precondition runs BEFORE a single retrieval, because a score is
    # the most dangerous possible output: a ranking measured over the wrong corpus
    # is not merely useless, it is a number someone would quote (section 5.3).
    corpus_mode = CorpusMode.OPEN_CORPUS if allow_ambient_corpus else CorpusMode.SEALED
    expected_facts = (
        () if corpus_mode is CorpusMode.OPEN_CORPUS else _expected_corpus_facts(container)
    )
    actual_facts = await _actual_corpus_facts(container)
    expected_fingerprint: str | None = None
    if corpus_mode is CorpusMode.SEALED:
        preflight_corpus(expected=expected_facts, actual=actual_facts)
        expected_fingerprint = corpus_identity(
            mode=corpus_mode, facts=expected_facts
        ).fingerprint
    identity = corpus_identity(mode=corpus_mode, facts=actual_facts)
    print(
        f"corpus: {identity.mode.value} fingerprint={identity.fingerprint[:16]} "
        f"documents={identity.eligible_document_count} "
        f"versions={identity.eligible_version_count} "
        f"chunks={identity.eligible_chunk_count}"
    )
    if corpus_mode is CorpusMode.OPEN_CORPUS:
        print(
            "  WARNING: --allow-ambient-corpus: this run measured whatever the "
            "database holds and cannot produce a sealed KB-GOLDEN-V1 baseline"
        )

    retrieval = container.knowledge_retrieval_service(embedding_provider=provider)
    resolver = container.knowledge_citation_resolver()

    async def retrieve(
        tenant_id: str, query: EvalQuery, mode: str
    ) -> Sequence[RetrievedHit]:
        try:
            result = await retrieval.retrieve(
                tenant_id=tenant_id,
                query=KnowledgeQuery(
                    topic=query.topic,
                    context_terms=query.context_terms,
                    limit=query.limit,
                ),
                mode=RetrievalMode(mode),
            )
        except KnowledgeRetrievalUnavailableError as exc:
            # ONLY "this channel cannot run" is re-labelled. Any other exception
            # propagates and fails the run loudly, because a defect must never be
            # reported as an unavailable channel (section 62).
            raise ModeUnavailableError(str(exc)) from exc
        return tuple(_retrieved_hit(hit, entries=entries) for hit in result.hits)

    async def resolve(tenant_id: str, citation_id: str) -> bool:
        resolution = await resolver.resolve(tenant_id=tenant_id, citation_id=citation_id)
        return resolution.resolved

    suite = await run_suite(
        cases=CASES,
        retrieve=retrieve,
        resolve=resolve,
        modes=requested,
        k=k,
        sealed=corpus_mode is CorpusMode.SEALED,
    )
    artifact = build_artifact(
        suite=suite,
        retrieval_profile=_retrieval_profile(container),
        embedding_profile=_embedding_profile(provider, plumbing_only=plumbing_only),
        corpus=identity.as_record(expected_fingerprint=expected_fingerprint),
    )
    written = write_artifact(
        _artifact_path(
            container,
            k=k,
            plumbing_only=plumbing_only,
            open_corpus=corpus_mode is CorpusMode.OPEN_CORPUS,
            out_dir=out_dir,
        ),
        artifact,
        overwrite=overwrite,
    )

    print(_render_summary(artifact, written, k=k, plumbing_only=plumbing_only))
    return 1 if suite.hybrid_gate == HybridGate.FAIL.value else 0


# ---------------------------------------------------------------------------
# corpus setup
# ---------------------------------------------------------------------------
async def _corpus_documents_already_retired(
    container: Container,
) -> dict[str, UUID]:
    """Corpus keys the fixture retires that are ALREADY retired in the database.

    Scoped to the keys the corpus itself retires, so an unrelated document
    found in a retired state is not silently skipped: that would be an
    unexpected corpus state, and the ingest below must fail loudly on it.
    """
    withdrawn: dict[str, UUID] = {}
    async with container.unit_of_work() as uow:
        for document in CORPUS:
            if document.document_key not in RETIRED_DOCUMENT_KEYS:
                continue
            row = await uow.knowledge_documents.find_by_external_key(
                tenant_id=document.tenant_id,
                source_kind=SourceKind(document.source_kind),
                external_key=document.document_key,
                visibility=Visibility(document.visibility),
            )
            if row is not None and row.status is DocumentStatus.RETIRED:
                withdrawn[document.document_key] = row.id
    return withdrawn


async def _ingest_corpus(
    container: Container, provider: EmbeddingProvider, *, allow_switch: bool
) -> dict[UUID, _CorpusEntry]:
    """Ingest the sealed corpus through the ordinary ingestion use case."""
    handler = container.knowledge_ingestion_handler(embedding_provider=provider)
    entries: dict[UUID, _CorpusEntry] = {}
    withdrawn = await _corpus_documents_already_retired(container)
    for index, document in enumerate(CORPUS, start=1):
        if document.document_key in withdrawn:
            # RETIRED is terminal by design (section 9): the domain refuses
            # to re-ingest a retired document, and that refusal is correct.
            # The corpus owns WHICH document is retired, so on a re-run the
            # intended state is already in place -- carry it forward and say
            # so, rather than failing after a full ingestion pass.
            document_id = withdrawn[document.document_key]
            entries[document_id] = _entry(document_id, document)
            print(
                f'  [{index}/{len(CORPUS)}] {document.document_key}: '
                'skipped (already retired by a previous run)'
            )
            continue
        outcome = await handler.ingest(
            IngestKnowledgeDocument(
                source_kind=SourceKind(document.source_kind),
                # The fixture's key IS the knowledge external key; that is what
                # lets a re-run converge instead of stacking versions.
                external_key=document.document_key,
                visibility=Visibility(document.visibility),
                title=document.title,
                content=document.content,
                tenant_id=document.tenant_id,
                language=document.language,
                source_version=document.source_version,
                metadata={
                    "suite": SUITE_ID,
                    "corpus_version": CORPUS_VERSION,
                    "chunk_labels": list(document.chunk_labels),
                },
                allow_embedding_profile_switch=allow_switch,
            )
        )
        entries[outcome.document.id] = _entry(outcome.document.id, document)
        print(
            f"  [{index}/{len(CORPUS)}] {document.document_key}: "
            f"chunks={outcome.chunk_count} new_version={outcome.version_created}"
        )
    return entries


async def _lookup_corpus(container: Container) -> dict[UUID, _CorpusEntry]:
    """Rebuild the key map from documents already in the database.

    This is what makes ``--skip-ingest`` honest: the harness re-reads the natural
    keys the corpus declares instead of trusting a map it kept in memory, so
    scoring an existing corpus neither needs a stale map nor succeeds against a
    corpus that is not actually there.
    """
    entries: dict[UUID, _CorpusEntry] = {}
    missing: list[str] = []
    async with container.unit_of_work() as uow:
        for document in CORPUS:
            row = await uow.knowledge_documents.find_by_external_key(
                tenant_id=document.tenant_id,
                source_kind=SourceKind(document.source_kind),
                external_key=document.document_key,
                visibility=Visibility(document.visibility),
            )
            if row is None:
                missing.append(document.document_key)
                continue
            entries[row.id] = _entry(row.id, document)
    if missing:
        raise ApplicationError(
            "--skip-ingest was given, but these corpus documents are not in the "
            "database: " + ", ".join(sorted(missing)) + " (drop --skip-ingest to "
            "ingest the corpus first)"
        )
    print(f"corpus: reusing {len(entries)} already-ingested documents (--skip-ingest)")
    return entries


def _entry(document_id: UUID, document: CorpusDocument) -> _CorpusEntry:
    """Describe one corpus document by its DECLARED scope.

    ``owning_tenant_id`` comes from the fixture, not from the row that was
    written. That is deliberate: the corpus declares which scope a document is
    *supposed* to live in, and the leakage metric exists to catch retrieval
    returning something outside that declaration. Reading the owner back out of
    the database would make an ingestion bug that wrote the wrong tenant look
    like correct behaviour.
    """
    return _CorpusEntry(
        document_id=document_id,
        document_key=document.document_key,
        owning_tenant_id=document.tenant_id,
    )


async def _retire_corpus_documents(
    container: Container, entries: dict[UUID, _CorpusEntry]
) -> None:
    """Retire the fixture's retired documents, idempotently.

    A retired document must stop appearing in normal search while citations taken
    before the retirement still resolve (sections 52/53). The corpus owns WHICH
    document that is, so the harness retires it on every run rather than leaving
    the state to whoever ran the suite last.
    """
    handler = container.knowledge_ingestion_handler()
    by_key = {entry.document_key: entry for entry in entries.values()}
    for key in RETIRED_DOCUMENT_KEYS:
        entry = by_key.get(key)
        if entry is None:
            raise ApplicationError(f"the corpus retires {key!r}, but it was not ingested")
        outcome = await handler.retire(
            RetireKnowledgeDocument(
                tenant_id=entry.owning_tenant_id or _RETIREMENT_SCOPE,
                document_id=entry.document_id,
                reason=f"{SUITE_ID} fixture: this document is retired by design",
            )
        )
        print(f"  retired {key} (already_retired={outcome.already_retired})")


# ---------------------------------------------------------------------------
# corpus identity (brief section 5)
# ---------------------------------------------------------------------------
def _expected_corpus_facts(container: Container) -> tuple[CorpusFact, ...]:
    """The eligible corpus the SEALED fixture declares, as reproducible facts.

    Derived with the production functions rather than restated here: the document
    body is normalized and hashed by the one domain rule, and chunked by the one
    frozen chunker bound to the configured bounds. A second implementation of
    either would let the expectation drift away from what ingestion actually
    produced, which is the only thing that makes the comparison meaningful.

    Two values are asserted rather than derived, and both are properties of a
    sealed corpus:

    * ``document_version_number`` is 1 -- the fixture's documents are the FIRST
      version of each document. A database holding a second version of a corpus
      document is not holding the sealed corpus, and failing here is how that gets
      noticed instead of being averaged into a score.
    * ``document_key`` is the external key, because that is what ingestion used
      (the fixture's key IS the knowledge external key).

    Retired corpus documents are excluded, because a retired document is not
    eligible for retrieval (section 9) -- and the harness retires exactly the
    documents the fixture names as retired.
    """
    chunker = container.knowledge_chunker()
    facts: list[CorpusFact] = []
    for document in CORPUS:
        if document.document_key in RETIRED_DOCUMENT_KEYS:
            continue
        normalized, content_hash = normalize_and_hash(document.content)
        for chunk in chunker.chunk_document(normalized):
            facts.append(
                CorpusFact(
                    visibility=document.visibility,
                    tenant_id=document.tenant_id or "",
                    source_kind=document.source_kind,
                    external_key=document.document_key,
                    document_version_number=1,
                    document_content_hash=content_hash,
                    chunk_generation=CHUNK_GENERATION_INITIAL,
                    ordinal=chunk.ordinal,
                    chunk_content_hash=chunk.content_hash,
                )
            )
    return tuple(facts)


async def _actual_corpus_facts(container: Container) -> tuple[CorpusFact, ...]:
    """The corpus the DATABASE actually holds, read through the tenant scope.

    Read once per corpus tenant and unioned. A GLOBAL document is visible to every
    tenant, so it is returned by every one of those reads; the fact set is deduped
    by :func:`corpus_identity`, which is also why the counts it reports are
    documents rather than rows.

    Every read goes through ``corpus_snapshot(tenant_id=...)``, whose scope
    predicate is applied IN SQL. A document belonging to another tenant therefore
    never enters this list, which is what makes "no cross-tenant read" a property
    of the query rather than of a Python filter.
    """
    facts: list[CorpusFact] = []
    async with container.unit_of_work() as uow:
        for tenant in TENANTS:
            snapshot = await uow.knowledge_chunks.corpus_snapshot(tenant_id=tenant)
            facts.extend(_fact_of(chunk) for chunk in snapshot.chunks)
    return tuple(facts)


def _fact_of(chunk: CorpusChunkFact) -> CorpusFact:
    """Map one persisted fact onto the evaluation boundary's plain-data form.

    The mapping is the whole point of the boundary: the sealed package describes a
    corpus with strings and integers and never learns that a ``Visibility`` or a
    ``SourceKind`` exists.
    """
    return CorpusFact(
        visibility=chunk.visibility.value,
        tenant_id=chunk.tenant_id or "",
        source_kind=chunk.source_kind.value,
        external_key=chunk.external_key,
        document_version_number=chunk.document_version_number,
        document_content_hash=chunk.document_content_hash,
        chunk_generation=chunk.chunk_generation,
        ordinal=chunk.ordinal,
        chunk_content_hash=chunk.chunk_content_hash,
    )


# ---------------------------------------------------------------------------
# artifact inputs
# ---------------------------------------------------------------------------
def _retrieved_hit(hit: KnowledgeHit, *, entries: dict[UUID, _CorpusEntry]) -> RetrievedHit:
    """Reduce one production hit to what the artifact may carry.

    An unknown ``document_id`` is neither an error nor hidden: it is reported as
    its own UUID string, which cannot match a corpus key and therefore scores as
    a miss. Inventing a corpus key there would let a document the harness never
    ingested be credited against the suite.
    """
    entry = entries.get(hit.document_id)
    return RetrievedHit(
        citation_id=hit.citation_id,
        document_key=entry.document_key if entry else str(hit.document_id),
        document_id=str(hit.document_id),
        document_version_id=str(hit.document_version_id),
        # Section 39's hit deliberately exposes no chunk ordinal, so the harness
        # reports 0 rather than reaching past the contract for one. Nothing in the
        # scoring or the artifact reads this field.
        chunk_ordinal=0,
        title=hit.title,
        excerpt=hit.excerpt,
        source_kind=hit.source_kind.value,
        language=hit.language,
        source_version=hit.source_version,
        owning_tenant_id=entry.owning_tenant_id if entry else None,
        # The retriever's own claim about its hits. The runner reports the
        # RESOLVER's answer instead and counts the disagreements, so a claim that
        # stops being true shows up as a number rather than as a silent pass.
        resolved=True,
    )


def _retrieval_profile(container: Container) -> dict[str, object]:
    """The frozen knobs the ranking ran under (section 50).

    Recorded from the same configuration object the service is built from, so the
    artifact describes the run that happened rather than a copy of the defaults.
    """
    config = container.knowledge_retrieval_config()
    return {
        "profile_id": "hybrid-v1",
        "lexical_candidate_limit": config.lexical_candidate_limit,
        "vector_candidate_limit": config.vector_candidate_limit,
        "rrf_k": config.rrf_k,
        "max_chunks_per_document": config.max_chunks_per_document,
        "chunker_version": config.chunker_version,
    }


def _embedding_profile(
    provider: EmbeddingProvider | None, *, plumbing_only: bool
) -> dict[str, object]:
    """The embedding space the run indexed and queried in, and what it proves.

    ``evidence`` is the field that stops a plumbing run being quoted as a quality
    claim: the profile identity alone cannot tell an operator whether the vectors
    meant anything, and that is exactly the distinction section 20 requires the
    artifact to carry.
    """
    if provider is None:
        return {
            "available": False,
            "evidence": "NONE",
            "detail": "no embedding provider was configured for this run",
        }
    descriptor = provider.descriptor
    return {
        "available": True,
        "provider": descriptor.provider,
        "model_id": descriptor.model_id,
        "dimension": descriptor.dimension,
        "distance_metric": descriptor.distance_metric,
        "normalization": descriptor.normalization,
        "profile_version": descriptor.profile_version,
        "evidence": "PLUMBING_ONLY" if plumbing_only else "DEPLOYMENT_CONFIGURED",
        "detail": (
            "deterministic test fixture: these vectors carry no semantic meaning, so "
            "VECTOR_ONLY and HYBRID results validate the pipeline and say nothing "
            "about retrieval quality (section 20)"
            if plumbing_only
            else "the deployment's configured embedding provider; the semantic "
            "validity of the model itself is outside what this harness can assert"
        ),
    }


def _artifact_path(
    container: Container,
    *,
    k: int,
    plumbing_only: bool,
    open_corpus: bool,
    out_dir: str | None,
) -> Path:
    """Where the artifact lands, with every caveat the run carries in its NAME.

    The two markers compose rather than override each other: a run over an ambient
    corpus with the deterministic fixture is neither a baseline nor a semantic
    measurement, and the file name has to say both.
    """
    root = (
        Path(out_dir)
        if out_dir
        else Path(container.settings.evaluation.runs_dir) / ARTIFACT_SUBDIR
    )
    markers = (PLUMBING_SUFFIX if plumbing_only else "") + (
        OPEN_CORPUS_SUFFIX if open_corpus else ""
    )
    return root / f"kb-golden-v1-k{k}{markers}.json"


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def _render_summary(
    artifact: dict[str, object], written: Path, *, k: int, plumbing_only: bool
) -> str:
    """Render a human summary from the artifact, never from the live objects.

    Reporting from the artifact is a small discipline with a large payoff: the
    screen and the evidence file cannot disagree, because one is printed from the
    other.
    """
    lines = [
        f"{SUITE_ID} (corpus {artifact['corpus_version']}) "
        f"cases={artifact['case_count']} k={k}"
    ]
    corpus = _mapping(artifact["corpus"])
    lines.append(
        f"corpus: {corpus['corpus_mode']} "
        f"fingerprint={str(corpus['corpus_fingerprint'])[:16]} "
        f"documents={corpus['eligible_document_count']} "
        f"versions={corpus['eligible_version_count']} "
        f"chunks={corpus['eligible_chunk_count']}"
    )
    if not corpus["sealed"]:
        lines.append(
            "  NOT A SEALED BASELINE: this run measured an ambient corpus, so its "
            "numbers describe the database rather than the KB-GOLDEN-V1 fixture"
        )
    embedding = _mapping(artifact["embedding_profile"])
    if plumbing_only:
        lines.append(
            "embedding: PLUMBING ONLY (deterministic-test-only) -- these vectors "
            "carry no semantic meaning"
        )
    elif not embedding["available"]:
        lines.append("embedding: not configured -- the vector channel is unavailable")
    else:
        lines.append(
            f"embedding: {embedding['provider']}/{embedding['model_id']} "
            f"dim={embedding['dimension']} {embedding['distance_metric']}"
        )

    modes = _mapping(artifact["modes"])
    for name in ("LEXICAL_ONLY", "VECTOR_ONLY", "HYBRID"):
        raw = modes.get(name)
        if raw is None:
            continue
        entry = _mapping(raw)
        if not entry["available"]:
            lines.append(f"  {name:<13} unavailable: {entry['unavailable_reason']}")
            continue
        lines.append(
            f"  {name:<13} recall@{entry['k']}={_number(entry['mean_recall_at_k'])} "
            f"mrr={_number(entry['mean_reciprocal_rank'])} "
            f"ndcg={_number(entry['mean_ndcg_at_k'])} "
            f"citation_resolution={_number(entry['citation_resolution_rate'])} "
            f"leaks={entry['cross_tenant_leakage_count']} "
            f"forbidden={entry['forbidden_retrieval_count']} "
            f"scored={entry['cases_scored']}"
        )

    gate = _mapping(artifact["hybrid_gate"])
    lines.append(f"hybrid gate: {gate['verdict']} -- {gate['detail']}")
    lines.append(f"artifact: {written}")
    return "\n".join(lines)


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ApplicationError("the evaluation artifact is malformed")
    return value


def _number(value: object) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, int | float):
        return f"{float(value):.3f}"
    return str(value)
