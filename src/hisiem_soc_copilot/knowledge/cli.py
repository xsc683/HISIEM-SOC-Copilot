"""Knowledge CLI — the operator's door to the P3-A retrieval subsystem.

Commands::

    python -m hisiem_soc_copilot.knowledge.cli doctor
    python -m hisiem_soc_copilot.knowledge.cli ingest-file --path guide.md \\
        --source-kind CURATED_GUIDANCE --external-key ssh-hardening \\
        --visibility GLOBAL --title "SSH hardening"
    python -m hisiem_soc_copilot.knowledge.cli import-attack \\
        --file attack-enterprise.json --release v15.1 --activate
    python -m hisiem_soc_copilot.knowledge.cli search --tenant tenant-a \\
        --topic T1110 --context-term ssh --mode hybrid
    python -m hisiem_soc_copilot.knowledge.cli resolve-citation \\
        --tenant tenant-a --citation kcit:<chunk>:<hash>
    python -m hisiem_soc_copilot.knowledge.cli retire \\
        --tenant tenant-a --document-id <uuid>
    python -m hisiem_soc_copilot.knowledge.cli evaluate

Every command goes through the Application use cases or the Application read
services. There is deliberately NO path here that writes the ORM directly
(section 32): a CLI that could bypass the domain would be a second, unaudited
ingestion pipeline, and the whole point of the versioning and hashing rules is
that there is exactly one.

This CLI is a DEV/EVAL tool. It is NOT registered as an Agent tool, and nothing
in the Agent can reach it (sections 4/88): the knowledge tools stay absent from
the model's selectable surface in P3-A.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from ..application.commands.knowledge import (
    ImportAttackRelease,
    IngestKnowledgeDocument,
    RetireKnowledgeDocument,
)
from ..application.errors import ApplicationError
from ..application.ports.knowledge import KnowledgeQuery, KnowledgeSearchResult
from ..application.services.knowledge_retrieval import RetrievalMode
from ..bootstrap.container import Container
from ..config import Settings
from ..domain.knowledge.enums import SourceKind, Visibility
from ..domain.knowledge.errors import KnowledgeError
from .providers import (
    DETERMINISTIC_PROVIDER,
    PROVIDER_CHOICES,
    require_embedding_provider,
    resolve_embedding_provider,
)

#: The two text formats section 32 allows. Anything else (PDF, DOCX, HTML) is
#: refused HERE rather than half-read: a parser we do not have would silently
#: ingest whatever bytes happened to decode.
_ALLOWED_SUFFIXES = (".txt", ".md")

#: CLI spelling -> retrieval mode. Spelled out rather than derived from the flag
#: string, so a renamed enum member fails type-checking instead of silently
#: producing a mode nobody asked for.
_MODES = {
    "lexical": RetrievalMode.LEXICAL_ONLY,
    "vector": RetrievalMode.VECTOR_ONLY,
    "hybrid": RetrievalMode.HYBRID,
}


# ---------------------------------------------------------------------------
# argument parsing
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m hisiem_soc_copilot.knowledge.cli",
        description="Knowledge foundation + hybrid retrieval operator CLI (P3-A).",
    )
    parser.add_argument(
        "--embedding-provider",
        choices=PROVIDER_CHOICES,
        default="configured",
        help=(
            "embedding provider to use. 'configured' uses the deployment's "
            "EMBEDDING_* settings (the production path). "
            f"'{DETERMINISTIC_PROVIDER}' is a DEV FIXTURE whose vectors carry no "
            "semantic meaning and must never back a quality claim."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="report knowledge deployment readiness")
    doctor.add_argument("--json", action="store_true", help="emit JSON")

    ingest = sub.add_parser("ingest-file", help="ingest one local .txt/.md document")
    ingest.add_argument("--path", required=True)
    ingest.add_argument("--source-kind", required=True, choices=[k.value for k in SourceKind])
    ingest.add_argument("--external-key", required=True)
    ingest.add_argument("--visibility", required=True, choices=[v.value for v in Visibility])
    ingest.add_argument("--tenant", default=None, help="required for TENANT, forbidden for GLOBAL")
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--language", default="en")
    ingest.add_argument("--source-version", default=None)
    ingest.add_argument(
        "--metadata",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="repeatable; stored on the immutable version, never interpreted",
    )
    ingest.add_argument(
        "--allow-embedding-profile-switch",
        action="store_true",
        help="explicitly permit retiring the ACTIVE embedding profile",
    )

    attack = sub.add_parser("import-attack", help="import a LOCAL MITRE ATT&CK STIX bundle")
    attack.add_argument("--file", required=True)
    attack.add_argument("--release", required=True, help="pinned release name, e.g. v15.1")
    attack.add_argument(
        "--activate",
        action="store_true",
        help="also make this the authoritative release for the canonical projection",
    )

    search = sub.add_parser("search", help="run one retrieval (dev/eval)")
    search.add_argument("--tenant", required=True)
    search.add_argument("--topic", required=True)
    search.add_argument("--context-term", action="append", default=[])
    search.add_argument("--mode", default="hybrid", choices=("lexical", "vector", "hybrid"))
    search.add_argument("--limit", type=int, default=5)
    search.add_argument("--json", action="store_true")

    resolve = sub.add_parser("resolve-citation", help="re-validate a citation handle")
    resolve.add_argument("--tenant", required=True)
    resolve.add_argument("--citation", required=True)
    resolve.add_argument("--json", action="store_true")

    retire = sub.add_parser("retire", help="retire a document so it leaves retrieval")
    retire.add_argument("--tenant", required=True)
    retire.add_argument("--document-id", required=True)
    retire.add_argument("--reason", default=None)

    evaluate = sub.add_parser(
        "evaluate", help="run the sealed KB-GOLDEN-V1 retrieval baseline"
    )
    evaluate.add_argument("--k", type=int, default=5)
    evaluate.add_argument(
        "--mode",
        action="append",
        default=[],
        choices=("LEXICAL_ONLY", "VECTOR_ONLY", "HYBRID"),
    )
    evaluate.add_argument("--out", default=None, help="artifact directory")
    evaluate.add_argument("--overwrite", action="store_true")
    evaluate.add_argument(
        "--allow-embedding-profile-switch",
        action="store_true",
        help="explicitly permit retiring the ACTIVE embedding profile",
    )
    evaluate.add_argument(
        "--skip-ingest",
        action="store_true",
        help="score an already-ingested corpus instead of ingesting it again",
    )
    return parser


def _metadata_pairs(pairs: Sequence[str]) -> dict[str, Any]:
    """Parse ``--metadata K=V`` pairs; a pair without ``=`` is a caller mistake."""
    out: dict[str, Any] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ApplicationError(f"--metadata expects KEY=VALUE, got {pair!r}")
        out[key] = value
    return out


def _require_present(args: argparse.Namespace) -> None:
    """Enforce the parts of the argument contract argparse cannot express.

    GLOBAL forbids a tenant and TENANT requires one, exactly as the domain and the
    database CHECK constraint do -- failing at the CLI boundary means the operator
    sees the mistake before anything is normalized, hashed or embedded. The
    document id is checked here for the same reason: a malformed UUID is a typo in
    an argument, not a persistence failure, and it must not reach the database or
    escape as an unhandled ``ValueError``.
    """
    if args.command == "ingest-file":
        visibility = Visibility(args.visibility)
        if visibility is Visibility.GLOBAL and args.tenant:
            raise ApplicationError("a GLOBAL document must not name a tenant")
        if visibility is Visibility.TENANT and not args.tenant:
            raise ApplicationError("a TENANT document requires --tenant")
    if args.command == "retire":
        try:
            UUID(args.document_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ApplicationError(
                f"--document-id must be a UUID, got {args.document_id!r}"
            ) from exc


# ---------------------------------------------------------------------------
# local file inputs
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Inputs:
    """Bytes read from the operator's own machine, before any async work starts.

    Reading a file is a blocking call, and the commands are async only because
    the repositories are. Pulling the reads out to the edge keeps blocking I/O
    off the event loop and, more usefully, makes a bad path fail BEFORE the
    container is opened -- the operator sees "no such file", not a connection
    error followed by a stack trace about a missing table.
    """

    content: str | None = None
    payload: bytes | None = None


def _load_inputs(args: argparse.Namespace) -> _Inputs:
    """Read the local file this command names, if it names one.

    Both format checks live here rather than in the commands because they are
    properties of the FILE, not of the use case: a ``.pdf`` has to be refused
    before its bytes are decoded into a string, and an ``.html`` bundle has to be
    refused before STIX parsing gets a chance to be lenient about it.
    """
    if args.command == "ingest-file":
        path = Path(args.path)
        if path.suffix.lower() not in _ALLOWED_SUFFIXES:
            raise ApplicationError(
                f"only {'/'.join(_ALLOWED_SUFFIXES)} documents may be ingested; "
                f"{path.name!r} is not a supported format"
            )
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        return _Inputs(content=path.read_text(encoding="utf-8"))
    if args.command == "import-attack":
        path = Path(args.file)
        if path.suffix.lower() != ".json":
            raise ApplicationError(
                "an ATT&CK import expects a local STIX 2.1 .json bundle; "
                f"{path.name!r} is not a supported format"
            )
        if not path.is_file():
            raise FileNotFoundError(f"no such file: {path}")
        return _Inputs(payload=path.read_bytes())
    return _Inputs()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
async def _cmd_doctor(container: Container, args: argparse.Namespace) -> int:
    report = await container.knowledge_diagnostics()
    if args.json:
        print(
            json.dumps(
                {
                    "overall": report.overall,
                    "database": report.database_url,
                    "checks": [
                        {"name": c.name, "status": c.status, "detail": c.detail}
                        for c in report.checks
                    ],
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(report.render())
    return 0 if report.overall != "NOT_READY" else 1


async def _cmd_ingest_file(
    container: Container, args: argparse.Namespace, inputs: _Inputs
) -> int:
    assert inputs.content is not None  # guaranteed by _load_inputs
    provider = require_embedding_provider(
        resolve_embedding_provider(container, args.embedding_provider), what="ingestion"
    )
    handler = container.knowledge_ingestion_handler(embedding_provider=provider)
    outcome = await handler.ingest(
        IngestKnowledgeDocument(
            source_kind=SourceKind(args.source_kind),
            external_key=args.external_key,
            visibility=Visibility(args.visibility),
            title=args.title,
            content=inputs.content,
            tenant_id=args.tenant,
            language=args.language,
            source_version=args.source_version,
            metadata=_metadata_pairs(args.metadata),
            allow_embedding_profile_switch=args.allow_embedding_profile_switch,
        )
    )
    print(
        f"document {outcome.document.id} version {outcome.version.version} "
        f"({outcome.version.content_hash[:12]}) chunks={outcome.chunk_count} "
        f"created_document={outcome.document_created} "
        f"created_version={outcome.version_created} "
        f"rebuilt_projection={outcome.projection_rebuilt}"
    )
    return 0


async def _cmd_import_attack(
    container: Container, args: argparse.Namespace, inputs: _Inputs
) -> int:
    assert inputs.payload is not None  # guaranteed by _load_inputs
    provider = require_embedding_provider(
        resolve_embedding_provider(container, args.embedding_provider),
        what="an ATT&CK import",
    )
    handler = container.attack_import_handler(embedding_provider=provider)
    outcome = await handler.import_release(
        ImportAttackRelease(
            release=args.release, payload=inputs.payload, activate=args.activate
        )
    )
    print(
        f"release {outcome.release}: parsed={outcome.techniques_parsed} "
        f"techniques_created={outcome.techniques_created} "
        f"documents_created={outcome.documents_created} "
        f"versions_ingested={outcome.versions_ingested} "
        f"unchanged={outcome.unchanged} skipped={len(outcome.skipped)}"
    )
    if outcome.skipped:
        # Section 35: skipping is never silent.
        print("  skipped ids: " + ", ".join(outcome.skipped[:20]))
    return 0


async def _cmd_search(container: Container, args: argparse.Namespace) -> int:
    provider = resolve_embedding_provider(container, args.embedding_provider)
    service = container.knowledge_retrieval_service(embedding_provider=provider)
    mode = _MODES[args.mode]
    try:
        result = await service.retrieve(
            tenant_id=args.tenant,
            query=KnowledgeQuery(
                topic=args.topic,
                context_terms=tuple(args.context_term),
                limit=args.limit,
            ),
            mode=mode,
        )
    except ApplicationError as exc:
        # An unavailable channel is a legitimate answer to "search", not a crash:
        # report it and exit non-zero so a script can tell the difference.
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3

    if args.json:
        print(json.dumps(_result_json(result), indent=2, sort_keys=True))
        return 0
    print(_render_result(result))
    return 0


def _result_json(result: KnowledgeSearchResult) -> dict[str, Any]:
    """JSON view of a result.

    Carries the ranked hits and the profile -- never an embedding vector, never a
    raw database row, never a credential (section 54).
    """
    profile = result.retrieval_profile
    return {
        "profile_id": result.profile_id,
        "truncated": result.truncated,
        "retrieval_profile": None
        if profile is None
        else {
            "lexical_candidate_limit": profile.lexical_candidate_limit,
            "vector_candidate_limit": profile.vector_candidate_limit,
            "rrf_k": profile.rrf_k,
            "max_chunks_per_document": profile.max_chunks_per_document,
            "chunker_version": profile.chunker_version,
            "embedding_model_id": profile.embedding_model_id,
        },
        "hits": [
            {
                "citation_id": hit.citation_id,
                "document_id": str(hit.document_id),
                "document_version_id": str(hit.document_version_id),
                "chunk_id": str(hit.chunk_id),
                "source_kind": hit.source_kind.value,
                "title": hit.title,
                "language": hit.language,
                "source_version": hit.source_version,
                "excerpt": hit.excerpt,
            }
            for hit in result.hits
        ],
    }


def _render_result(result: KnowledgeSearchResult) -> str:
    lines = [
        f"profile={result.profile_id} hits={len(result.hits)} truncated={result.truncated}"
    ]
    for rank, hit in enumerate(result.hits, start=1):
        lines.append(
            f"{rank}. [{hit.source_kind.value}] {hit.title} "
            f"({hit.document_id}) citation={hit.citation_id}"
        )
        lines.append("   " + hit.excerpt.replace("\n", " ")[:300])
    return "\n".join(lines)


async def _cmd_resolve_citation(container: Container, args: argparse.Namespace) -> int:
    resolver = container.knowledge_citation_resolver()
    resolution = await resolver.resolve(tenant_id=args.tenant, citation_id=args.citation)
    if args.json:
        print(
            json.dumps(
                {
                    "citation_id": resolution.citation_id,
                    "resolved": resolution.resolved,
                    "reason": resolution.reason,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        state = "resolved" if resolution.resolved else f"NOT resolved ({resolution.reason})"
        print(f"{resolution.citation_id}: {state}")
    return 0 if resolution.resolved else 1


async def _cmd_retire(container: Container, args: argparse.Namespace) -> int:
    # No provider is resolved for a retirement: withdrawing a document is a
    # lifecycle transition, not an embedding operation, and it must keep working
    # during an embedding outage.
    handler = container.knowledge_ingestion_handler()
    outcome = await handler.retire(
        RetireKnowledgeDocument(
            tenant_id=args.tenant,
            document_id=UUID(args.document_id),
            reason=args.reason,
        )
    )
    print(
        f"document {outcome.document.id} status={outcome.document.status.value} "
        f"already_retired={outcome.already_retired}"
    )
    return 0


async def _cmd_evaluate(container: Container, args: argparse.Namespace) -> int:
    from .evaluation import run_knowledge_evaluation

    return await run_knowledge_evaluation(
        container,
        embedding_choice=args.embedding_provider,
        k=args.k,
        modes=tuple(args.mode) or ("LEXICAL_ONLY", "VECTOR_ONLY", "HYBRID"),
        out_dir=args.out,
        overwrite=args.overwrite,
        skip_ingest=args.skip_ingest,
        allow_embedding_profile_switch=args.allow_embedding_profile_switch,
    )


#: Commands taking only ``(container, args)``. ``ingest-file`` and
#: ``import-attack`` are dispatched explicitly in ``_dispatch`` because their
#: local file has already been read by then.
_HANDLERS = {
    "doctor": _cmd_doctor,
    "search": _cmd_search,
    "resolve-citation": _cmd_resolve_citation,
    "retire": _cmd_retire,
    "evaluate": _cmd_evaluate,
}


async def _dispatch(argv: Sequence[str], settings: Settings) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv))
    _require_present(args)
    inputs = _load_inputs(args)

    container = Container(settings)
    await container.open()
    try:
        if args.command == "ingest-file":
            return await _cmd_ingest_file(container, args, inputs)
        if args.command == "import-attack":
            return await _cmd_import_attack(container, args, inputs)
        return await _HANDLERS[args.command](container, args)
    finally:
        await container.close()


def main(argv: Sequence[str] | None = None) -> int:
    from ..config import get_settings

    try:
        return asyncio.run(
            _dispatch(sys.argv[1:] if argv is None else argv, get_settings()),
            loop_factory=asyncio.SelectorEventLoop,
        )
    except (ApplicationError, KnowledgeError) as exc:
        code = getattr(exc, "code", type(exc).__name__)
        print(f"error: {code}: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
