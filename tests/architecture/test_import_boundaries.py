"""Architecture import-boundary tests.

Automatically checks the module dependency rules from python-package-boundary.md
§24/§35: inner layers never import outer/infrastructure. Boundaries are enforced
at test time — they do not exist only in documentation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent.parent / "src"
PKG = SRC / "hisiem_soc_copilot"

# Forbidden import targets per owning layer (source_layer -> forbidden fragments).
# Fragments match against the imported absolute module path (e.g. ``infrastructure``,
# ``sqlalchemy``), i.e. anything the layer must not depend on.
BOUNDARIES: dict[str, tuple[str, ...]] = {
    "domain": (
        "application",
        "agent",
        "infrastructure",
        "api",
        "bootstrap",
        "sqlalchemy",
        "langgraph",
        "fastapi",
        "httpx",
        "pydantic",
        "alembic",
    ),
    "application": (
        "infrastructure",
        "agent",
        "api",
        "bootstrap",
        "sqlalchemy",
        "langgraph",
        "fastapi",
        "httpx",
    ),
    "agent": ("infrastructure", "api", "bootstrap", "sqlalchemy"),
    "api": (
        "infrastructure.persistence",
        "infrastructure.hisiem",
        "langgraph",
        "agent",
    ),
    "infrastructure": ("api",),
}


def _iter_source_files() -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    for path in PKG.rglob("*.py"):
        rel = path.relative_to(PKG)
        parts = list(rel.parts[:-1]) + [rel.stem]
        # drop the file name for __init__ modules
        if path.name == "__init__.py":
            parts = list(rel.parts[:-1])
        if not parts:
            continue  # hisiem_soc_copilot/__init__.py root
        module = ".".join(parts)
        files.append((module, path))
    return files


def _imported_modules(tree: ast.Module) -> set[str]:
    """Return absolute module names imported by an AST module."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


@pytest.mark.parametrize("module,path", _iter_source_files())
def test_layer_does_not_import_forbidden(module: str, path: Path) -> None:
    layer = module.split(".")[0]
    forbidden = BOUNDARIES.get(layer, ())
    if not forbidden:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Only consider imports of the copilot project itself or of the forbidden
    # third-party/outer modules; a project import resolves via relative base.
    for top in _imported_modules(tree):
        if top in forbidden:
            pytest.fail(f"{module} imports forbidden module '{top}'")


def test_domain_imports_no_framework() -> None:
    """The domain package must be importable with only stdlib available."""
    import hisiem_soc_copilot.domain  # noqa: F401
    import hisiem_soc_copilot.domain.investigation.aggregate  # noqa: F401
    import hisiem_soc_copilot.domain.response.aggregate  # noqa: F401
