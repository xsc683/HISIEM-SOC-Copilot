"""E1-C3 tool/evidence investigation quality gate unit tests (E1-C3 §11/§13).

Covers the pure evaluator + the bounded sidecar artifact:

- the S1 identity match (exact index + document_id), wrong-document_id, wrong-index
- the successful ``hisiem.search_events`` invocation requirement (a FAILED one can
  never satisfy the gate)
- the Evidence → ToolInvocation same-investigation link
- Finding → S1 Evidence grounding, dangling + cross-investigation citations
- W1 control-event rejection
- the "COMPLETED + model gate PASS but no S1 → FAIL" and "S1 present but no
  grounded Finding → FAIL" negative cases
- the complete golden chain → PASS
- schema round-trip, atomic write, allowlist/secret boundary, unknown-schema guard
- evaluation-side S1/W1 resolution from a real sealed manifest + the oracle
  firewall check
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hisiem_soc_copilot.evaluation_harness.quality import (
    EXPECTED_SOURCE_OPERATION,
    EXPECTED_SOURCE_PROVIDER,
    EXPECTED_SOURCE_TYPE,
    GATE_FAIL,
    GATE_PASS,
    MAX_FIELD_LEN,
    QUALITY_SCHEMA_VERSION,
    SEARCH_EVENTS_TOOL_NAME,
    STATUS_SUCCEEDED,
    EvidenceFact,
    FindingFact,
    QualitySchemaError,
    ToolEvidenceQuality,
    ToolInvocationFact,
    build_tool_evidence_quality,
    read_tool_evidence_quality,
    tool_evidence_quality_path,
    write_tool_evidence_quality,
)
from hisiem_soc_copilot.evaluation_harness.quality_harness import (
    _oracle_firewall_pass,
    _secret_scan_pass,
    resolve_scenario_identities,
    verify_dataset_manifest,
)
from tests.unit.evaluation_harness._seal_helpers import seal_dataset

INV = "11111111-1111-1111-1111-111111111111"
OTHER_INV = "22222222-2222-2222-2222-222222222222"
S1_INDEX = "siem-events-gp01"
S1_DOC = "es-doc-S1-run-1"
W1_INDEX = "siem-events-gp01"
W1_DOC = "es-doc-W1-run-1"


def _s1_evidence(
    eid: str = "ev-s1",
    *,
    investigation: str = INV,
    tool_id: str | None = "tool-1",
    index: str | None = S1_INDEX,
    doc: str | None = S1_DOC,
    query_fp: str | None = "fp",
    content_hash: str | None = "hash",
    source_type: str = EXPECTED_SOURCE_TYPE,
    provider: str = EXPECTED_SOURCE_PROVIDER,
    operation: str = EXPECTED_SOURCE_OPERATION,
) -> EvidenceFact:
    return EvidenceFact(
        evidence_id=eid,
        investigation_id=investigation,
        source_type=source_type,
        source_provider=provider,
        source_operation=operation,
        source_tool_invocation_id=tool_id,
        raw_index=index,
        raw_document_id=doc,
        raw_query_fingerprint=query_fp,
        content_hash=content_hash,
    )


def _inv(
    iid: str = "tool-1",
    *,
    investigation: str = INV,
    tool: str = SEARCH_EVENTS_TOOL_NAME,
    status: str = STATUS_SUCCEEDED,
) -> ToolInvocationFact:
    return ToolInvocationFact(
        invocation_id=iid,
        investigation_id=investigation,
        tool_name=tool,
        status=status,
    )


def _finding(
    fid: str = "find-1", *, investigation: str = INV, cites: tuple[str, ...] = ("ev-s1",)
) -> FindingFact:
    return FindingFact(
        finding_id=fid, investigation_id=investigation, cited_evidence_ids=cites
    )


def _build(**overrides: object) -> ToolEvidenceQuality:
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
        "expected_control_index": W1_INDEX,
        "expected_control_document_id": W1_DOC,
        "evidence": (_s1_evidence(),),
        "findings": (_finding(),),
        "tool_invocations": (_inv(),),
        "citation_owners": {},
    }
    kwargs.update(overrides)
    return build_tool_evidence_quality(**kwargs)  # type: ignore[arg-type]


# --- complete golden chain ----------------------------------------------------


def test_complete_golden_chain_passes() -> None:
    quality = _build()
    assert quality.gate_status == GATE_PASS
    assert quality.gate_failures == ()
    assert quality.quality_attempt_valid is True
    assert quality.matched_s1_evidence_ids == ("ev-s1",)
    assert quality.findings_citing_s1_evidence == ("find-1",)
    assert quality.successful_search_events_invocation_count == 1
    # Verdict disposition is NOT part of the gate (§11).
    assert quality.oracle_firewall_pass is True
    assert quality.secret_scan_pass is True


# --- S1 identity match --------------------------------------------------------


def test_wrong_document_id_does_not_match() -> None:
    quality = _build(evidence=(_s1_evidence(doc="es-doc-S1-other"),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_S1_EVIDENCE" in quality.gate_failures
    assert quality.matched_s1_evidence_ids == ()


def test_wrong_index_does_not_match() -> None:
    quality = _build(evidence=(_s1_evidence(index="siem-events-other"),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_S1_EVIDENCE" in quality.gate_failures


def test_completed_gate_pass_but_no_s1_is_fail() -> None:
    """COMPLETED + model gate PASS but the Agent never produced S1 → E1-C3 FAIL."""
    quality = _build(evidence=(), findings=(), tool_invocations=(_inv(),))
    assert quality.gate_status == GATE_FAIL
    assert quality.quality_attempt_valid is True  # a VALID failure, not invalid
    assert "NO_S1_EVIDENCE" in quality.gate_failures


# --- successful search_events invocation --------------------------------------


def test_no_successful_search_events_invocation_fails() -> None:
    quality = _build(tool_invocations=(_inv(status="FAILED"),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_SUCCESSFUL_SEARCH_EVENTS_INVOCATION" in quality.gate_failures
    # ...and the S1 evidence cannot link to a successful invocation.
    assert "S1_EVIDENCE_TOOL_INVOCATION_NOT_LINKED" in quality.gate_failures


def test_failed_invocation_cannot_satisfy_gate() -> None:
    quality = _build(tool_invocations=(_inv(status="FAILED"),))
    assert quality.gate_status == GATE_FAIL
    assert quality.successful_search_events_invocation_count == 0


def test_non_search_tool_invocation_does_not_count() -> None:
    quality = _build(tool_invocations=(_inv(tool="hisiem.get_detection_rule"),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_SUCCESSFUL_SEARCH_EVENTS_INVOCATION" in quality.gate_failures


def test_evidence_to_tool_invocation_same_investigation_link() -> None:
    """S1 Evidence linked to a SUCCEEDED invocation in ANOTHER investigation is a
    broken link → FAIL (the link is scoped to the same Investigation)."""
    quality = _build(tool_invocations=(_inv(investigation=OTHER_INV),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_SUCCESSFUL_SEARCH_EVENTS_INVOCATION" in quality.gate_failures
    assert "S1_EVIDENCE_TOOL_INVOCATION_NOT_LINKED" in quality.gate_failures


def test_evidence_to_tool_invocation_link_by_distinct_invocation() -> None:
    """A SUCCEEDED search exists but the S1 evidence points at a DIFFERENT (failed)
    invocation id → the link is not satisfied."""
    quality = _build(
        tool_invocations=(_inv(iid="tool-ok"), _inv(iid="tool-bad", status="FAILED")),
        evidence=(_s1_evidence(tool_id="tool-bad"),),
    )
    assert quality.gate_status == GATE_FAIL
    assert "S1_EVIDENCE_TOOL_INVOCATION_NOT_LINKED" in quality.gate_failures


# --- provenance completeness --------------------------------------------------


@pytest.mark.parametrize(
    "override",
    [
        {"tool_id": None},
        {"query_fp": None},
        {"content_hash": None},
        {"provider": "other"},
        {"operation": "get_alert"},
        {"source_type": "MODEL_INFERENCE"},
    ],
)
def test_incomplete_s1_provenance_fails(override: dict[str, object]) -> None:
    quality = _build(evidence=(_s1_evidence(**override),))  # type: ignore[arg-type]
    assert quality.gate_status == GATE_FAIL
    assert "S1_EVIDENCE_PROVENANCE_INCOMPLETE" in quality.gate_failures


def test_evidence_in_other_investigation_fails_provenance() -> None:
    """An Evidence row whose index/document_id match but whose owning investigation
    differs fails the same-investigation provenance requirement."""
    quality = _build(evidence=(_s1_evidence(investigation=OTHER_INV),))
    assert quality.gate_status == GATE_FAIL
    assert "S1_EVIDENCE_PROVENANCE_INCOMPLETE" in quality.gate_failures


# --- Finding grounding --------------------------------------------------------


def test_s1_present_but_no_grounded_finding_fails() -> None:
    quality = _build(findings=())
    assert quality.gate_status == GATE_FAIL
    assert "NO_FINDING_CITES_S1" in quality.gate_failures


def test_finding_citing_other_evidence_does_not_ground_s1() -> None:
    quality = _build(findings=(_finding(cites=("ev-other",)),))
    assert quality.gate_status == GATE_FAIL
    assert "NO_FINDING_CITES_S1" in quality.gate_failures
    # The cited id is absent from this investigation's evidence AND the owners map
    # → a dangling citation is detected alongside the missing grounding.
    assert "DANGLING_EVIDENCE_CITATION" in quality.gate_failures


def test_dangling_citation_detection() -> None:
    quality = _build(
        findings=(_finding(cites=("ev-s1", "ev-missing")),),
    )
    assert quality.gate_status == GATE_FAIL
    assert quality.dangling_citation_count == 1
    assert "DANGLING_EVIDENCE_CITATION" in quality.gate_failures


def test_cross_investigation_citation_detection() -> None:
    quality = _build(
        findings=(_finding(cites=("ev-s1", "ev-foreign")),),
        citation_owners={"ev-foreign": OTHER_INV},
    )
    assert quality.gate_status == GATE_FAIL
    assert quality.cross_investigation_citation_count == 1
    assert "CROSS_INVESTIGATION_EVIDENCE_CITATION" in quality.gate_failures


def test_foreign_evidence_owner_equal_to_investigation_is_not_cross() -> None:
    """A cited id absent from the local evidence set but owned by THIS investigation
    (a read that raced the local projection) is neither dangling nor cross."""
    quality = _build(
        findings=(_finding(cites=("ev-s1", "ev-late")),),
        citation_owners={"ev-late": INV},
    )
    assert quality.gate_status == GATE_PASS
    assert quality.dangling_citation_count == 0
    assert quality.cross_investigation_citation_count == 0


# --- W1 control-event exclusion -----------------------------------------------


def test_w1_control_event_used_as_evidence_fails() -> None:
    w1 = _s1_evidence(
        eid="ev-w1", index=W1_INDEX, doc=W1_DOC, tool_id="tool-1"
    )
    quality = _build(evidence=(_s1_evidence(), w1))
    assert quality.gate_status == GATE_FAIL
    assert quality.control_event_evidence_ids == ("ev-w1",)
    assert "CONTROL_EVENT_USED_AS_EVIDENCE" in quality.gate_failures


# --- model telemetry / execution prerequisites --------------------------------


def test_model_telemetry_gate_fail_is_invalid_attempt() -> None:
    quality = _build(model_telemetry_gate=GATE_FAIL)
    assert quality.gate_status == GATE_FAIL
    assert quality.quality_attempt_valid is False
    assert "MODEL_TELEMETRY_GATE_NOT_PASS" in quality.gate_failures


def test_execution_not_completed_and_result_missing() -> None:
    quality = _build(execution_completed=False, result_present=False)
    assert quality.gate_status == GATE_FAIL
    assert quality.quality_attempt_valid is False
    assert "EXECUTION_NOT_COMPLETED" in quality.gate_failures
    assert "INVESTIGATION_RESULT_MISSING" in quality.gate_failures


def test_oracle_firewall_and_secret_scan_violations_fail() -> None:
    quality = _build(oracle_firewall_pass=False, secret_scan_pass=False)
    assert quality.gate_status == GATE_FAIL
    assert "ORACLE_FIREWALL_VIOLATION" in quality.gate_failures
    assert "SECRET_SCAN_VIOLATION" in quality.gate_failures


# --- artifact schema / atomicity / allowlist ----------------------------------


def test_payload_round_trip() -> None:
    quality = _build()
    restored = ToolEvidenceQuality.from_payload(quality.to_payload())
    assert restored.to_payload() == quality.to_payload()
    assert restored.gate_status == GATE_PASS


def test_unknown_schema_rejected() -> None:
    payload = _build().to_payload()
    payload["schema_version"] = "evaluation-tool-evidence-quality/v99"
    with pytest.raises(QualitySchemaError):
        ToolEvidenceQuality.from_payload(payload)


def test_schema_version_constant() -> None:
    assert QUALITY_SCHEMA_VERSION == "evaluation-tool-evidence-quality/v1"
    assert ToolEvidenceQuality().schema_version == QUALITY_SCHEMA_VERSION


def test_artifact_atomic_write_and_read(tmp_path: Path) -> None:
    quality = _build()
    path = tool_evidence_quality_path(tmp_path, "run-1", "exec-1")
    write_tool_evidence_quality(path, quality)
    assert path.is_file()
    restored = read_tool_evidence_quality(path)
    assert restored.to_payload() == quality.to_payload()
    # Atomic replace leaves no temp artifacts behind.
    assert sorted(p.name for p in path.parent.iterdir()) == ["tool-evidence-quality.json"]


def test_readback_reapplies_allowlist_against_tampering() -> None:
    payload = _build().to_payload()
    payload["api_key"] = "sk-INJECTED"
    payload["oracle"] = {"expected_verdict": "MALICIOUS"}
    payload["sneaky"] = "x"
    restored = ToolEvidenceQuality.from_payload(payload)
    dumped = json.dumps(restored.to_payload())
    # The tampered non-allowlisted keys/values are dropped entirely (the legitimate
    # ``oracle_firewall_pass`` allowlisted key remains).
    for leaked in ("api_key", "sk-INJECTED", "expected_verdict", "sneaky", "MALICIOUS"):
        assert leaked not in dumped


def test_payload_never_leaks_secrets_or_oracle() -> None:
    text = json.dumps(_build().to_payload())
    for forbidden in (
        "api_key",
        "CMD_API_KEY",
        "Authorization",
        "Bearer",
        "password",
        "postgresql://",
        "sk-",
    ):
        assert forbidden not in text, f"artifact leaked {forbidden!r}"


def test_provider_supplied_strings_are_bounded() -> None:
    huge = "A" * (MAX_FIELD_LEN * 5)
    quality = _build(execution_id=huge, expected_s1_document_id=huge)
    assert len(quality.execution_id) == MAX_FIELD_LEN
    assert len(quality.expected_s1_document_id) == MAX_FIELD_LEN


def test_secret_scan_helper() -> None:
    assert _secret_scan_pass(_build()) is True
    tainted = _build(execution_id="sk-SECRET")
    assert _secret_scan_pass(tainted) is False


# --- evaluation-side S1/W1 resolution + oracle firewall ------------------------


def test_resolve_scenario_identities_from_sealed_manifest(tmp_path: Path) -> None:
    runs_dir = tmp_path / "runs"
    manifest = seal_dataset(runs_dir=runs_dir, dataset_run_id="run-1")
    verified = verify_dataset_manifest(
        runs_dir / "gp-01" / "run-1" / "manifest.json"
    )
    ids = resolve_scenario_identities(verified)
    assert ids.required_role == "S1"
    assert ids.s1_index == "siem-events-gp01"
    assert ids.s1_document_id == "es-doc-S1-run-1"
    assert ids.control_index == "siem-events-gp01"
    assert ids.control_document_id == "es-doc-W1-run-1"
    # The sealed manifest's S1 doc id is what the fake dataset resolved.
    assert manifest.events[-1].document_id == ids.s1_document_id


def test_oracle_firewall_passes_clean_artifacts_and_flags_markers(
    tmp_path: Path,
) -> None:
    from hisiem_soc_copilot.evaluation_harness.record import (
        EvaluationExecutionRecord,
        ExecutionStatus,
        execution_artifact_path,
        rfc3339_utc,
        write_record,
    )

    executions_dir = tmp_path / "executions"
    record = EvaluationExecutionRecord(
        execution_id="exec-1",
        dataset_run_id="run-1",
        tenant_id="tenant-a",
        started_at=rfc3339_utc(),
        execution_status=ExecutionStatus.COMPLETED,
        result_disposition="MALICIOUS",
    )
    write_record(execution_artifact_path(executions_dir, "run-1", "exec-1"), record)
    assert _oracle_firewall_pass(executions_dir, "run-1", "exec-1") is True

    # A leaked oracle marker in the execution artifact is detected.
    artifact = execution_artifact_path(executions_dir, "run-1", "exec-1")
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    payload["required_evidence_roles"] = ["S1"]
    artifact.write_text(json.dumps(payload), encoding="utf-8")
    assert _oracle_firewall_pass(executions_dir, "run-1", "exec-1") is False


def test_report_tool_evidence_is_bounded() -> None:
    from hisiem_soc_copilot.evaluation_harness.quality_harness import (
        report_tool_evidence,
    )

    lines = report_tool_evidence(_build())
    joined = "\n".join(lines)
    assert "tool_evidence_quality_gate=PASS" in joined
