"""Architecture boundaries for the knowledge bounded context (brief sections 63-69, 88).

Four one-way rules, each enforced over parsed ASTs. No module under test is
imported, with one deliberate exception the brief itself asks for: the section-88
lock is a BEHAVIOURAL contract on the tool registry, and it is called out where it
happens.

1. ``domain.knowledge`` is pure domain: stdlib plus sibling ``domain`` modules.
   A driver (``sqlalchemy``, ``pgvector``, ...) can never appear in the aggregate.
2. The knowledge ports/handlers/services in ``application`` never reach into
   ``infrastructure`` and never import a driver.
3. Raw SQL and pgvector queries stay in ``infrastructure``: no file under
   ``application/`` or ``domain/knowledge/`` names ``AsyncSession`` or
   ``cosine_distance``, calls ``text(...)``/``select(...)``, calls
   ``session.execute(...)``, or carries the ``<=>`` operator. The SAME scanner is
   then pointed at the infrastructure repository as a positive control, so a
   scanner that had silently stopped matching could not make this pass.
4. Section 88's lock: the model-selectable tool surface is EXACTLY the pre-P3-A
   set, knowledge lookup stays catalog-only, the knowledge CLI is unreachable from
   the agent, and only the non-production ``knowledge``/``evaluation_harness``
   packages may see the evaluation oracle.
5. The P3-A closure's own guarantees, each asserted where it could be undone: the
   ATT&CK import path cannot reach the network, tenant scope is a mandatory
   keyword on every retrieval entry point, the sealed evaluation package imports
   nothing ambient, a citation names immutable content rather than a rebuildable
   projection row, and the legacy embedding-profile switch flag has no executable
   path behind it.
"""

from __future__ import annotations

import ast
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from hisiem_soc_copilot.application.ports.clock import SystemClock

SRC = Path(__file__).resolve().parent.parent.parent / "src"
PKG = SRC / "hisiem_soc_copilot"

DOMAIN_KNOWLEDGE_DIR = PKG / "domain" / "knowledge"
APPLICATION_DIR = PKG / "application"
AGENT_DIR = PKG / "agent"

PACKAGE_ROOT = "hisiem_soc_copilot"

# Production layers (brief section 20). ``knowledge`` is deliberately NOT one: it
# is the operator/evaluation CLI surface, which is why it is allowed to reach the
# evaluation oracle -- see ``ALLOWED_EVALUATION_IMPORTERS``.
PRODUCTION_LAYERS = frozenset(
    {"domain", "application", "agent", "api", "infrastructure", "bootstrap"}
)

# Third-party packages the knowledge bounded context must never depend on.
FORBIDDEN_THIRD_PARTY = frozenset(
    {
        "alembic",
        "fastapi",
        "httpx",
        "langgraph",
        "numpy",
        "openai",
        "pgvector",
        "pydantic",
        "sqlalchemy",
    }
)

# Outer/inner copilot packages the domain and application halves must not reach.
FORBIDDEN_COPILOT_PACKAGES = frozenset(
    {"agent", "api", "application", "bootstrap", "evaluation", "infrastructure", "knowledge"}
)

# The knowledge modules the application boundary rule is asserted over.
APPLICATION_KNOWLEDGE_MODULES = (
    "ports/knowledge.py",
    "ports/embedding.py",
    "ports/attack.py",
    "handlers/knowledge.py",
    "handlers/attack_import.py",
    "services/knowledge_retrieval.py",
    "services/attack_projection.py",
)

# AST-visible spellings of "raw SQL lives here".
_SQL_TYPE_NAMES = frozenset({"AsyncSession", "Connection", "cosine_distance"})
_SQL_CALL_NAMES = frozenset({"text", "select"})
_SQL_STRING_MARKERS = ("<=>", "cosine_distance", "AsyncSession", "pgvector")

# The infrastructure module that legitimately owns the SQL, used as the positive
# control for the scanner above.
_INFRASTRUCTURE_SQL_MODULE = "infrastructure/persistence/repositories/knowledge.py"

# ---------------------------------------------------------------------------
# section 88 -- pinned LITERALLY, never computed from the code under test
# ---------------------------------------------------------------------------

# The pre-P3-A model-selectable surface: exactly the tools the executor implements.
EXPECTED_MODEL_SELECTABLE = frozenset({"hisiem.get_detection_rule", "hisiem.search_events"})

# Catalog-only documentation entries. Knowledge lookup is spec'd here and stays
# here: P3-A added a retrieval SERVICE, not a model-selectable TOOL.
EXPECTED_FUTURE_CATALOG = frozenset(
    {
        "hisiem.get_entity_activity",
        "threat_intel.lookup_ip",
        "knowledge.retrieve_security_guidance",
        "knowledge.resolve_attack_technique",
    }
)

EXPECTED_SYSTEM_CONTROLLED = "hisiem.get_alert_context"

# Top-level packages that may import ``hisiem_soc_copilot.evaluation`` and are NOT
# production layers, pinned rather than discovered:
#   knowledge          -- the knowledge CLI/evaluation DRIVER. It is a dev/eval
#                         surface (it needs a provider, a container and a corpus),
#                         and it reads the knowledge scenario set the oracle owns.
#   evaluation_harness -- the harness sibling that bridges a SealedManifest to a
#                         container. It is the eval instrument, not a layer the
#                         production runtime can reach.
# Neither is importable from domain/application/agent/api/infrastructure/bootstrap,
# which is asserted separately above.
ALLOWED_EVALUATION_IMPORTERS = frozenset({"evaluation_harness", "knowledge"})


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _python_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.rglob("*.py"))


def _owner(path: Path) -> str:
    """Top-level package under ``hisiem_soc_copilot`` owning ``path`` ('' at root)."""
    parts = path.relative_to(PKG).parts[:-1]
    return parts[0] if parts else ""


def _resolve(path: Path, node: ast.ImportFrom) -> str:
    """Resolve an ImportFrom against the importing file's package.

    ``level`` counts leading dots; each dot beyond the first climbs one package.
    An absolute import (level 0) is already resolved.
    """
    if not node.level:
        return node.module or ""
    parts = [p for p in path.relative_to(PKG).parts[:-1] if p]
    for _ in range(node.level - 1):
        if parts:
            parts.pop()
    name = [PACKAGE_ROOT, *parts]
    if node.module:
        name.append(node.module)
    return ".".join(name)


def _absolute_imports(path: Path) -> list[str]:
    """Every absolute module path ``path`` imports, relative imports resolved."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve(path, node)
            if resolved:
                targets.append(resolved)
    return targets


def _reaches(target: str, package: str) -> bool:
    """True when ``target`` is ``package`` itself or a module beneath it."""
    prefix = f"{PACKAGE_ROOT}.{package}"
    return target == prefix or target.startswith(f"{prefix}.")


def _statement_string_ids(tree: ast.Module) -> set[int]:
    """``id()`` of every string constant that sits in STATEMENT position.

    Those are docstrings, not data. ``domain/knowledge/entities.py`` explains the
    scope rule with the words ``GLOBAL <=> no tenant``, and
    ``application/ports/unit_of_work.py`` documents that handlers never touch an
    ``AsyncSession``; both are prose about the boundary, not a violation of it.
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _attribute_base(func: ast.expr) -> str:
    """Render the receiver of a method call as a dotted name (best effort)."""
    if not isinstance(func, ast.Attribute):
        return ""
    base = func.value
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return f"{_attribute_base(base)}.{base.attr}"
    return ""


def _sql_offences(tree: ast.Module) -> list[str]:
    """AST-visible raw-SQL / pgvector spellings inside one module."""
    docstrings = _statement_string_ids(tree)
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _SQL_TYPE_NAMES:
            offenders.append(f"name {node.id!r} at line {node.lineno}")
        elif isinstance(node, ast.Attribute) and node.attr in _SQL_TYPE_NAMES:
            offenders.append(f"attribute {node.attr!r} at line {node.lineno}")
        elif isinstance(node, ast.Call):
            name = _call_name(node.func)
            if name in _SQL_CALL_NAMES:
                offenders.append(f"call {name}(...) at line {node.lineno}")
            elif name == "execute" and "session" in _attribute_base(node.func).lower():
                offenders.append(f"call {_attribute_base(node.func)}.execute(...)")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            for marker in _SQL_STRING_MARKERS:
                if marker in node.value:
                    offenders.append(f"string {marker!r} at line {node.lineno}")
    return offenders


def _evaluation_importers() -> dict[str, list[str]]:
    """owner package -> modules that import ``hisiem_soc_copilot.evaluation``."""
    importers: dict[str, list[str]] = {}
    for path in _python_files(PKG):
        owner = _owner(path)
        if not owner or owner == "evaluation":
            continue  # the package importing its own submodules is not a boundary
        for target in _absolute_imports(path):
            if _reaches(target, "evaluation"):
                importers.setdefault(owner, []).append(str(path.relative_to(PKG)))
    return importers


# ---------------------------------------------------------------------------
# 1. domain.knowledge is pure domain
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", _python_files(DOMAIN_KNOWLEDGE_DIR), ids=lambda p: p.name)
def test_domain_knowledge_imports_only_stdlib_and_domain(path: Path) -> None:
    offenders: list[str] = []
    for target in _absolute_imports(path):
        top = target.split(".")[0]
        if top in sys.stdlib_module_names:
            continue
        if _reaches(target, "domain"):
            continue
        offenders.append(target)
    assert not offenders, (
        f"{path.name} escapes the domain layer by importing {offenders}; "
        "domain.knowledge may use the stdlib and sibling domain modules only"
    )


@pytest.mark.parametrize("path", _python_files(DOMAIN_KNOWLEDGE_DIR), ids=lambda p: p.name)
def test_domain_knowledge_imports_no_driver_or_outer_layer(path: Path) -> None:
    """The named blacklist, asserted separately so a failure names the risk."""
    offenders: list[str] = []
    for target in _absolute_imports(path):
        top = target.split(".")[0]
        if top in FORBIDDEN_THIRD_PARTY:
            offenders.append(target)
            continue
        for package in FORBIDDEN_COPILOT_PACKAGES:
            if _reaches(target, package):
                offenders.append(target)
    assert not offenders, (
        f"{path.name} imports {offenders}; the knowledge aggregate must not reach "
        "a driver or an outer layer"
    )


def test_domain_knowledge_directory_is_actually_scanned() -> None:
    """Guard: an empty/renamed directory must not make the rules above vacuous."""
    assert [path.name for path in _python_files(DOMAIN_KNOWLEDGE_DIR)] == [
        "__init__.py",
        "entities.py",
        "enums.py",
        "errors.py",
        "events.py",
        "value_objects.py",
    ]


# ---------------------------------------------------------------------------
# 2. the application knowledge modules stay inside application
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("relative", APPLICATION_KNOWLEDGE_MODULES)
def test_application_knowledge_module_imports_no_infrastructure(relative: str) -> None:
    path = APPLICATION_DIR / relative
    assert path.is_file(), f"{relative} moved; update the boundary test with it"

    offenders: list[str] = []
    for target in _absolute_imports(path):
        top = target.split(".")[0]
        if top in FORBIDDEN_THIRD_PARTY or _reaches(target, "infrastructure"):
            offenders.append(target)
    assert not offenders, (
        f"application/{relative} imports {offenders}; ports, handlers and services "
        "speak in protocols, never in drivers"
    )


def test_the_application_knowledge_module_list_is_not_stale() -> None:
    """Guard: every listed module exists, so the rule above is not vacuous."""
    missing = [
        relative
        for relative in APPLICATION_KNOWLEDGE_MODULES
        if not (APPLICATION_DIR / relative).is_file()
    ]
    assert missing == [], f"listed knowledge modules no longer exist: {missing}"


# ---------------------------------------------------------------------------
# 3. raw SQL and pgvector queries stay in infrastructure
# ---------------------------------------------------------------------------


def test_no_raw_sql_or_vector_query_leaks_into_application_or_domain() -> None:
    """AST/attribute inspection only -- the tokens are matched as NODES, not text.

    A text scan cannot be used here: ``build_search_text(`` and
    ``_require_bounded_text(`` both contain the literal ``text(`` while being
    ordinary Python calls, so a substring search would report false positives.
    That is why this walks the tree instead.
    """
    offenders: list[str] = []
    for path in _python_files(APPLICATION_DIR) + _python_files(DOMAIN_KNOWLEDGE_DIR):
        for offence in _sql_offences(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(f"{path.relative_to(PKG)}: {offence}")
    assert not offenders, "SQL and vector operators must stay in infrastructure:\n" + "\n".join(
        offenders
    )


def test_the_sql_scanner_can_actually_see_sql() -> None:
    """Positive control: the scanner must FIND the SQL in the module that owns it.

    Without this, a scanner that had stopped matching (a renamed node type, a
    changed walker) would report "no offenders" and the rule above would pass
    while enforcing nothing. Each matcher family is exercised here -- the type
    name, the method call, the bare ``select(...)`` call, and the pgvector
    distance attribute.
    """
    path = PKG / _INFRASTRUCTURE_SQL_MODULE
    offences = _sql_offences(ast.parse(path.read_text(encoding="utf-8")))

    assert any("AsyncSession" in offence for offence in offences), offences
    assert any(".execute(" in offence for offence in offences), offences
    assert any("call select(" in offence for offence in offences), offences
    assert any("cosine_distance" in offence for offence in offences), offences


# ---------------------------------------------------------------------------
# 4a. section 88 -- the model-selectable surface is unchanged by P3-A
# ---------------------------------------------------------------------------


def test_the_registry_model_selectable_set_is_the_pre_p3a_surface() -> None:
    """Behavioural contract -- the one place this file imports a module under test."""
    from hisiem_soc_copilot.agent.tools.registry import (
        AGENT_SELECTABLE_TOOLS,
        FUTURE_CATALOG_TOOLS,
        SYSTEM_CONTROLLED_TOOL,
        ToolRegistry,
    )

    registry = ToolRegistry()

    assert set(registry.model_selectable_names) == EXPECTED_MODEL_SELECTABLE, (
        "P3-A added retrieval, not a tool: knowledge must not become model-selectable"
    )
    assert set(AGENT_SELECTABLE_TOOLS) == EXPECTED_MODEL_SELECTABLE
    assert SYSTEM_CONTROLLED_TOOL == EXPECTED_SYSTEM_CONTROLLED
    assert SYSTEM_CONTROLLED_TOOL not in registry.model_selectable_names

    assert FUTURE_CATALOG_TOOLS == EXPECTED_FUTURE_CATALOG
    assert "knowledge.retrieve_security_guidance" in FUTURE_CATALOG_TOOLS
    assert "knowledge.resolve_attack_technique" in FUTURE_CATALOG_TOOLS

    # Catalog-only means exactly that: no executor, no registration, no selection.
    for name in FUTURE_CATALOG_TOOLS:
        assert not registry.is_registered(name)
        assert name not in registry.model_selectable_names

    assert [
        name for name in registry.model_selectable_names if name.startswith("knowledge.")
    ] == []


# ---------------------------------------------------------------------------
# 4b. the knowledge CLI is unreachable from the agent
# ---------------------------------------------------------------------------


def test_the_agent_never_imports_the_knowledge_cli() -> None:
    """The agent runtime must not reach the operator/eval knowledge surface."""
    offenders: list[str] = []
    for path in _python_files(AGENT_DIR):
        for target in _absolute_imports(path):
            if _reaches(target, "knowledge"):
                offenders.append(f"{path.relative_to(PKG)} imports {target!r}")
    assert not offenders, (
        "the knowledge CLI is a dev/eval surface; the agent must not import it:\n"
        + "\n".join(offenders)
    )


def test_the_agent_directory_is_actually_scanned() -> None:
    """Guard: the rule above must be running over real files."""
    assert len(_python_files(AGENT_DIR)) > 5


# ---------------------------------------------------------------------------
# 4c. the evaluation boundary, with its exception made explicit
# ---------------------------------------------------------------------------


def test_no_production_layer_imports_the_evaluation_oracle() -> None:
    offenders = {
        owner: files
        for owner, files in _evaluation_importers().items()
        if owner in PRODUCTION_LAYERS
    }
    assert not offenders, (
        f"evaluation oracle/control data must never reach a production layer: {offenders}"
    )


def test_only_the_non_production_knowledge_and_harness_see_evaluation() -> None:
    """The exception is pinned, not accidental.

    ``hisiem_soc_copilot.knowledge`` imports ``hisiem_soc_copilot.evaluation.
    knowledge`` on purpose: the knowledge CLI is a dev/eval surface (it needs a
    provider, a container and a seeded corpus), not a layer the production runtime
    can reach. ``evaluation_harness`` is the eval instrument that bridges a
    SealedManifest to a container. Nothing else may join this list without editing
    this test, which is the point.
    """
    importers = _evaluation_importers()

    assert set(importers) == ALLOWED_EVALUATION_IMPORTERS, (
        f"evaluation importers changed: {sorted(importers)}"
    )
    # And the exception is REAL -- otherwise the allow-list is dead code that
    # would hide a later removal of the boundary it documents.
    assert "knowledge" in importers


# ---------------------------------------------------------------------------
# 5. P3-A closure guards (brief section 11)
#
# Each one pins a property the closure created, in the place where it could be
# quietly undone: the ATT&CK import port, the retrieval entry points, the sealed
# evaluation package, the citation target, and the embedding-profile switch.
# ---------------------------------------------------------------------------

ATTACK_PORT = PKG / "application" / "ports" / "attack.py"
RETRIEVAL_SERVICE = PKG / "application" / "services" / "knowledge_retrieval.py"
EVALUATION_KNOWLEDGE_DIR = PKG / "evaluation" / "knowledge"

#: Every module a runtime network fetch would arrive through. Pinned rather than
#: discovered, because the promise is "this port cannot reach the network" and a
#: new spelling of that capability is exactly what the guard exists to catch.
NETWORK_MODULES = frozenset(
    {
        "aiohttp",
        "ftplib",
        "http",
        "httpx",
        "requests",
        "socket",
        "ssl",
        "urllib",
        "urllib3",
        "websockets",
    }
)

#: The ONLY stdlib the sealed-evaluation package may touch: serialization and
#: pure data structures. No clock, no randomness, no host, no driver -- which is
#: what makes a sealed run reproducible from the corpus facts alone.
#: ``__future__`` is a compiler directive rather than an import that runs, so it
#: is listed beside them rather than treated as an exception.
EVALUATION_ALLOWED_STDLIB = frozenset(
    {"__future__", "collections", "dataclasses", "enum", "hashlib", "json", "pathlib", "typing"}
)


def _functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ]


def _imported_modules(tree: ast.Module) -> list[tuple[str, int]]:
    """Every top-level module name imported by ``tree``, with its line."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            found.append((node.module, node.lineno))
    return found


def test_the_attack_import_port_cannot_reach_the_network() -> None:
    """Section 33: the bundle is pinned locally; the port has no fetch capability.

    Asserted on the AST rather than on behaviour, because the risk is a future
    ADDITION (a ``fetch(url)`` method, an injected client) rather than a bug in
    today's code. A port with no URL literal and no network import cannot be
    asked to download anything, which is a stronger promise than "it currently
    does not".
    """
    tree = ast.parse(ATTACK_PORT.read_text(encoding="utf-8"))
    offenders: list[str] = []

    for module, line in _imported_modules(tree):
        if module.split(".")[0] in NETWORK_MODULES:
            offenders.append(f"imports {module} at line {line}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "http://" in node.value or "https://" in node.value:
                offenders.append(f"URL literal at line {node.lineno}")
        elif isinstance(node, ast.Name) and node.id.lower() in {"urlopen", "urlretrieve"}:
            offenders.append(f"name {node.id!r} at line {node.lineno}")

    assert not offenders, (
        "the ATT&CK import port must have no network capability: " + "; ".join(offenders)
    )
    # Positive control: the port really was parsed and really does declare the
    # protocol this rule is about.
    names = {node.name for node in _functions(tree)}
    classes = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    assert "parse" in names and "AttackBundleParser" in classes, (sorted(names), sorted(classes))


def test_no_network_import_survives_anywhere_in_the_attack_path() -> None:
    """The port is not the only place a download could hide: the parser counts too."""
    offenders: list[str] = []
    for path in _python_files(PKG / "infrastructure") + _python_files(PKG / "application"):
        target = str(path.relative_to(PKG))
        if "attack" not in target:
            continue
        for module, line in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            if module.split(".")[0] in NETWORK_MODULES:
                offenders.append(f"{target}:{line}: imports {module}")
    assert not offenders, "ATT&CK parsing is offline by construction: " + "; ".join(offenders)


def test_ordinary_retrieval_requires_a_mandatory_tenant_id_keyword() -> None:
    """Section 40: tenant scope is an argument, never an ambient default.

    Every ``tenant_id`` parameter in the retrieval service must be keyword-only
    and must have NO default. A positional or defaulted scope is how a caller
    silently reads another tenant's knowledge, so the SHAPE of the signature is
    the guard -- not a runtime check a future overload could bypass.
    """
    tree = ast.parse(RETRIEVAL_SERVICE.read_text(encoding="utf-8"))
    seen: set[str] = set()
    offenders: list[str] = []

    for node in _functions(tree):
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        if not any(arg.arg == "tenant_id" for arg in [*positional, *args.kwonlyargs]):
            continue
        seen.add(node.name)
        if any(arg.arg == "tenant_id" for arg in positional):
            offenders.append(f"{node.name}: tenant_id is positional")
            continue
        index = [arg.arg for arg in args.kwonlyargs].index("tenant_id")
        if args.kw_defaults[index] is not None:
            offenders.append(f"{node.name}: tenant_id has a default")

    assert not offenders, "tenant scope must be a required keyword: " + "; ".join(offenders)
    # And the guard really did look at the entry points it names.
    assert {"retrieve", "resolve"} <= seen, sorted(seen)


def test_the_sealed_evaluation_package_takes_no_ambient_input() -> None:
    """Sections 5.5/5.6: no clock, no randomness, no host, no driver.

    A reproducible baseline is a claim about the WHOLE package, not about the
    ranking function alone: one ``uuid4()`` in the artifact writer is enough to
    make two runs over one corpus disagree. So the rule is an allow-list of
    imports, and the list is deliberately short.
    """
    offenders: list[str] = []
    for path in _python_files(EVALUATION_KNOWLEDGE_DIR):
        for module, line in _imported_modules(ast.parse(path.read_text(encoding="utf-8"))):
            top = module.split(".")[0]
            if top not in sys.stdlib_module_names:
                offenders.append(f"{path.name}:{line}: imports non-stdlib {module}")
            elif top not in EVALUATION_ALLOWED_STDLIB:
                offenders.append(f"{path.name}:{line}: imports {module}")

    assert not offenders, (
        "the sealed evaluation package must be a pure function of its corpus: "
        + "; ".join(offenders)
    )


def test_the_citation_target_is_immutable_content_not_a_projection_row() -> None:
    """Section 3.1: the identity that survives a reindex is CONTENT identity.

    Two halves, because either alone is escapable: the domain formats a citation
    from a content hash (so the handle cannot encode a disposable row id), and
    the rebuildable projection row carries no content and no hash column (so a
    citation can never be re-pointed at one).
    """
    from uuid import UUID

    from hisiem_soc_copilot.domain.knowledge.value_objects import (
        compute_content_hash,
        format_citation_id,
        parse_citation_id,
    )
    from hisiem_soc_copilot.infrastructure.persistence.orm.knowledge import (
        KnowledgeChunkEmbeddingRow,
        KnowledgeContentChunkRow,
    )

    content = "Repeated failed logons from one source address."
    digest = compute_content_hash(content)
    citation = format_citation_id(
        chunk_id=UUID("00000000-0000-4000-8000-0000000000aa"), content_hash=digest
    )
    parsed = parse_citation_id(citation)
    assert parsed is not None
    assert digest.startswith(parsed.content_hash_prefix)

    content_columns = set(KnowledgeContentChunkRow.__table__.columns.keys())
    embedding_columns = set(KnowledgeChunkEmbeddingRow.__table__.columns.keys())

    assert {"content", "content_hash"} <= content_columns
    # The projection is exactly that: a vector plus the identity of the chunk it
    # projects. Nothing about it is citable, so rebuilding it cannot move a
    # citation's target.
    assert "content" not in embedding_columns
    assert "content_hash" not in embedding_columns
    assert "content_chunk_id" in embedding_columns


def test_no_per_document_embedding_profile_switch_path_is_executable() -> None:
    """Section 4.1: the legacy flag is a DIAGNOSIS, not a switch.

    Behavioural, and built so the failure mode is specific: the UoW factory
    raises if it is ever called, so a handler that opened a transaction and only
    THEN refused the switch fails here instead of passing by raising the right
    exception a little too late.
    """
    from hisiem_soc_copilot.application.commands.knowledge import IngestKnowledgeDocument
    from hisiem_soc_copilot.application.errors import (
        EmbeddingProfileSwitchRequiresReindexError,
    )
    from hisiem_soc_copilot.application.handlers.knowledge import KnowledgeIngestionHandler
    from hisiem_soc_copilot.application.ports.embedding import (
        EmbeddingBatch,
        EmbeddingProfileDescriptor,
    )
    from hisiem_soc_copilot.domain.knowledge.enums import SourceKind, Visibility

    opened: list[str] = []

    class _ForbiddenUnitOfWork:
        async def __aenter__(self) -> object:
            opened.append("entered")
            raise AssertionError("the switch must be refused before any read")

        async def __aexit__(self, *exc: object) -> bool:
            return False

    class _Provider:
        @property
        def descriptor(self) -> EmbeddingProfileDescriptor:
            return EmbeddingProfileDescriptor(provider="test", model_id="m", dimension=4)

        async def embed_documents(self, texts: Sequence[str]) -> EmbeddingBatch:
            raise AssertionError("no document may be embedded for a refused switch")

    class _Chunker:
        @property
        def chunker_version(self) -> str:
            return "test-chunker/v1"

        def chunk_document(self, text: str) -> tuple[object, ...]:
            raise AssertionError("the switch must be refused before any chunking")

    handler = KnowledgeIngestionHandler(
        unit_of_work_factory=lambda: _ForbiddenUnitOfWork(),
        embedding_provider=_Provider(),
        chunker=_Chunker(),
        clock=SystemClock(),
    )
    command = IngestKnowledgeDocument(
        source_kind=SourceKind.CURATED_GUIDANCE,
        external_key="curated:switch",
        visibility=Visibility.GLOBAL,
        title="Switch",
        content="body",
        allow_embedding_profile_switch=True,
    )

    with pytest.raises(EmbeddingProfileSwitchRequiresReindexError):
        asyncio.run(handler.ingest(command))

    assert opened == [], "the refusal must happen before the handler reads anything"

# ---------------------------------------------------------------------------
# section 5 (continued) -- the ATT&CK staging/cutover split
#
# The closure's central claim is that an import can no longer make the
# authoritative release and the served content disagree. That claim rests on three
# structural facts about the code, and each is asserted here where it could be
# undone by a later edit:
#
#   * staging ingests WITHOUT moving the pointer;
#   * registration never touches release authority;
#   * the only thing that moves a pointer for a release is the cutover, and the
#     only thing that gates it is the shared ``activate_version`` flag, whose
#     default keeps every ordinary ingest behaving exactly as before.
# ---------------------------------------------------------------------------

_ATTACK_IMPORT = "application/handlers/attack_import.py"
_INGESTION_HANDLER = "application/handlers/knowledge.py"


def _module_tree(relative: str) -> ast.Module:
    """Parse one production module. Raises if the file moved, never skips."""
    path = PKG / relative
    assert path.is_file(), f"{relative} should exist; the guard is scanning nothing"
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found; the guard is asserting about nothing")


def _called_names(node: ast.AST) -> list[str]:
    """Every ``f(...)`` and ``x.y(...)`` call name appearing under ``node``."""
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Name):
                found.append(func.id)
            elif isinstance(func, ast.Attribute):
                found.append(func.attr)
    return found


def test_attack_staging_ingests_without_moving_the_active_pointer() -> None:
    """Staging must be inert: it projects knowledge, it does not publish it.

    ``activate_version=False`` is the whole mechanism. Without it an
    ``activate=False`` import would still move ``active_version_id``, which is
    precisely the defect this closure fixes -- an inactive release changing what
    normal retrieval serves (brief sections 2.4/2.5).
    """
    tree = _module_tree(_ATTACK_IMPORT)
    staging = _function(tree, "_stage_documents")

    ingests = [
        node
        for node in ast.walk(staging)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "IngestKnowledgeDocument"
    ]
    assert ingests, "_stage_documents should build an IngestKnowledgeDocument"

    for call in ingests:
        keywords = {keyword.arg: keyword.value for keyword in call.keywords}
        assert "activate_version" in keywords, (
            "the ATT&CK staging path must state activate_version explicitly; "
            "defaulting to True is the defect"
        )
        value = keywords["activate_version"]
        assert isinstance(value, ast.Constant) and value.value is False, (
            "ATT&CK staging must ingest with activate_version=False, so a staged "
            "release cannot become what retrieval serves"
        )


def test_attack_registration_never_touches_release_authority() -> None:
    """Registration writes canonical rows; authority changes only at the cutover.

    Registering a release and making it authoritative are different acts. If
    registration flipped authority, a crash between registration and projection
    would leave a release authoritative whose content retrieval does not serve --
    the divergence, reintroduced through the back door (brief section 2.7).
    """
    tree = _module_tree(_ATTACK_IMPORT)
    registration = _function(tree, "_persist_release")
    names = _called_names(registration)

    assert "activate" not in names, (
        "registering a release must not activate it; that belongs to _cutover"
    )
    assert "set_active_for_release" not in names, (
        "the technique mirror is owned by the cutover, not by registration"
    )


def test_activation_goes_through_the_cutover_transaction() -> None:
    """The one path that makes a release authoritative, and it is atomic.

    ``import_release`` must call ``_cutover``, and ``_cutover`` must do the
    authority flip, the mirror and the pointer move under ONE ``commit`` -- so a
    reader can never observe authority switched while retrieval still serves the
    previous release (brief sections 2.7/2.8).
    """
    tree = _module_tree(_ATTACK_IMPORT)
    entry = _called_names(_function(tree, "import_release"))
    assert "_cutover" in entry, (
        "import_release must run the cutover; otherwise nothing activates a release"
    )

    cutover = _function(tree, "_cutover")
    names = _called_names(cutover)
    for required in ("activate", "set_active_for_release", "activate_version"):
        assert required in names, (
            f"_cutover should perform {required}; the authority flip and the "
            "pointer move must be one transition"
        )
    commits = [
        node
        for node in ast.walk(cutover)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "commit"
    ]
    assert len(commits) == 1, (
        f"_cutover must commit exactly once, found {len(commits)}: the authority "
        "flip and the pointer move have to land together"
    )


def test_only_the_activate_version_flag_gates_the_pointer_move() -> None:
    """The ordinary "re-ingesting old content is not a rollback" rule survives.

    ``KnowledgeIngestionHandler`` must call ``document.activate_version`` exactly
    once, and only inside ``if command.activate_version:``. That is what lets the
    ATT&CK staging path be inert WITHOUT weakening the generic rule for runbooks
    and curated guidance (brief section 2.4).
    """
    tree = _module_tree(_INGESTION_HANDLER)
    handler = _function(tree, "_persist")

    moves: list[ast.Call] = []
    guards: list[ast.If] = []
    for node in ast.walk(handler):
        if isinstance(node, ast.If):
            guards.append(node)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "activate_version"
        ):
            moves.append(node)

    assert len(moves) == 1, (
        f"expected exactly one activate_version call in _persist, found {len(moves)}"
    )
    guarded = [
        node
        for node in guards
        if any(call is move for call in ast.walk(node) for move in moves)
    ]
    assert guarded, (
        "the pointer move must sit inside a conditional; unconditional activation "
        "is the defect"
    )
    test = guarded[0].test
    assert isinstance(test, ast.Attribute) and test.attr == "activate_version", (
        "the pointer move must be gated on command.activate_version specifically, "
        "not on some other condition that merely happens to hold today"
    )

# ---------------------------------------------------------------------------
# system-owned MITRE_ATTACK (closure-3 section 4)
#
# The authoritative ATT&CK projection has exactly one writer. These guards pin
# the three structural facts that make it true, each asserted where a later
# edit could undo it:
#
#   * the ordinary handler's default capability excludes MITRE_ATTACK;
#   * the importer is wired to the dedicated MITRE-capable factory;
#   * the CLI's ingest-file choices exclude MITRE_ATTACK.
#
# None of them depends on a caller boolean, command metadata, tenant, actor or
# CLI flag -- capability comes from wiring, which is what makes self-asserted
# authority unrepresentable rather than merely refused.
# ---------------------------------------------------------------------------

_HANDLER_MODULE = "application/handlers/knowledge.py"
_CONTAINER_MODULE = "bootstrap/container.py"
_CLI_MODULE = "knowledge/cli.py"


def test_the_ordinary_handler_defaults_to_non_mitre_sources() -> None:
    """The default capability must not include MITRE_ATTACK.

    ``allowed_source_kinds=None`` falls back to ``ORDINARY_WRITABLE_SOURCES``,
    so every caller that forgot to think about capability gets the ordinary
    one. If MITRE_ATTACK ever appears in that default, the single-writer
    invariant is gone for every handler the container builds.
    """
    tree = _module_tree(_HANDLER_MODULE)

    for node in ast.walk(tree):
        target: ast.expr | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target, node.value
        else:
            continue
        if target.id == "ORDINARY_WRITABLE_SOURCES" and value is not None:
                    assert isinstance(value, ast.Call), (
                        "ORDINARY_WRITABLE_SOURCES should stay a frozenset literal"
                    )
                    func = value.func
                    assert isinstance(func, ast.Name) and func.id == "frozenset", (
                        "ORDINARY_WRITABLE_SOURCES should stay a frozenset literal"
                    )
                    args = value.args
                    assert len(args) == 1 and isinstance(args[0], ast.Set), (
                        "ORDINARY_WRITABLE_SOURCES should stay a set literal"
                    )
                    members = {
                        element.attr
                        for element in args[0].elts
                        if isinstance(element, ast.Attribute)
                    }
                    assert "MITRE_ATTACK" not in members, (
                        "the ordinary capability must never include MITRE_ATTACK; "
                        "that source is written only through the ATT&CK importer"
                    )
                    assert members == {"CURATED_GUIDANCE", "TENANT_RUNBOOK"}, (
                        f"unexpected ordinary capability members: {sorted(members)}"
                    )
                    return
    raise AssertionError(
        "ORDINARY_WRITABLE_SOURCES not found; the guard is asserting about nothing"
    )


def test_the_attack_importer_uses_the_dedicated_mitre_capability() -> None:
    """The importer must be wired to the factory that may write MITRE_ATTACK.

    ``attack_import_handler`` builds its ingestion through
    ``attack_projection_ingestion_handler``, never through the ordinary factory
    and never by constructing a handler inline with a caller-chosen allowlist.
    """
    tree = _module_tree(_CONTAINER_MODULE)
    factory = _function(tree, "attack_import_handler")
    names = _called_names(factory)

    assert "attack_projection_ingestion_handler" in names, (
        "the importer must stage through the dedicated MITRE capability"
    )
    assert "knowledge_ingestion_handler" not in names, (
        "the importer must not share the ordinary factory; sharing it would "
        "make the two capabilities one refactor away from each other"
    )

    for node in ast.walk(factory):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "KnowledgeIngestionHandler", (
                "capability is decided by the container's factories, not by an "
                "inline construction inside the importer"
            )


def test_no_caller_boolean_can_grant_mitre_write_authority() -> None:
    """No command field may act as a MITRE bypass flag.

    The forbidden shapes are a boolean like ``allow_system_source``/``trusted``/
    ``internal`` on ``IngestKnowledgeDocument``, and metadata-based inference.
    Capability comes from the handler's construction, full stop.
    """
    tree = _module_tree("application/commands/knowledge.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "IngestKnowledgeDocument":
            fields = {
                target.id
                for statement in node.body
                for target in getattr(statement, "targets", [])
                if isinstance(target, ast.Name)
            }
            forbidden = {
                "allow_system_source",
                "trusted",
                "internal",
                "allow_mitre",
                "system_source",
            }
            assert not (fields & forbidden), (
                f"a caller-controlled bypass flag would re-create self-asserted "
                f"authority: {sorted(fields & forbidden)}"
            )
            return
    raise AssertionError(
        "IngestKnowledgeDocument not found; the guard is asserting about nothing"
    )


def test_the_cli_does_not_offer_mitre_attack_as_an_ingest_choice() -> None:
    """The UX guard: ``ingest-file --source-kind`` must not list MITRE_ATTACK.

    The boundary itself lives in the application handler; this pins the CLI
    surface so an operator is refused by the parser before anything is hashed,
    embedded or written.
    """
    cli_tree = _module_tree(_CLI_MODULE)
    for node in ast.walk(cli_tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        args = [
            arg.value for arg in node.args
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
        ]
        if "--source-kind" not in args:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        choices = keywords.get("choices")
        assert choices is not None, (
            "ingest-file --source-kind must declare an explicit choices list; "
            "an open string would accept MITRE_ATTACK"
        )
        source = ast.unparse(choices)
        assert "MITRE_ATTACK" not in source, (
            "the CLI must not offer MITRE_ATTACK as an ingest-file source kind; "
            "MITRE content enters only through `import-attack`"
        )
        assert "SourceKind" not in source or "ORDINARY_WRITABLE" in source, (
            "the choices must come from the ordinary writable set, not from the "
            "whole SourceKind enum"
        )
        return
    raise AssertionError(
        "no --source-kind add_argument found; the guard is asserting about nothing"
    )
