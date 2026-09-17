"""Architecture boundary for the XP-01 cross-plane evaluation family.

Three rules, enforced over parsed ASTs (never by importing the modules):

1. **Production layers must not import ``evaluation_harness``.** This invariant was
   already stated in the harness package docstring ("Production code never imports
   this package") but was **not enforced anywhere**: ``test_evaluation_boundary``'s
   import matcher deliberately matches the ``evaluation`` package only, and
   ``_reaches(target, "evaluation")`` does not match ``evaluation_harness`` because
   the separator is ``_`` rather than ``.``. E1 closes that gap, because the XP-01
   adapter adds a second non-production bridge and the document-only guarantee would
   otherwise cover neither.

2. **Production layers must not import the XP-01 oracle.** Re-asserted here,
   scoped to this family, so a future loosening of the general boundary cannot
   silently loosen XP-01's.

3. **No XP-01 scenario identity may appear in production source.** Scenario ids and
   gate ids are the evaluation oracle's vocabulary; finding one in a production
   layer means evaluation expectations reached the runtime. Expected/forbidden *fact
   tokens* are deliberately NOT string-scanned: they are ordinary English compounds
   that production already uses for unrelated purposes (``workspace_service``'s
   timeline label ``INVESTIGATION_COMPLETED``, ``response_runner``'s
   ``_SUBMISSION_ATTENTION_REQUIRED`` variable), so a string scan over them would
   report noise rather than a boundary violation. The structural guarantee is the
   import rule above.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent.parent / "src"
PKG = SRC / "hisiem_soc_copilot"
PACKAGE_ROOT = "hisiem_soc_copilot"

#: Production layers, pinned rather than discovered: a new production package must
#: be added here deliberately before it can import the evaluation planes.
PRODUCTION_LAYERS = frozenset(
    {"domain", "application", "agent", "api", "infrastructure", "bootstrap"}
)

EVALUATION_HARNESS = f"{PACKAGE_ROOT}.evaluation_harness"
EVALUATION = f"{PACKAGE_ROOT}.evaluation"
CROSS_PLANE_DIR = PKG / "evaluation" / "cross_plane"
ADAPTER_PATH = PKG / "evaluation_harness" / "cross_plane_adapter.py"

#: stdlib the pure XP-01 package may touch: serialization, identity, and pure data
#: structures. No clock, no randomness, no host, no driver — which is what makes a
#: gate verdict reproducible from the collected facts alone.
CROSS_PLANE_ALLOWED_FRAGMENTS = frozenset(
    {
        "__future__",
        "collections",
        "dataclasses",
        "enum",
        "hashlib",
        "json",
        "typing",
        "hisiem_soc_copilot",
    }
)

#: Fragments that would make the pure package an execution path rather than a
#: contract. Matched as top-level import names.
CROSS_PLANE_FORBIDDEN_FRAGMENTS = frozenset(
    {
        "asyncio",
        "httpx",
        "os",
        "pathlib",
        "requests",
        "socket",
        "subprocess",
        "tempfile",
        "time",
        "uuid",
    }
)


def _production_files() -> list[Path]:
    return [
        path
        for path in PKG.rglob("*.py")
        if path.relative_to(PKG).parts[0] in PRODUCTION_LAYERS
    ]


def _absolute_imports(tree: ast.Module) -> set[str]:
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            imported.add(node.module)
    return imported


def _reaches(target: str, package: str) -> bool:
    """True when ``target`` is ``package`` or a module beneath it."""
    return target == package or target.startswith(f"{package}.")


def test_production_never_imports_evaluation_harness() -> None:
    """The invariant the harness docstring promised but no test enforced."""
    offenders: list[str] = []
    for path in _production_files():
        for target in _absolute_imports(ast.parse(path.read_text(encoding="utf-8"))):
            if _reaches(target, EVALUATION_HARNESS):
                offenders.append(f"{path.relative_to(PKG)} imports {target!r}")
    assert not offenders, (
        "production layers must never import the evaluation harness:\n"
        + "\n".join(offenders)
    )


def test_production_never_imports_the_xp01_oracle() -> None:
    offenders: list[str] = []
    for path in _production_files():
        for target in _absolute_imports(ast.parse(path.read_text(encoding="utf-8"))):
            if _reaches(target, EVALUATION) or _reaches(target, CROSS_PLANE_DIR.name):
                offenders.append(f"{path.relative_to(PKG)} imports {target!r}")
    assert not offenders, (
        "production layers must never import the XP-01 evaluation oracle:\n"
        + "\n".join(offenders)
    )


def test_no_xp01_scenario_or_gate_identity_appears_in_production() -> None:
    """The oracle firewall: evaluation vocabularies stay inside the evaluation plane."""
    import sys

    sys.path.insert(0, str(SRC))
    from hisiem_soc_copilot.evaluation.cross_plane import GATE_IDS, XP01_SCENARIO_IDS

    tokens = sorted(set(XP01_SCENARIO_IDS) | set(GATE_IDS))
    assert tokens, "the XP-01 vocabulary must not be empty"
    offenders: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        for token in tokens:
            if token in text:
                offenders.append(f"{path.relative_to(PKG)} contains {token!r}")
    assert not offenders, "XP-01 identity leaked into production:\n" + "\n".join(offenders)


def test_xp01_adapter_lives_under_the_sanctioned_bridge() -> None:
    assert ADAPTER_PATH.is_file(), "the XP-01 adapter must live in evaluation_harness"
    for layer in PRODUCTION_LAYERS:
        assert not (PKG / layer / "cross_plane_adapter.py").exists(), (
            f"XP-01 adapter must not live in production layer {layer!r}"
        )


def test_xp01_pure_package_imports_only_the_allowed_surface() -> None:
    offenders: list[str] = []
    for path in sorted(CROSS_PLANE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in CROSS_PLANE_ALLOWED_FRAGMENTS:
                        offenders.append(f"{path.name} imports {alias.name!r}")
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                top = node.module.split(".")[0]
                if top not in CROSS_PLANE_ALLOWED_FRAGMENTS:
                    offenders.append(f"{path.name} imports {node.module!r}")
    assert not offenders, (
        "the XP-01 contract package escaped its allowed import surface:\n"
        + "\n".join(offenders)
    )


def test_xp01_pure_package_has_no_io_or_clock_imports() -> None:
    """A gate verdict must not be able to read a clock, a file, or a socket."""
    offenders: list[str] = []
    for path in sorted(CROSS_PLANE_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                if top in CROSS_PLANE_FORBIDDEN_FRAGMENTS:
                    offenders.append(f"{path.name} imports {name!r}")
    assert not offenders, (
        "the XP-01 contract package must stay pure:\n" + "\n".join(offenders)
    )


def test_the_boundary_is_not_vacuous() -> None:
    """Guards against a rename/emptying that would make the rules above pass trivially."""
    assert sorted(path.name for path in CROSS_PLANE_DIR.rglob("*.py")) == [
        "__init__.py",
        "artifacts.py",
        "catalog.py",
        "contracts.py",
        "gates.py",
    ]
    assert _production_files(), "production file discovery must not be empty"
