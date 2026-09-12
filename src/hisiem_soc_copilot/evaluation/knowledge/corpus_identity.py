"""Corpus identity: the sealed-evaluation precondition (brief sections 5.3-5.6).

A baseline is only a baseline if the corpus it measured is the corpus it was
supposed to measure. Two databases, each ingested from the same sealed fixture,
must produce the same ranking, the same metrics, and the same fingerprint -- and
the way to know they did is to compare a corpus DESCRIPTION that contains no
database-generated value at all.

Every fact below is reproducible from the corpus itself: which document, which
scope, which version number, which chunk of which generation, and the content
hashes that pin the bytes. Deliberately absent are the things that would make two
identical corpora look different -- row UUIDs, timestamps, the database host, the
embedding vectors -- and the things that must never be written down at all
(credentials). The fingerprint is a SHA-256 over the canonical JSON of the sorted
fact set, so it is order-independent by construction.

Nothing here reaches a database or a model: this module is data and arithmetic,
like the rest of the package, and the caller supplies both sides (the corpus it
EXPECTED and the corpus it FOUND).
"""

from __future__ import annotations

import enum
import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

#: Schema tag mixed into the hashed bytes. Bumping it is how a future, genuinely
#: different fingerprint definition is introduced without silently reinterpreting
#: fingerprints already recorded in a baseline artifact.
CORPUS_FINGERPRINT_SCHEMA = "knowledge-corpus-fingerprint/v1"

#: The failure code a preflight reports. Named rather than implied, because the
#: operator's next action depends on telling "the corpus is not what the suite
#: expects" apart from "the retrieval stack is broken".
CORPUS_PRECONDITION_FAILED = "CORPUS_PRECONDITION_FAILED"

#: How many names one problem line may list, and how many problem lines are
#: reported. Both are bounded so a badly wrong database cannot turn the refusal
#: into an unbounded dump -- and the truncation is STATED rather than silent, so
#: a bounded message is never mistaken for a complete one.
_MAX_LISTED = 5
_MAX_PROBLEMS = 8


class CorpusMode(enum.StrEnum):
    """Whether a run asserted its corpus or accepted whatever was there.

    ``OPEN_CORPUS`` is not a lesser SEALED: it is a different measurement. It can
    say what the database currently returns, and it can never say "this is the
    KB-GOLDEN-V1 baseline", because nothing about the run establishes that the
    corpus was the fixture.
    """

    SEALED = "SEALED"
    OPEN_CORPUS = "OPEN_CORPUS"


@dataclass(frozen=True, order=True)
class CorpusFact:
    """One eligible corpus chunk, reduced to reproducible semantic facts.

    ``tenant_id`` is the empty string for a GLOBAL document rather than ``None``,
    so the canonical form stays total and ``order=True`` gives a real total order.
    That is the same convention the retrieval stable-ranking key uses, and for the
    same reason: the scope is already carried by ``visibility``, so the empty
    string loses nothing and makes the sort explainable.

    ``order=True`` makes the dataclass's own field order the semantic ranking key,
    which is deliberate -- the field order IS the order a reader would want:
    scope, then document, then version, then chunk.
    """

    visibility: str
    tenant_id: str
    source_kind: str
    external_key: str
    document_version_number: int
    document_content_hash: str
    chunk_generation: int
    ordinal: int
    chunk_content_hash: str


def corpus_fingerprint(facts: Iterable[CorpusFact]) -> str:
    """Return the lowercase hex SHA-256 fingerprint of one corpus.

    ``facts`` may arrive in any order and with duplicates (a GLOBAL document is
    visible to every tenant scope, so a caller that unions per-tenant snapshots
    sees it more than once); the result depends only on the SET of facts.
    """
    document = {
        "schema": CORPUS_FINGERPRINT_SCHEMA,
        "chunks": [
            {
                "visibility": fact.visibility,
                "tenant_id": fact.tenant_id,
                "source_kind": fact.source_kind,
                "external_key": fact.external_key,
                "document_version_number": fact.document_version_number,
                "document_content_hash": fact.document_content_hash,
                "chunk_generation": fact.chunk_generation,
                "ordinal": fact.ordinal,
                "chunk_content_hash": fact.chunk_content_hash,
            }
            for fact in sorted(set(facts))
        ],
    }
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CorpusIdentity:
    """What a corpus run measured, in the form the artifact records.

    The counts are derived from the fact SET, not from a row count: a GLOBAL
    document visible to three tenants is one document with three copies of the
    same fact, and counting rows would report three.
    """

    mode: CorpusMode
    fingerprint: str
    eligible_document_count: int
    eligible_version_count: int
    eligible_chunk_count: int

    def as_record(self, *, expected_fingerprint: str | None) -> dict[str, object]:
        """Render the artifact fields (section 5.5).

        ``expected_fingerprint`` is the fingerprint the run ASSERTED, and is
        ``None`` under ``OPEN_CORPUS`` because such a run asserts nothing. Keeping
        the expected and the actual fingerprint side by side is what lets a reader
        of the artifact tell a sealed run from an ambient one without reading the
        file name.
        """
        return {
            "corpus_mode": self.mode.value,
            "sealed": self.mode is CorpusMode.SEALED,
            "corpus_fingerprint": self.fingerprint,
            "expected_corpus_fingerprint": expected_fingerprint,
            "eligible_document_count": self.eligible_document_count,
            "eligible_version_count": self.eligible_version_count,
            "eligible_chunk_count": self.eligible_chunk_count,
        }


def corpus_identity(*, mode: CorpusMode, facts: Iterable[CorpusFact]) -> CorpusIdentity:
    """Build the identity of one corpus from its facts."""
    ordered = sorted(set(facts))
    return CorpusIdentity(
        mode=mode,
        fingerprint=corpus_fingerprint(ordered),
        eligible_document_count=len(
            {
                (fact.visibility, fact.tenant_id, fact.source_kind, fact.external_key)
                for fact in ordered
            }
        ),
        eligible_version_count=len(
            {
                (
                    fact.visibility,
                    fact.tenant_id,
                    fact.source_kind,
                    fact.external_key,
                    fact.document_version_number,
                )
                for fact in ordered
            }
        ),
        eligible_chunk_count=len(ordered),
    )


class CorpusPreconditionError(RuntimeError):
    """Raised when the corpus is not the one the suite is scored against.

    The message always begins with the code :data:`CORPUS_PRECONDITION_FAILED`, so
    an operator (or a log search) can key on it without parsing prose. The reasons
    are kept as data as well, so a caller can render or record them separately.
    """

    code = CORPUS_PRECONDITION_FAILED

    def __init__(self, reasons: Sequence[str]) -> None:
        self.reasons: tuple[str, ...] = tuple(reasons)
        super().__init__(self.code + ": " + "; ".join(self.reasons))


def preflight_corpus(
    *, expected: Sequence[CorpusFact], actual: Sequence[CorpusFact]
) -> None:
    """Fail closed unless ``actual`` IS the sealed corpus ``expected``.

    Runs BEFORE any metric, so a wrong or mutated corpus is reported as a
    precondition failure rather than as a plausible-looking score. A score is the
    most dangerous possible output here: a ranking measured over the wrong corpus
    is not merely useless, it is a number someone would quote.
    """
    problems = corpus_problems(expected=expected, actual=actual)
    if problems:
        raise CorpusPreconditionError(problems)


def corpus_problems(
    *, expected: Sequence[CorpusFact], actual: Sequence[CorpusFact]
) -> list[str]:
    """Return every way ``actual`` differs from ``expected``, in sorted order.

    Separated from :func:`preflight_corpus` so a caller that wants to REPORT the
    differences (a dry run, a diagnostic) does not have to raise to obtain them.
    """
    expected_documents = _by_document(expected)
    actual_documents = _by_document(actual)
    problems: list[str] = []

    expected_keys = set(expected_documents)
    actual_keys = set(actual_documents)

    unexpected = sorted(actual_keys - expected_keys)
    if unexpected:
        problems.append(
            "unexpected eligible document(s): "
            + _listed([_render_key(key) for key in unexpected])
        )
    missing = sorted(expected_keys - actual_keys)
    if missing:
        problems.append(
            "expected document(s) missing or not eligible: "
            + _listed([_render_key(key) for key in missing])
        )

    for key in sorted(expected_keys & actual_keys):
        want = expected_documents[key]
        got = actual_documents[key]
        want_version = {(fact.document_version_number, fact.document_content_hash) for fact in want}
        got_version = {(fact.document_version_number, fact.document_content_hash) for fact in got}
        if want_version != got_version:
            problems.append(
                f"{_render_key(key)}: document version/content hash mismatch "
                f"(expected version {_version_of(want)}, found {_version_of(got)})"
            )
        want_chunks = _chunk_shape(want)
        got_chunks = _chunk_shape(got)
        if want_chunks != got_chunks:
            problems.append(
                f"{_render_key(key)}: chunk projection mismatch "
                f"(expected {len(want)} content chunk(s) in "
                f"{_generations(want)}, found {len(got)} in {_generations(got)})"
            )
        if len(problems) >= _MAX_PROBLEMS:
            break

    if not problems:
        return []
    if len(problems) >= _MAX_PROBLEMS:
        problems.append(
            f"further differences were not listed (report capped at {_MAX_PROBLEMS})"
        )
    return problems


def _by_document(
    facts: Sequence[CorpusFact],
) -> dict[tuple[str, str, str, str], tuple[CorpusFact, ...]]:
    grouped: dict[tuple[str, str, str, str], list[CorpusFact]] = {}
    for fact in sorted(set(facts)):
        grouped.setdefault(
            (fact.visibility, fact.tenant_id, fact.source_kind, fact.external_key), []
        ).append(fact)
    return {key: tuple(value) for key, value in grouped.items()}


def _chunk_shape(facts: Sequence[CorpusFact]) -> frozenset[tuple[int, int, str]]:
    return frozenset(
        (fact.chunk_generation, fact.ordinal, fact.chunk_content_hash) for fact in facts
    )


def _version_of(facts: Sequence[CorpusFact]) -> str:
    return ", ".join(sorted({str(fact.document_version_number) for fact in facts}))


def _generations(facts: Sequence[CorpusFact]) -> str:
    return ", ".join(sorted({str(fact.chunk_generation) for fact in facts}))


def _render_key(key: tuple[str, str, str, str]) -> str:
    """Render one document key for an operator without inventing a scope.

    A GLOBAL document has no tenant, and printing an empty tenant as ``''`` would
    read as a mistake; printing ``-`` would read as a value. The scope is spelled
    out instead, because the whole point of the check is that scope is part of the
    corpus identity.
    """
    visibility, tenant_id, source_kind, external_key = key
    scope = f"tenant {tenant_id}" if tenant_id else "GLOBAL"
    return f"{source_kind}:{external_key} ({scope}, {visibility})"


def _listed(names: Sequence[str]) -> str:
    shown = ", ".join(names[:_MAX_LISTED])
    if len(names) > _MAX_LISTED:
        shown += f", and {len(names) - _MAX_LISTED} more"
    return shown
