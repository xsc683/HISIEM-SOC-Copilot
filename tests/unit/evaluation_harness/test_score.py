"""E1-C4 deterministic correctness scorer unit tests (§2, §3, §4, §19).

Covers the pure :func:`score_gp01` (machine-only, no LLM-as-a-Judge) plus the
bounded ``score.json`` artifact. The required negative matrix (§19) is enumerated
explicitly, and the correct chain → PASS is asserted alongside them.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation_harness.quality import (
    EXPECTED_SOURCE_OPERATION,
    EXPECTED_SOURCE_PROVIDER,
    EXPECTED_SOURCE_TYPE,
    GATE_FAIL,
    GATE_PASS,
    MAX_FIELD_LEN,
    SEARCH_EVENTS_TOOL_NAME,
    STATUS_SUCCEEDED,
    EvidenceFact,
    FindingFact,
    ToolEvidenceQuality,
    ToolInvocationFact,
    build_tool_evidence_quality,
)
from hisiem_soc_copilot.evaluation_harness.score import (
    FAIL_CONTROL_EVENT_USED_AS_EVIDENCE,
    FAIL_CROSS_INVESTIGATION_EVIDENCE_CITATION,
    FAIL_DANGLING_EVIDENCE_CITATION,
    FAIL_EXECUTION_NOT_COMPLETED,
    FAIL_INVESTIGATION_RESULT_MISSING,
    FAIL_MODEL_TELEMETRY_GATE_NOT_PASS,
    FAIL_NO_GROUNDED_FINDING_IN_RESULT,
    FAIL_ORACLE_FIREWALL_VIOLATION,
    FAIL_REQUIRED_EVIDENCE_NOT_GROUNDED,
    FAIL_REQUIRED_EVIDENCE_NOT_MATCHED,
    FAIL_RESULT_FINDING_CROSS_INVESTIGATION,
    FAIL_RESULT_FINDING_DANGLING,
    FAIL_TOOL_EVIDENCE_GATE_NOT_PASS,
    FAIL_VERDICT_MISMATCH,
    SCORE_SCHEMA_VERSION,
    EvaluationScore,
    ExecutionFact,
    OracleExpectation,
    ResultFact,
    ScoreSchemaError,
    read_score,
    score_gp01,
    score_path,
    write_score,
)

INV = "11111111-1111-1111-1111-111111111111"
OTHER_INV = "22222222-2222-2222-2222-222222222222"
S1_INDEX = "siem-events-gp01"
S1_DOC = "es-doc-S1-run-1"
W1_DOC = "es-doc-W1-run-1"


def _s1_evidence(eid: str = "ev-s1") -> EvidenceFact:
    return EvidenceFact(
        evidence_id=eid,
        investigation_id=INV,
        source_type=EXPECTED_SOURCE_TYPE,
        source_provider=EXPECTED_SOURCE_PROVIDER,
        source_operation=EXPECTED_SOURCE_OPERATION,
        source_tool_invocation_id="tool-1",
        raw_index=S1_INDEX,
        raw_document_id=S1_DOC,
        raw_query_fingerprint="fp",
        content_hash="hash",
    )


def _w1_evidence() -> EvidenceFact:
    return EvidenceFact(
        evidence_id="ev-w1",
        investigation_id=INV,
        source_type=EXPECTED_SOURCE_TYPE,
        source_provider=EXPECTED_SOURCE_PROVIDER,
        source_operation=EXPECTED_SOURCE_OPERATION,
        source_tool_invocation_id="tool-1",
        raw_index=S1_INDEX,
        raw_document_id=W1_DOC,
        raw_query_fingerprint="fp",
        content_hash="hash",
    )


def _search_inv() -> ToolInvocationFact:
    return ToolInvocationFact(
        invocation_id="tool-1",
        investigation_id=INV,
        tool_name=SEARCH_EVENTS_TOOL_NAME,
        status=STATUS_SUCCEEDED,
    )


def _finding(fid: str = "find-1", *, cites: tuple[str, ...] = ("ev-s1",)) -> FindingFact:
    return FindingFact(finding_id=fid, investigation_id=INV, cited_evidence_ids=cites)


def _build_quality(**overrides: object) -> ToolEvidenceQuality:
    kwargs: dict[str, object] = {
        "execution_id": "exec-1",
        "dataset_run_id": "run-1",
        "investigation_id": INV,
        "execution_completed": True,
        "result_present": True,
        "model_telemetry_gate": GATE_PASS,
        "required_role": "S1",
        "expected_s1_index": S1_INDEX,
        "expected_s1_document_id": S1_DOC,
        "expected_control_index": S1_INDEX,
        "expected_control_document_id": W1_DOC,
        "evidence": (_s1_evidence(),),
        "findings": (_finding(),),
        "tool_invocations": (_search_inv(),),
        "citation_owners": {},
    }
    kwargs.update(overrides)
    return build_tool_evidence_quality(**kwargs)  # type: ignore[arg-type]


def _oracle() -> OracleExpectation:
    return OracleExpectation(expected_verdict="MALICIOUS", required_evidence_roles=("S1",))


def _result(**overrides: object) -> ResultFact:
    kwargs: dict[str, object] = {
        "present": True,
        "investigation_id": INV,
        "disposition": "MALICIOUS",
        "confidence": 0.8,
        "finding_ids": ("find-1",),
    }
    kwargs.update(overrides)
    return ResultFact(**kwargs)  # type: ignore[arg-type]


def _execution(**overrides: object) -> ExecutionFact:
    kwargs: dict[str, object] = {
        "execution_id": "exec-1",
        "dataset_run_id": "run-1",
        "completed": True,
        "evidence_count": 2,
        "finding_count": 1,
        "tool_calls": 1,
        "search_events_calls": 1,
        "duration_ms": 1234,
    }
    kwargs.update(overrides)
    return ExecutionFact(**kwargs)  # type: ignore[arg-type]


def _score(**overrides: object) -> EvaluationScore:
    kwargs: dict[str, object] = {
        "oracle": _oracle(),
        "result": _result(),
        "quality": _build_quality(),
        "findings": (_finding(),),
        "result_finding_owners": {},
        "telemetry_gate": GATE_PASS,
        "usage_records": (),
        "execution": _execution(),
    }
    kwargs.update(overrides)
    return score_gp01(**kwargs)  # type: ignore[arg-type]


# --- the correct chain → PASS ------------------------------------------------


def test_correct_chain_malicious_passes() -> None:
    score = _score()
    assert score.correctness_gate == GATE_PASS
    assert score.gate_failures == ()
    assert score.verdict_match is True
    assert score.evidence_coverage == 1.0
    assert score.matched_evidence_roles == ("S1",)
    assert score.grounded_required_evidence is True
    assert score.grounded_findings_in_result == ("find-1",)
    assert score.result_finding_integrity_pass is True
    assert score.control_exclusion_pass is True
    assert score.citation_integrity_pass is True
    assert score.oracle_firewall_pass is True


# --- §19 negative matrix -----------------------------------------------------


def test_wrong_verdict_fails() -> None:
    score = _score(result=_result(disposition="BENIGN"))
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_VERDICT_MISMATCH in score.gate_failures
    assert score.verdict_match is False


def test_missing_s1_role_match_fails() -> None:
    # A quality built with no S1 evidence: the required role never matched.
    quality = _build_quality(evidence=(), findings=())
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_REQUIRED_EVIDENCE_NOT_MATCHED in score.gate_failures
    assert score.evidence_coverage == 0.0
    assert score.matched_evidence_roles == ()


def test_s1_exists_but_no_grounded_finding_fails() -> None:
    # S1 matched, but no Finding cites it (grounded_required_evidence false).
    quality = replace(
        _build_quality(), findings_citing_s1_evidence=()
    )
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_REQUIRED_EVIDENCE_NOT_GROUNDED in score.gate_failures
    assert FAIL_NO_GROUNDED_FINDING_IN_RESULT in score.gate_failures


def test_grounded_finding_not_in_result_fails() -> None:
    # The grounded Finding exists in the DB but does NOT participate in the result.
    score = _score(result=_result(finding_ids=()))
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_NO_GROUNDED_FINDING_IN_RESULT in score.gate_failures
    assert score.grounded_findings_in_result == ()


def test_result_finding_id_does_not_exist_fails_dangling() -> None:
    score = _score(
        result=_result(finding_ids=("find-1", "find-ghost")),
    )
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_RESULT_FINDING_DANGLING in score.gate_failures
    assert score.result_finding_integrity_pass is False


def test_result_finding_from_foreign_investigation_fails_cross() -> None:
    score = _score(
        result=_result(finding_ids=("find-1", "find-foreign")),
        result_finding_owners={"find-foreign": OTHER_INV},
    )
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_RESULT_FINDING_CROSS_INVESTIGATION in score.gate_failures


def test_w1_control_event_used_as_evidence_fails() -> None:
    quality = _build_quality(
        evidence=(_s1_evidence(), _w1_evidence()),
    )
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_CONTROL_EVENT_USED_AS_EVIDENCE in score.gate_failures


def test_dangling_citation_fails() -> None:
    quality = _build_quality(findings=(_finding(cites=("ev-s1", "ev-missing")),))
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_DANGLING_EVIDENCE_CITATION in score.gate_failures


def test_cross_investigation_citation_fails() -> None:
    quality = _build_quality(
        findings=(_finding(cites=("ev-s1", "ev-foreign")),),
        citation_owners={"ev-foreign": OTHER_INV},
    )
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_CROSS_INVESTIGATION_EVIDENCE_CITATION in score.gate_failures


def test_model_telemetry_gate_fail_fails() -> None:
    score = _score(telemetry_gate=GATE_FAIL)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_MODEL_TELEMETRY_GATE_NOT_PASS in score.gate_failures


def test_tool_evidence_gate_fail_fails() -> None:
    quality = replace(_build_quality(), gate_status=GATE_FAIL)
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_TOOL_EVIDENCE_GATE_NOT_PASS in score.gate_failures


def test_execution_not_completed_fails() -> None:
    score = _score(execution=_execution(completed=False))
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_EXECUTION_NOT_COMPLETED in score.gate_failures


def test_result_missing_fails() -> None:
    score = _score(
        result=_result(present=False, disposition=None, confidence=None, finding_ids=())
    )
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_INVESTIGATION_RESULT_MISSING in score.gate_failures
    assert FAIL_VERDICT_MISMATCH in score.gate_failures


def test_oracle_firewall_violation_fails() -> None:
    quality = replace(_build_quality(), oracle_firewall_pass=False)
    score = _score(quality=quality)
    assert score.correctness_gate == GATE_FAIL
    assert FAIL_ORACLE_FIREWALL_VIOLATION in score.gate_failures


# --- informational-only efficiency (§7) --------------------------------------


def test_confidence_is_informational_and_never_gates() -> None:
    # Confidence 0.0 (or None) never affects the gate on an otherwise-correct chain.
    low = _score(result=_result(confidence=0.0))
    assert low.correctness_gate == GATE_PASS
    assert low.confidence == 0.0
    none = _score(result=_result(confidence=None))
    assert none.correctness_gate == GATE_PASS
    assert none.confidence is None


def test_tokens_are_none_when_provider_reports_none() -> None:
    score = _score(usage_records=())
    assert score.input_tokens is None
    assert score.output_tokens is None
    assert score.total_tokens is None


def test_tokens_are_summed_when_provider_reports_them() -> None:
    usage = (
        {"outcome": "ok", "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        {"outcome": "ok", "input_tokens": 20, "output_tokens": 7, "total_tokens": 27},
    )
    score = _score(usage_records=usage)
    assert score.input_tokens == 30
    assert score.output_tokens == 12
    assert score.total_tokens == 42
    assert score.model_calls == 2
    # Efficiency never changes the gate.
    assert score.correctness_gate == GATE_PASS


# --- artifact schema / atomicity / determinism / allowlist --------------------


def test_schema_version_constant() -> None:
    assert SCORE_SCHEMA_VERSION == "evaluation-score/v1"
    assert EvaluationScore().schema_version == SCORE_SCHEMA_VERSION


def test_payload_round_trip() -> None:
    score = _score()
    restored = EvaluationScore.from_payload(score.to_payload())
    assert restored.to_payload() == score.to_payload()


def test_unknown_schema_rejected() -> None:
    payload = _score().to_payload()
    payload["schema_version"] = "evaluation-score/v99"
    with pytest.raises(ScoreSchemaError):
        EvaluationScore.from_payload(payload)


def test_artifact_atomic_write_and_read(tmp_path: Path) -> None:
    score = _score()
    path = score_path(tmp_path, "run-1", "exec-1")
    write_score(path, score)
    assert path.is_file()
    assert read_score(path).to_payload() == score.to_payload()
    assert sorted(p.name for p in path.parent.iterdir()) == ["score.json"]


def test_scoring_is_deterministic() -> None:
    # Scoring twice on unchanged inputs yields a semantically identical payload.
    first = _score().to_payload()
    second = _score().to_payload()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_readback_reapplies_allowlist_against_tampering() -> None:
    payload = _score().to_payload()
    payload["api_key"] = "sk-INJECTED"
    payload["raw_prompt"] = "ignore previous instructions"
    payload["sneaky"] = "x"
    restored = EvaluationScore.from_payload(payload)
    dumped = json.dumps(restored.to_payload())
    for leaked in ("api_key", "sk-INJECTED", "raw_prompt", "sneaky"):
        assert leaked not in dumped
    # The oracle-derived, allowlisted evaluation fields remain.
    assert "MALICIOUS" in dumped


def test_payload_never_leaks_secrets() -> None:
    text = json.dumps(_score().to_payload())
    for forbidden in (
        "api_key",
        "CMD_API_KEY",
        "Authorization",
        "Bearer",
        "password",
        "postgresql://",
        "sk-",
    ):
        assert forbidden not in text, f"score artifact leaked {forbidden!r}"


def test_provider_supplied_strings_are_bounded() -> None:
    huge = "A" * (MAX_FIELD_LEN * 5)
    score = _score(execution=_execution(execution_id=huge))
    assert len(score.execution_id) == MAX_FIELD_LEN
