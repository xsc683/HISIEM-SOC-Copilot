"""Architecture boundaries for the evaluation package (E1-B.3 §20, E1-B.4 §13/§24).

Two one-way dependency rules, enforced over parsed ASTs (never importing modules):

1. Production layers (domain | application | agent | api | infrastructure |
   bootstrap) MUST NOT import ``hisiem_soc_copilot.evaluation`` — evaluation
   oracle/control data can never reach production investigation code.
2. Evaluation submodules import only the allowed surface: stdlib + ``..config`` +
   sibling ``evaluation.*`` + the reader's one httpx dependency + (reader only)
   the production ``application.errors`` taxonomy. In particular the pure sealer
   modules (manifest/sealer/oracle/launch_projection/errors) never import
   hisiem_reader/injector/materializer, and no evaluation module leaks the oracle
   into production.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src"
PKG = SRC / "hisiem_soc_copilot"

# Production layers that must never see the evaluation package.
_PRODUCTION_LAYERS = frozenset(
    {"domain", "application", "agent", "api", "infrastructure", "bootstrap"}
)

# Allowed top-level fragments for the evaluation module's own imports. Fragments
# are matched against the FIRST top-level name of each absolute import target
# (stdlib top-levels, ``hisiem_soc_copilot``, or third-party top-levels).
_ALLOWED_FRAGMENTS = frozenset(
    {
        # stdlib
        "__future__",
        "asyncio",
        "collections",
        "contextlib",
        "dataclasses",
        "datetime",
        "enum",
        "fcntl",  # POSIX advisory publication lock (platform-branched in sealer)
        "hashlib",
        "ipaddress",
        "json",
        "msvcrt",  # Windows advisory publication lock (platform-branched in sealer)
        "os",
        "pathlib",
        "socket",
        "subprocess",
        "sys",
        "time",
        "typing",
        "uuid",
        "zoneinfo",
        # project config + the evaluation package itself
        "hisiem_soc_copilot",
        # the reader's HTTP surface
        "httpx",
    }
)

# The pure sealer/manifest stack must stay I/O free: it never imports the reader,
# injector, materializer, or the CLI (which reaches the reader/injector).
_PURE_SEALER = frozenset({"manifest", "sealer", "oracle", "launch_projection", "errors"})
_IO_MODULES = frozenset({"hisiem_reader", "injector", "materializer", "cli"})


def _module_files() -> list[tuple[str, Path]]:
    """Enumerate (module_path_under_hisiem_soc_copilot, file) for evaluation."""
    eval_dir = PKG / "evaluation"
    for path in sorted(eval_dir.rglob("*.py")):
        rel = path.relative_to(PKG)
        parts = list(rel.parts[:-1]) + [rel.stem]
        if path.name == "__init__.py":
            parts = list(rel.parts[:-1])
        if not parts:
            continue
        yield ".".join(parts), path


def _production_files() -> list[Path]:
    """Every .py file under a production layer (never the evaluation package)."""
    files: list[Path] = []
    for path in PKG.rglob("*.py"):
        parts = path.relative_to(PKG).parts
        if parts and parts[0] in _PRODUCTION_LAYERS:
            files.append(path)
    return files


def _evaluation_segment(target: str) -> str | None:
    """Return the evaluation submodule segment an absolute target imports.

    Handles ``evaluation.X`` and ``hisiem_soc_copilot.evaluation.X``. Returns the
    first segment after ``evaluation`` (e.g. ``manifest``), or None when the
    target does not reach an evaluation submodule.
    """
    head, _, tail = target.partition(".")
    if head == "hisiem_soc_copilot":
        rest = target[len("hisiem_soc_copilot."):]
        prefix, _, sub = rest.partition(".")
        if prefix == "evaluation" and sub:
            return sub.split(".")[0]
        return None
    if head == "evaluation" and tail:
        return tail.split(".")[0]
    return None


def _resolve_relative(path: Path, target: str, level: int) -> str:
    """Resolve a relative import against the importing file's package directory.

    Relative imports resolve from the importing module's ``__package__``, which is
    the *directory* holding the file (for ``evaluation/manifest.py`` and
    ``evaluation/__init__.py`` alike that is ``hisiem_soc_copilot.evaluation``).
    ``level`` is the number of leading dots; each dot beyond the first climbs one
    package toward the project root. Returns a full ``hisiem_soc_copilot.*`` path.
    """
    parts = [p for p in path.relative_to(PKG).parts[:-1] if p]
    for _ in range(level - 1):
        if parts:
            parts.pop()
    name = ["hisiem_soc_copilot"] + parts
    if target:
        name.append(target)
    return ".".join(name)


def _relative_level(node: ast.ImportFrom) -> int:
    return node.level if node.level else 0


def _allowed_relative(resolved: str) -> bool:
    """True when a resolved relative import is legal for an evaluation module.

    Allowed: any ``hisiem_soc_copilot.evaluation.*`` sibling,
    ``hisiem_soc_copilot.config``, and the production ``application.errors``
    taxonomy the reader's request path raises.
    """
    return (
        resolved.startswith("hisiem_soc_copilot.evaluation")
        or resolved == "hisiem_soc_copilot.config"
        or resolved == "hisiem_soc_copilot.application.errors"
    )


def test_production_modules_never_import_evaluation() -> None:
    offenders: list[str] = []
    for path in _production_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _evaluation_segment(alias.name) is not None:
                        offenders.append(f"{path.relative_to(PKG)} imports {alias.name!r}")
            elif isinstance(node, ast.ImportFrom) and node.module and _evaluation_segment(
                node.module
            ) is not None:
                offenders.append(f"{path.relative_to(PKG)} imports {node.module!r}")
    msg = "production layers must never import the evaluation oracle:\n" + "\n".join(offenders)
    assert not offenders, msg


def test_evaluation_modules_import_only_allowed_surface() -> None:
    offenders: list[str] = []
    for module, path in _module_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in _ALLOWED_FRAGMENTS:
                        offenders.append(
                            f"{module} imports disallowed {alias.name!r} (top={top!r})"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                level = _relative_level(node)
                if level:
                    resolved = _resolve_relative(path, node.module, level)
                    if not _allowed_relative(resolved):
                        offenders.append(f"{module} imports disallowed relative {resolved!r}")
                elif node.module.split(".")[0] not in _ALLOWED_FRAGMENTS:
                    offenders.append(
                        f"{module} imports disallowed {node.module!r} "
                        f"(top={node.module.split('.')[0]!r})"
                    )
    assert not offenders, "evaluation module escaped its allowed import surface:\n" + "\n".join(
        offenders
    )


def test_pure_sealer_modules_never_import_reader_or_injector() -> None:
    offenders: list[str] = []
    for module, path in _module_files():
        if module.split(".")[-1] not in _PURE_SEALER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if _evaluation_segment(alias.name) in _IO_MODULES:
                        offenders.append(
                            f"{module} imports {alias.name!r} — pure sealer must stay I/O free"
                        )
            elif isinstance(node, ast.ImportFrom) and node.module:
                level = _relative_level(node)
                resolved = (
                    _resolve_relative(path, node.module, level)
                    if level
                    else node.module
                )
                if _evaluation_segment(resolved) in _IO_MODULES:
                    offenders.append(
                        f"{module} imports {resolved!r} — pure sealer must stay I/O free"
                    )
    assert not offenders, "\n".join(offenders)
