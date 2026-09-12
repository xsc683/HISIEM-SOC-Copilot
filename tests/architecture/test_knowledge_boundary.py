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
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

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
