"""Test-isolation architecture guard.

Two independent rules, both read from source by parsing (never importing) the
test modules:

1.  No test module may put the operator's development database, or HISIEM's, into
    a string literal. Either one appearing in a test is a test that has been
    pointed at a database it must not touch; the pgvector TEST server exists for
    exactly that reason (see ``tests/support/db_runtime.py``). A handful of
    modules legitimately *name* those ports -- the fail-closed devtools
    validator, the compose-file contract, and two redaction tests -- and each is
    allow-listed below with its justification.

2.  A module that contains a ``TRUNCATE`` or ``DELETE FROM`` statement must not
    also resolve the operator's database. This is the actual invariant behind
    the P3-A closure-2 blocker: the two facts together are what make a
    destructive test dangerous, and rule 1 alone would not notice a module that
    TRUNCATEs a URL it built from a variable.

Parsing rather than importing matters: importing a test module executes its
module-level code (module-level ``pytestmark`` reachability probes, module-level
``Settings()`` construction), which is precisely the code this guard exists to
police.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent.parent

SELF = Path(__file__).resolve()

#: Fragments that must not appear in a test string literal, except where
#: allow-listed. ``:5433`` is the operator's Copilot development database;
#: ``:5432`` is HISIEM's own.
FORBIDDEN_FRAGMENTS = (":5433", ":5432")

#: Statements that make a module destructive. Word boundaries matter: the CLI's
#: ``truncated`` result key must not read as a TRUNCATE statement.
DESTRUCTIVE_RE = re.compile(r"\bTRUNCATE\b|\bDELETE\s+FROM\b", re.IGNORECASE)

#: ``{path relative to tests/, permitted substrings}``.
#:
#: Deliberately keyed by file + a STABLE substring of the literal rather than by
#: line number: another agent is editing the migration round-trip test, and a
#: line-keyed allow-list would break under their edits and silently stop guarding
#: anything.
ALLOWED: dict[str, tuple[str, ...]] = {
    # The fail-closed target validator. This module's whole job is to prove that
    # 5433/copilot is ACCEPTED and that 5432, a wrong database, and a non-local
    # host are REJECTED -- so it has to contain the literals, including the
    # rejected ones. It never connects to anything.
    "tests/devtools/test_copilot_db.py": (
        "@127.0.0.1:5433/copilot",  # _OK: the one accepted target
        "@localhost:5433/copilot",  # the accepted localhost alias
        "@127.0.0.1:5432/copilot",  # the 5432 REJECTION case
        "@127.0.0.1:5433/siem",  # the wrong-database REJECTION case
        "@10.0.0.5:5433/copilot",  # the non-local-host REJECTION case
    ),
    # The default development runtime contract. The compose file must publish
    # the Copilot PostgreSQL on 5433 and must NOT claim HISIEM's 5432; the test
    # asserts both directions, so both literals have to be here.
    "tests/integration/migrations/test_migration_round_trip.py": (
        "5433:5432",  # the required mapping
        "5432:5432",  # the mapping that must be ABSENT
    ),
    # Diagnostics redaction. The point of the assertion is that the rendered URL
    # keeps the host and port while dropping the password, so the host:port has
    # to survive in the expected value.
    "tests/unit/knowledge/test_diagnostics.py": (
        "@127.0.0.1:5433/copilot",  # DATABASE_URL + the redacted expectation
    ),
    # The CLI doctor payload echoes the configured URL verbatim; the assertion
    # is that it does so without leaking the password.
    "tests/unit/knowledge/test_knowledge_cli.py": (
        "@localhost:5433/copilot",  # DATABASE_URL
    ),
}


def _relative(path: Path) -> str:
    """POSIX-style path of ``path`` relative to the repo root."""
    return path.resolve().relative_to(TESTS.parent).as_posix()


def _iter_test_files() -> Iterator[Path]:
    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _string_constants(tree: ast.Module) -> Iterator[tuple[int, str]]:
    """Every string literal in the module, with its line number.

    f-strings contribute their literal segments as ``ast.Constant`` nodes, so a
    forbidden port spelled inside an f-string is caught only if it is spelled
    literally -- which is the right behaviour: building the address from the
    shared constants is the thing we WANT tests to do.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _violates(rel: str, value: str) -> str | None:
    """The forbidden fragment in ``value``, or None when it is permitted."""
    if rel == _relative(SELF):
        return None  # the guard's own source, including this allow-list
    hit = next((f for f in FORBIDDEN_FRAGMENTS if f in value), None)
    if hit is None:
        return None
    permitted = ALLOWED.get(rel, ())
    if any(sub in value for sub in permitted):
        return None
    return hit


def test_no_test_module_names_a_forbidden_database_port() -> None:
    """A forbidden host:port literal anywhere in ``tests/`` fails the suite."""
    failures: list[str] = []
    for path in _iter_test_files():
        rel = _relative(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, value in _string_constants(tree):
            hit = _violates(rel, value)
            if hit is not None:
                failures.append(f"{rel}:{lineno}: {hit!r} in {value[:90]!r}")
    if failures:
        pytest.fail(
            "Test modules name a database tests must never touch:\n  "
            + "\n  ".join(failures)
            + "\nUse tests.support.db_runtime (scratch_database_url / "
            "session_scratch_database_url) instead, or add a justified entry to "
            "ALLOWED in this module."
        )


def test_no_destructive_module_resolves_the_operator_database() -> None:
    """A module that TRUNCATEs or DELETEs must not also resolve port 5433.

    Rule 1 already catches a literal 5433 URL, so this rule is about the SHAPE
    of the hazard rather than a duplicate check: it states, in one place, that
    "destructive statement" and "operator database" must never co-occur in a
    test module. It is the invariant a reviewer should be able to read.
    """
    failures: list[str] = []
    for path in _iter_test_files():
        rel = _relative(path)
        if rel == _relative(SELF):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        values = [v for _lineno, v in _string_constants(tree)]
        destructive = [v for v in values if DESTRUCTIVE_RE.search(v)]
        if not destructive:
            continue
        operator = [v for v in values if ":5433" in v and not _violates(rel, v)]
        if operator:
            failures.append(f"{rel}: TRUNCATE/DELETE present together with {operator[0]!r}")
    if failures:
        pytest.fail(
            "Destructive test module resolves the operator database:\n  " + "\n  ".join(failures)
        )
