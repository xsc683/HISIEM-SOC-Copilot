"""E1-C4 deterministic GP-01 correctness scorer (evaluation-only sidecar).

E1-C4 answers the CORRECTNESS question — did the Agent reach the sealed oracle's
expected verdict backed by the required, grounded, correctly-cited evidence — and
persists a bounded ``score.json`` beside the execution / telemetry / quality
artifacts:

    <executions_dir>/gp-01/<dataset_run_id>/<execution_id>/
        execution.json              (schema evaluation-execution/v2 — unchanged)
        model-telemetry.json        (schema evaluation-model-telemetry/v1)
        tool-evidence-quality.json  (schema evaluation-tool-evidence-quality/v1)
        score.json                  (schema evaluation-score/v1)

Scoring is DETERMINISTIC and MACHINE-ONLY (§2/§6). It is computed from the sealed
oracle (expected verdict + required evidence roles), the persisted
``InvestigationResult`` (disposition + ``finding_ids``), the E1-C3 tool/evidence
quality facts, and the E1-C2 model telemetry gate. It NEVER parses verdict prose,
NEVER scores natural-language similarity, NEVER uses embeddings/fuzzy matching, and
NEVER calls a model or a tool. The confidence value is INFORMATIONAL only and never
carries a threshold (§4/§7).

Oracle firewall (§18): the sealed oracle IS allowed in this evaluation-only
artifact (it names the expected verdict/roles for the report), but it must NEVER
reach ``execution.json``, ``model-telemetry.json``, the graph, a prompt, a tool, the
EvidenceNormalizer, or any production state — the production launch projection
stays the bounded four fields. Every persisted string is length-bounded and the
payload re-validated on read-back against an explicit allowlist.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .quality import (
    DEFAULT_REQUIRED_ROLE,
    GATE_FAIL,
    GATE_PASS,
    FindingFact,
    ToolEvidenceQuality,
)
from .record import atomic_write_json

SCORE_SCHEMA_VERSION = "evaluation-score/v1"
_SCORE_ARTIFACT = "score.json"

# Hard bound for any persisted string (ids are opaque; safety margin).
MAX_FIELD_LEN = 200

# The ONLY keys this artifact may persist (a real allowlist — never a pass-through
# of an internal scoring object).
_PAYLOAD_KEYS: tuple[str, ...] = (
    "schema_version",
    "execution_id",
    "dataset_run_id",
    "investigation_id",
    "expected_verdict",
    "actual_verdict",
    "verdict_match",
    "required_evidence_roles",
    "matched_evidence_roles",
    "evidence_coverage",
    "model_telemetry_gate",
    "tool_evidence_gate",
    "grounded_required_evidence",
    "grounded_finding_ids",
    "result_finding_ids",
    "grounded_findings_in_result",
    "result_finding_integrity_pass",
    "control_exclusion_pass",
    "citation_integrity_pass",
    "oracle_firewall_pass",
    "correctness_gate",
    "gate_failures",
    "confidence",
    "model_calls",
    "tool_calls",
    "search_events_calls",
    "evidence_count",
    "finding_count",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "total_tokens",
)

# Correctness failure tokens (bounded — a deterministic diagnostic, never prose).
FAIL_EXECUTION_NOT_COMPLETED = "EXECUTION_NOT_COMPLETED"
FAIL_INVESTIGATION_RESULT_MISSING = "INVESTIGATION_RESULT_MISSING"
FAIL_MODEL_TELEMETRY_GATE_NOT_PASS = "MODEL_TELEMETRY_GATE_NOT_PASS"
FAIL_TOOL_EVIDENCE_GATE_NOT_PASS = "TOOL_EVIDENCE_GATE_NOT_PASS"
FAIL_VERDICT_MISMATCH = "VERDICT_MISMATCH"
FAIL_REQUIRED_EVIDENCE_NOT_MATCHED = "REQUIRED_EVIDENCE_NOT_MATCHED"
FAIL_REQUIRED_EVIDENCE_NOT_GROUNDED = "REQUIRED_EVIDENCE_NOT_GROUNDED"
FAIL_CONTROL_EVENT_USED_AS_EVIDENCE = "CONTROL_EVENT_USED_AS_EVIDENCE"
FAIL_DANGLING_EVIDENCE_CITATION = "DANGLING_EVIDENCE_CITATION"
FAIL_CROSS_INVESTIGATION_EVIDENCE_CITATION = "CROSS_INVESTIGATION_EVIDENCE_CITATION"
FAIL_ORACLE_FIREWALL_VIOLATION = "ORACLE_FIREWALL_VIOLATION"
FAIL_NO_GROUNDED_FINDING_IN_RESULT = "NO_GROUNDED_FINDING_IN_RESULT"
FAIL_RESULT_FINDING_DANGLING = "RESULT_FINDING_DANGLING"
FAIL_RESULT_FINDING_CROSS_INVESTIGATION = "RESULT_FINDING_CROSS_INVESTIGATION"


class ScoreSchemaError(ValueError):
    """A persisted score payload carries an unsupported/legacy schema version."""


def score_path(
    executions_dir: str | Path, dataset_run_id: str, execution_id: str
) -> Path:
    """The ``score.json`` sidecar beside the other execution artifacts."""
    return (
        Path(executions_dir)
        / "gp-01"
        / dataset_run_id
        / execution_id
        / _SCORE_ARTIFACT
    )


# ---------------------------------------------------------------------------
# Bounded evaluation facts the scorer consumes (production-agnostic inputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleExpectation:
    """The machine-authoritative oracle predicates (never prose, never facts)."""

    expected_verdict: str
    required_evidence_roles: tuple[str, ...] = (DEFAULT_REQUIRED_ROLE,)


@dataclass(frozen=True)
class ResultFact:
    """Bounded projection of the persisted InvestigationResult (§2)."""

    present: bool
    investigation_id: str
    disposition: str | None
    confidence: float | None
    finding_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutionFact:
    """Bounded operational facts of the scored execution (informational)."""

    execution_id: str
    dataset_run_id: str
    completed: bool
    evidence_count: int = 0
    finding_count: int = 0
    tool_calls: int = 0
    search_events_calls: int = 0
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# The pure E1-C4 scorer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvaluationScore:
    """Bounded E1-C4 correctness score + gate (immutable)."""

    schema_version: str = SCORE_SCHEMA_VERSION
    execution_id: str = ""
    dataset_run_id: str = ""
    investigation_id: str = ""
    expected_verdict: str = ""
    actual_verdict: str | None = None
    verdict_match: bool = False
    required_evidence_roles: tuple[str, ...] = ()
    matched_evidence_roles: tuple[str, ...] = ()
    evidence_coverage: float = 0.0
    model_telemetry_gate: str = GATE_FAIL
    tool_evidence_gate: str = GATE_FAIL
    grounded_required_evidence: bool = False
    grounded_finding_ids: tuple[str, ...] = ()
    result_finding_ids: tuple[str, ...] = ()
    grounded_findings_in_result: tuple[str, ...] = ()
    result_finding_integrity_pass: bool = False
    control_exclusion_pass: bool = False
    citation_integrity_pass: bool = False
    oracle_firewall_pass: bool = True
    correctness_gate: str = GATE_FAIL
    gate_failures: tuple[str, ...] = ()
    # informational-only efficiency metrics (§7) — never score-bearing
    confidence: float | None = None
    model_calls: int = 0
    tool_calls: int = 0
    search_events_calls: int = 0
    evidence_count: int = 0
    finding_count: int = 0
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "dataset_run_id": self.dataset_run_id,
            "investigation_id": self.investigation_id,
            "expected_verdict": self.expected_verdict,
            "actual_verdict": self.actual_verdict,
            "verdict_match": self.verdict_match,
            "required_evidence_roles": list(self.required_evidence_roles),
            "matched_evidence_roles": list(self.matched_evidence_roles),
            "evidence_coverage": self.evidence_coverage,
            "model_telemetry_gate": self.model_telemetry_gate,
            "tool_evidence_gate": self.tool_evidence_gate,
            "grounded_required_evidence": self.grounded_required_evidence,
            "grounded_finding_ids": list(self.grounded_finding_ids),
            "result_finding_ids": list(self.result_finding_ids),
            "grounded_findings_in_result": list(self.grounded_findings_in_result),
            "result_finding_integrity_pass": self.result_finding_integrity_pass,
            "control_exclusion_pass": self.control_exclusion_pass,
            "citation_integrity_pass": self.citation_integrity_pass,
            "oracle_firewall_pass": self.oracle_firewall_pass,
            "correctness_gate": self.correctness_gate,
            "gate_failures": list(self.gate_failures),
            "confidence": self.confidence,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "search_events_calls": self.search_events_calls,
            "evidence_count": self.evidence_count,
            "finding_count": self.finding_count,
            "duration_ms": self.duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> EvaluationScore:
        schema = payload.get("schema_version")
        if schema != SCORE_SCHEMA_VERSION:
            raise ScoreSchemaError(
                f"unsupported score schema {schema!r}; this reader only accepts "
                f"{SCORE_SCHEMA_VERSION}"
            )
        safe = {k: payload[k] for k in _PAYLOAD_KEYS if k in payload}
        return cls(
            execution_id=_bounded_str(safe.get("execution_id", "")),
            dataset_run_id=_bounded_str(safe.get("dataset_run_id", "")),
            investigation_id=_bounded_str(safe.get("investigation_id", "")),
            expected_verdict=_bounded_str(safe.get("expected_verdict", "")),
            actual_verdict=_optional_bounded_str(safe.get("actual_verdict")),
            verdict_match=bool(safe.get("verdict_match", False)),
            required_evidence_roles=_bounded_id_tuple(
                safe.get("required_evidence_roles")
            ),
            matched_evidence_roles=_bounded_id_tuple(safe.get("matched_evidence_roles")),
            evidence_coverage=_bounded_float(safe.get("evidence_coverage", 0.0)),
            model_telemetry_gate=_bounded_str(
                safe.get("model_telemetry_gate", GATE_FAIL)
            ),
            tool_evidence_gate=_bounded_str(safe.get("tool_evidence_gate", GATE_FAIL)),
            grounded_required_evidence=bool(
                safe.get("grounded_required_evidence", False)
            ),
            grounded_finding_ids=_bounded_id_tuple(safe.get("grounded_finding_ids")),
            result_finding_ids=_bounded_id_tuple(safe.get("result_finding_ids")),
            grounded_findings_in_result=_bounded_id_tuple(
                safe.get("grounded_findings_in_result")
            ),
            result_finding_integrity_pass=bool(
                safe.get("result_finding_integrity_pass", False)
            ),
            control_exclusion_pass=bool(safe.get("control_exclusion_pass", False)),
            citation_integrity_pass=bool(safe.get("citation_integrity_pass", False)),
            oracle_firewall_pass=bool(safe.get("oracle_firewall_pass", True)),
            correctness_gate=_bounded_str(safe.get("correctness_gate", GATE_FAIL)),
            gate_failures=_bounded_id_tuple(safe.get("gate_failures")),
            confidence=_optional_bounded_float(safe.get("confidence")),
            model_calls=_bounded_int(safe.get("model_calls", 0)),
            tool_calls=_bounded_int(safe.get("tool_calls", 0)),
            search_events_calls=_bounded_int(safe.get("search_events_calls", 0)),
            evidence_count=_bounded_int(safe.get("evidence_count", 0)),
            finding_count=_bounded_int(safe.get("finding_count", 0)),
            duration_ms=_optional_bounded_int(safe.get("duration_ms")),
            input_tokens=_optional_bounded_int(safe.get("input_tokens")),
            output_tokens=_optional_bounded_int(safe.get("output_tokens")),
            total_tokens=_optional_bounded_int(safe.get("total_tokens")),
        )


def _matched_roles(
    required_roles: tuple[str, ...], quality: ToolEvidenceQuality
) -> tuple[str, ...]:
    """The required roles whose EXACT evidence provenance match succeeded.

    GP-01 has a single grounded required role (S1): its identity match is recorded
    by E1-C3 as ``matched_s1_evidence_ids``. The engine is written as a role→match
    resolution so a later multi-role scenario extends the mapping without rewriting
    the scorer.
    """
    if not required_roles:
        return ()
    # The current contract resolves exactly one required role; a non-empty
    # identity match means that role matched (matched_s1_evidence_ids non-empty).
    if quality.matched_s1_evidence_ids:
        return (required_roles[0],)
    return ()


def _token_totals(
    usage_records: Sequence[Mapping[str, Any]],
) -> tuple[int | None, int | None, int | None]:
    """Sum provider-reported token counts; ``None`` when the provider reports none.

    Missing values stay ``None`` — never guessed, never zeroed into a fabricated
    report (§7).
    """

    def _sum(key: str) -> int | None:
        values = [r.get(key) for r in usage_records]
        present = [int(v) for v in values if isinstance(v, int)]
        return sum(present) if present else None

    return _sum("input_tokens"), _sum("output_tokens"), _sum("total_tokens")


def score_gp01(
    *,
    oracle: OracleExpectation,
    result: ResultFact,
    quality: ToolEvidenceQuality,
    findings: Sequence[FindingFact],
    result_finding_owners: Mapping[str, str] | None,
    telemetry_gate: str,
    usage_records: Sequence[Mapping[str, Any]] = (),
    execution: ExecutionFact,
) -> EvaluationScore:
    """Deterministically score ONE execution against the sealed oracle (§4).

    Gate PASS only when EVERY condition holds (§4): the execution COMPLETED with a
    persisted InvestigationResult, the E1-C2 model telemetry gate PASSed, the E1-C3
    tool/evidence gate PASSed, the actual verdict equals the sealed expected verdict,
    every required evidence role matched, the required evidence is grounded through a
    Finding, the control event is not used as evidence, citations are neither
    dangling nor cross-investigation, the oracle firewall held, at least one grounded
    Finding is INCLUDED in ``InvestigationResult.finding_ids``, and every
    ``finding_id`` in the result resolves to a Finding belonging to the SAME
    Investigation (a correct Finding merely existing in the DB does NOT count).

    Pure: no IO, no model, no tools, no prose. ``result_finding_owners`` maps a
    result ``finding_id`` (for ids NOT among this Investigation's own findings) to
    its owning investigation_id within the tenant; absent from the map → dangling,
    a foreign owner → cross-investigation.
    """
    failures: list[str] = []
    owners: Mapping[str, str] = result_finding_owners or {}

    model_telemetry_gate = _bounded_str(telemetry_gate)
    if not execution.completed:
        failures.append(FAIL_EXECUTION_NOT_COMPLETED)
    if not result.present:
        failures.append(FAIL_INVESTIGATION_RESULT_MISSING)
    if model_telemetry_gate != GATE_PASS:
        failures.append(FAIL_MODEL_TELEMETRY_GATE_NOT_PASS)
    tool_evidence_gate = quality.gate_status
    if tool_evidence_gate != GATE_PASS:
        failures.append(FAIL_TOOL_EVIDENCE_GATE_NOT_PASS)

    actual_verdict = result.disposition
    verdict_match = actual_verdict is not None and actual_verdict == oracle.expected_verdict
    if not verdict_match:
        failures.append(FAIL_VERDICT_MISMATCH)

    required_roles = oracle.required_evidence_roles or (DEFAULT_REQUIRED_ROLE,)
    matched_roles = _matched_roles(required_roles, quality)
    evidence_coverage = (
        len(matched_roles) / len(required_roles) if required_roles else 1.0
    )
    if len(matched_roles) < len(required_roles):
        failures.append(FAIL_REQUIRED_EVIDENCE_NOT_MATCHED)

    grounded_finding_ids = quality.findings_citing_s1_evidence
    grounded_required_evidence = bool(grounded_finding_ids)
    if not grounded_required_evidence:
        failures.append(FAIL_REQUIRED_EVIDENCE_NOT_GROUNDED)

    control_exclusion_pass = not quality.control_event_evidence_ids
    if not control_exclusion_pass:
        failures.append(FAIL_CONTROL_EVENT_USED_AS_EVIDENCE)

    citation_integrity_pass = (
        quality.dangling_citation_count == 0
        and quality.cross_investigation_citation_count == 0
    )
    if quality.dangling_citation_count:
        failures.append(FAIL_DANGLING_EVIDENCE_CITATION)
    if quality.cross_investigation_citation_count:
        failures.append(FAIL_CROSS_INVESTIGATION_EVIDENCE_CITATION)

    oracle_firewall_pass = bool(quality.oracle_firewall_pass)
    if not oracle_firewall_pass:
        failures.append(FAIL_ORACLE_FIREWALL_VIOLATION)

    # Result → Finding integrity: every result finding_id must resolve to a Finding
    # belonging to THIS Investigation.
    own_finding_ids = {
        f.finding_id for f in findings if f.investigation_id == result.investigation_id
    }
    result_finding_ids = tuple(result.finding_ids)
    dangling_result = 0
    cross_result = 0
    for fid in result_finding_ids:
        if fid in own_finding_ids:
            continue
        owner = owners.get(fid)
        if owner is None:
            dangling_result += 1
        elif owner != result.investigation_id:
            cross_result += 1
    result_finding_integrity_pass = dangling_result == 0 and cross_result == 0
    if dangling_result:
        failures.append(FAIL_RESULT_FINDING_DANGLING)
    if cross_result:
        failures.append(FAIL_RESULT_FINDING_CROSS_INVESTIGATION)

    result_finding_set = set(result_finding_ids)
    grounded_findings_in_result = tuple(
        fid for fid in grounded_finding_ids if fid in result_finding_set
    )
    if not grounded_findings_in_result:
        failures.append(FAIL_NO_GROUNDED_FINDING_IN_RESULT)

    input_tokens, output_tokens, total_tokens = _token_totals(usage_records)

    return EvaluationScore(
        execution_id=_bounded_str(execution.execution_id),
        dataset_run_id=_bounded_str(execution.dataset_run_id),
        investigation_id=_bounded_str(result.investigation_id),
        expected_verdict=_bounded_str(oracle.expected_verdict),
        actual_verdict=_optional_bounded_str(actual_verdict),
        verdict_match=verdict_match,
        required_evidence_roles=tuple(_bounded_str(r) for r in required_roles),
        matched_evidence_roles=matched_roles,
        evidence_coverage=evidence_coverage,
        model_telemetry_gate=model_telemetry_gate,
        tool_evidence_gate=_bounded_str(tool_evidence_gate),
        grounded_required_evidence=grounded_required_evidence,
        grounded_finding_ids=tuple(_bounded_str(f) for f in grounded_finding_ids),
        result_finding_ids=tuple(_bounded_str(f) for f in result_finding_ids),
        grounded_findings_in_result=grounded_findings_in_result,
        result_finding_integrity_pass=result_finding_integrity_pass,
        control_exclusion_pass=control_exclusion_pass,
        citation_integrity_pass=citation_integrity_pass,
        oracle_firewall_pass=oracle_firewall_pass,
        correctness_gate=GATE_PASS if not failures else GATE_FAIL,
        gate_failures=tuple(failures),
        confidence=result.confidence,
        model_calls=len(usage_records),
        tool_calls=execution.tool_calls,
        search_events_calls=execution.search_events_calls,
        evidence_count=execution.evidence_count,
        finding_count=execution.finding_count,
        duration_ms=execution.duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


# ---------------------------------------------------------------------------
# Bounded coercion helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any) -> str:
    return str(value)[:MAX_FIELD_LEN]


def _optional_bounded_str(value: Any) -> str | None:
    return None if value is None else str(value)[:MAX_FIELD_LEN]


def _bounded_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_bounded_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bounded_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _optional_bounded_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bounded_id_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        return (_bounded_str(value),)
    return tuple(_bounded_str(item) for item in value)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def write_score(path: str | Path, score: EvaluationScore) -> None:
    """Atomically persist the score sidecar (never sealed)."""
    atomic_write_json(path, score.to_payload())


def read_score(path: str | Path) -> EvaluationScore:
    """Load + validate a persisted score artifact."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return EvaluationScore.from_payload(payload)
