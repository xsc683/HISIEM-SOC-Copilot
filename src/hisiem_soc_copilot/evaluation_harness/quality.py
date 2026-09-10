"""E1-C3 tool/evidence investigation quality artifact (evaluation-only sidecar).

One E1-C3 evaluation execution persists a bounded quality sidecar in the SAME
execution directory as the execution + telemetry artifacts:

    <executions_dir>/gp-01/<dataset_run_id>/<execution_id>/
        execution.json              (schema evaluation-execution/v2 — unchanged)
        model-telemetry.json        (schema evaluation-model-telemetry/v1 — unchanged)
        tool-evidence-quality.json  (schema evaluation-tool-evidence-quality/v1)

E1-C3 asks a DIFFERENT question than E1-C2. E1-C2 proves a real validated model
call ran; E1-C3 proves the real Agent independently DISCOVERED the golden evidence
(the post-brute-force successful SSH authentication S1), persisted immutable
Evidence whose provenance traces to the exact sealed S1 provider event, linked
that Evidence to a SUCCEEDED ``hisiem.search_events`` ToolInvocation in the same
Investigation, and grounded a persisted Finding on it — with the W1 control event
excluded and no dangling/cross-investigation citations.

Evaluation-only. The sealed manifest is read HERE (after the production execution
has finished) to resolve the expected S1/W1 provider identity. Nothing in this
module ever reaches the graph/model/tool/executor/normalizer/production
repositories — the production launch projection stays the bounded four fields.

The payload is strictly bounded — never a raw event body, prompt, model response,
Evidence summary, credential, expected verdict, or F1-F5 labels. Only the S1/W1
provider identity (index/document_id) resolved on the evaluation side, booleans,
counts, and opaque UUID strings are persisted; every string is length-bounded and
every collection re-validated on read-back.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .record import atomic_write_json

QUALITY_SCHEMA_VERSION = "evaluation-tool-evidence-quality/v1"
_QUALITY_ARTIFACT = "tool-evidence-quality.json"

GATE_PASS = "PASS"
GATE_FAIL = "FAIL"

# The required agent-discovered evidence role (GP-01: the post-failure SSH
# success). Resolved from the sealed oracle — never hardcoded into production.
DEFAULT_REQUIRED_ROLE = "S1"
DEFAULT_CONTROL_ROLE = "W1"

# The exact Evidence provenance the matched S1 Evidence must carry.
EXPECTED_SOURCE_TYPE = "HISIEM_LOG_SEARCH"
EXPECTED_SOURCE_PROVIDER = "hisiem"
EXPECTED_SOURCE_OPERATION = "search_events"

# The ONE production tool whose invocation is required to have SUCCEEDED.
SEARCH_EVENTS_TOOL_NAME = "hisiem.search_events"
STATUS_SUCCEEDED = "SUCCEEDED"

# Hard bound for any persisted string (ids are opaque; this is a safety margin).
MAX_FIELD_LEN = 200

# The ONLY keys this artifact may persist (a real allowlist — never a pass-through
# of an internal evaluation object).
_PAYLOAD_KEYS: tuple[str, ...] = (
    "schema_version",
    "execution_id",
    "dataset_run_id",
    "investigation_id",
    "execution_completed",
    "result_present",
    "quality_attempt_valid",
    "model_telemetry_gate",
    "search_events_invocation_count",
    "successful_search_events_invocation_count",
    "evidence_count",
    "finding_count",
    "required_role",
    "expected_s1_index",
    "expected_s1_document_id",
    "expected_control_index",
    "expected_control_document_id",
    "matched_s1_evidence_ids",
    "findings_citing_s1_evidence",
    "control_event_evidence_ids",
    "dangling_citation_count",
    "cross_investigation_citation_count",
    "oracle_firewall_pass",
    "secret_scan_pass",
    "gate_status",
    "gate_failures",
)


class QualitySchemaError(ValueError):
    """A persisted quality payload carries an unsupported/legacy schema version."""


def tool_evidence_quality_path(
    executions_dir: str | Path, dataset_run_id: str, execution_id: str
) -> Path:
    """The ``tool-evidence-quality.json`` sidecar beside ``execution.json``."""
    return (
        Path(executions_dir)
        / "gp-01"
        / dataset_run_id
        / execution_id
        / _QUALITY_ARTIFACT
    )


# ---------------------------------------------------------------------------
# Bounded evaluation facts the evaluator consumes (production-agnostic inputs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceFact:
    """Bounded projection of one persisted Evidence row (evaluation-side view)."""

    evidence_id: str
    investigation_id: str
    source_type: str
    source_provider: str
    source_operation: str
    source_tool_invocation_id: str | None
    raw_index: str | None
    raw_document_id: str | None
    raw_query_fingerprint: str | None
    content_hash: str | None


@dataclass(frozen=True)
class ToolInvocationFact:
    """Bounded projection of one persisted ToolInvocation audit row."""

    invocation_id: str
    investigation_id: str
    tool_name: str
    status: str


@dataclass(frozen=True)
class FindingFact:
    """Bounded projection of one persisted Finding + its citation ids."""

    finding_id: str
    investigation_id: str
    cited_evidence_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolEvidenceQuality:
    """Bounded E1-C3 quality result + gate (immutable)."""

    schema_version: str = QUALITY_SCHEMA_VERSION
    execution_id: str = ""
    dataset_run_id: str = ""
    investigation_id: str = ""
    execution_completed: bool = False
    result_present: bool = False
    quality_attempt_valid: bool = False
    model_telemetry_gate: str = GATE_FAIL
    search_events_invocation_count: int = 0
    successful_search_events_invocation_count: int = 0
    evidence_count: int = 0
    finding_count: int = 0
    required_role: str = DEFAULT_REQUIRED_ROLE
    expected_s1_index: str = ""
    expected_s1_document_id: str = ""
    expected_control_index: str = ""
    expected_control_document_id: str = ""
    matched_s1_evidence_ids: tuple[str, ...] = ()
    findings_citing_s1_evidence: tuple[str, ...] = ()
    control_event_evidence_ids: tuple[str, ...] = ()
    dangling_citation_count: int = 0
    cross_investigation_citation_count: int = 0
    oracle_firewall_pass: bool = True
    secret_scan_pass: bool = True
    gate_status: str = GATE_FAIL
    gate_failures: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "execution_id": self.execution_id,
            "dataset_run_id": self.dataset_run_id,
            "investigation_id": self.investigation_id,
            "execution_completed": self.execution_completed,
            "result_present": self.result_present,
            "quality_attempt_valid": self.quality_attempt_valid,
            "model_telemetry_gate": self.model_telemetry_gate,
            "search_events_invocation_count": self.search_events_invocation_count,
            "successful_search_events_invocation_count": (
                self.successful_search_events_invocation_count
            ),
            "evidence_count": self.evidence_count,
            "finding_count": self.finding_count,
            "required_role": self.required_role,
            "expected_s1_index": self.expected_s1_index,
            "expected_s1_document_id": self.expected_s1_document_id,
            "expected_control_index": self.expected_control_index,
            "expected_control_document_id": self.expected_control_document_id,
            "matched_s1_evidence_ids": list(self.matched_s1_evidence_ids),
            "findings_citing_s1_evidence": list(self.findings_citing_s1_evidence),
            "control_event_evidence_ids": list(self.control_event_evidence_ids),
            "dangling_citation_count": self.dangling_citation_count,
            "cross_investigation_citation_count": (
                self.cross_investigation_citation_count
            ),
            "oracle_firewall_pass": self.oracle_firewall_pass,
            "secret_scan_pass": self.secret_scan_pass,
            "gate_status": self.gate_status,
            "gate_failures": list(self.gate_failures),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ToolEvidenceQuality:
        schema = payload.get("schema_version")
        if schema != QUALITY_SCHEMA_VERSION:
            raise QualitySchemaError(
                f"unsupported quality schema {schema!r}; this reader only accepts "
                f"{QUALITY_SCHEMA_VERSION}"
            )
        # Only allowlisted keys are ever read back; a tampered payload cannot
        # smuggle an arbitrary field into the reconstructed artifact.
        safe = {k: payload[k] for k in _PAYLOAD_KEYS if k in payload}
        return cls(
            execution_id=_bounded_str(safe.get("execution_id", "")),
            dataset_run_id=_bounded_str(safe.get("dataset_run_id", "")),
            investigation_id=_bounded_str(safe.get("investigation_id", "")),
            execution_completed=bool(safe.get("execution_completed", False)),
            result_present=bool(safe.get("result_present", False)),
            quality_attempt_valid=bool(safe.get("quality_attempt_valid", False)),
            model_telemetry_gate=_bounded_str(
                safe.get("model_telemetry_gate", GATE_FAIL)
            ),
            search_events_invocation_count=_bounded_int(
                safe.get("search_events_invocation_count", 0)
            ),
            successful_search_events_invocation_count=_bounded_int(
                safe.get("successful_search_events_invocation_count", 0)
            ),
            evidence_count=_bounded_int(safe.get("evidence_count", 0)),
            finding_count=_bounded_int(safe.get("finding_count", 0)),
            required_role=_bounded_str(
                safe.get("required_role", DEFAULT_REQUIRED_ROLE)
            ),
            expected_s1_index=_bounded_str(safe.get("expected_s1_index", "")),
            expected_s1_document_id=_bounded_str(
                safe.get("expected_s1_document_id", "")
            ),
            expected_control_index=_bounded_str(safe.get("expected_control_index", "")),
            expected_control_document_id=_bounded_str(
                safe.get("expected_control_document_id", "")
            ),
            matched_s1_evidence_ids=_bounded_id_tuple(
                safe.get("matched_s1_evidence_ids")
            ),
            findings_citing_s1_evidence=_bounded_id_tuple(
                safe.get("findings_citing_s1_evidence")
            ),
            control_event_evidence_ids=_bounded_id_tuple(
                safe.get("control_event_evidence_ids")
            ),
            dangling_citation_count=_bounded_int(
                safe.get("dangling_citation_count", 0)
            ),
            cross_investigation_citation_count=_bounded_int(
                safe.get("cross_investigation_citation_count", 0)
            ),
            oracle_firewall_pass=bool(safe.get("oracle_firewall_pass", True)),
            secret_scan_pass=bool(safe.get("secret_scan_pass", True)),
            gate_status=_bounded_str(safe.get("gate_status", GATE_FAIL)),
            gate_failures=_bounded_id_tuple(safe.get("gate_failures")),
        )


# ---------------------------------------------------------------------------
# Bounded coercion helpers
# ---------------------------------------------------------------------------


def _bounded_str(value: Any) -> str:
    return str(value)[:MAX_FIELD_LEN]


def _bounded_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _bounded_id_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (str, bytes)):
        return (_bounded_str(value),)
    return tuple(_bounded_str(item) for item in value)


# ---------------------------------------------------------------------------
# The pure E1-C3 evaluator + gate
# ---------------------------------------------------------------------------


def _provenance_complete(fact: EvidenceFact, investigation_id: str) -> bool:
    return (
        fact.investigation_id == investigation_id
        and fact.source_type == EXPECTED_SOURCE_TYPE
        and fact.source_provider == EXPECTED_SOURCE_PROVIDER
        and fact.source_operation == EXPECTED_SOURCE_OPERATION
        and bool(fact.source_tool_invocation_id)
        and bool(fact.raw_index)
        and bool(fact.raw_document_id)
        and bool(fact.raw_query_fingerprint)
        and bool(fact.content_hash)
    )


def _matches_identity(
    fact: EvidenceFact, index: str, document_id: str
) -> bool:
    return bool(document_id) and fact.raw_index == index and fact.raw_document_id == document_id


def build_tool_evidence_quality(
    *,
    execution_id: str,
    dataset_run_id: str,
    investigation_id: str,
    execution_completed: bool,
    result_present: bool,
    model_telemetry_gate: str,
    required_role: str,
    expected_s1_index: str,
    expected_s1_document_id: str,
    expected_control_index: str,
    expected_control_document_id: str,
    evidence: Sequence[EvidenceFact] = (),
    findings: Sequence[FindingFact] = (),
    tool_invocations: Sequence[ToolInvocationFact] = (),
    citation_owners: Mapping[str, str] | None = None,
    oracle_firewall_pass: bool = True,
    secret_scan_pass: bool = True,
) -> ToolEvidenceQuality:
    """Evaluate the E1-C3 gate from bounded persisted facts (pure, no IO).

    Gate PASS (§11) only when EVERY condition holds: the E1-C2 model telemetry gate
    PASSed, the execution COMPLETED with a persisted InvestigationResult, >= 1
    SUCCEEDED ``hisiem.search_events`` invocation, >= 1 persisted Evidence matching
    the exact S1 index/document_id with complete provenance linking to a SUCCEEDED
    invocation in the same Investigation, >= 1 Finding citing that S1 Evidence, all
    Finding citations resolve within the same Investigation, the W1 control event is
    not used as Evidence, and the oracle-firewall / secret-scan invariants hold.

    ``citation_owners`` maps a cited Evidence id (for ids NOT in this
    Investigation's evidence set) to its owning investigation_id within the tenant;
    a cited id absent from BOTH is dangling, present with a foreign owner is a
    cross-investigation citation. The verdict disposition is deliberately NOT part
    of the gate (informational only — scoring is E1-C4).
    """
    failures: list[str] = []
    owners: Mapping[str, str] = citation_owners or {}

    if model_telemetry_gate != GATE_PASS:
        failures.append("MODEL_TELEMETRY_GATE_NOT_PASS")
    if not execution_completed:
        failures.append("EXECUTION_NOT_COMPLETED")
    if not result_present:
        failures.append("INVESTIGATION_RESULT_MISSING")
    if not oracle_firewall_pass:
        failures.append("ORACLE_FIREWALL_VIOLATION")
    if not secret_scan_pass:
        failures.append("SECRET_SCAN_VIOLATION")

    attempt_valid = (
        execution_completed
        and result_present
        and model_telemetry_gate == GATE_PASS
    )

    same_inv_invocations = [
        t
        for t in tool_invocations
        if t.investigation_id == investigation_id
        and t.tool_name == SEARCH_EVENTS_TOOL_NAME
    ]
    succeeded = [t for t in same_inv_invocations if t.status == STATUS_SUCCEEDED]
    succeeded_ids = {t.invocation_id for t in succeeded}
    if not succeeded:
        failures.append("NO_SUCCESSFUL_SEARCH_EVENTS_INVOCATION")

    identity_matched = [
        e
        for e in evidence
        if _matches_identity(e, expected_s1_index, expected_s1_document_id)
    ]
    matched_s1_ids = tuple(e.evidence_id for e in identity_matched)

    if not identity_matched:
        failures.append("NO_S1_EVIDENCE")
    else:
        provenance_complete = [
            e for e in identity_matched if _provenance_complete(e, investigation_id)
        ]
        if not provenance_complete:
            failures.append("S1_EVIDENCE_PROVENANCE_INCOMPLETE")
        else:
            grounded = [
                e
                for e in provenance_complete
                if e.source_tool_invocation_id in succeeded_ids
            ]
            if not grounded:
                failures.append("S1_EVIDENCE_TOOL_INVOCATION_NOT_LINKED")

    grounded_ids = {
        e.evidence_id
        for e in evidence
        if _matches_identity(e, expected_s1_index, expected_s1_document_id)
        and _provenance_complete(e, investigation_id)
        and e.source_tool_invocation_id in succeeded_ids
    }

    findings_citing_s1 = tuple(
        f.finding_id
        for f in findings
        if f.investigation_id == investigation_id
        and grounded_ids.intersection(f.cited_evidence_ids)
    )
    if not findings_citing_s1:
        failures.append("NO_FINDING_CITES_S1")

    # Citation lineage across ALL persisted findings in this investigation.
    own_evidence_ids = {e.evidence_id for e in evidence}
    dangling = 0
    cross = 0
    for finding in findings:
        if finding.investigation_id != investigation_id:
            continue
        for cited in finding.cited_evidence_ids:
            if cited in own_evidence_ids:
                continue
            owner = owners.get(cited)
            if owner is None:
                dangling += 1
            elif owner != investigation_id:
                cross += 1
    if dangling:
        failures.append("DANGLING_EVIDENCE_CITATION")
    if cross:
        failures.append("CROSS_INVESTIGATION_EVIDENCE_CITATION")

    control_event_ids = tuple(
        e.evidence_id
        for e in evidence
        if _matches_identity(e, expected_control_index, expected_control_document_id)
    )
    if control_event_ids:
        failures.append("CONTROL_EVENT_USED_AS_EVIDENCE")

    return ToolEvidenceQuality(
        execution_id=_bounded_str(execution_id),
        dataset_run_id=_bounded_str(dataset_run_id),
        investigation_id=_bounded_str(investigation_id),
        execution_completed=bool(execution_completed),
        result_present=bool(result_present),
        quality_attempt_valid=attempt_valid,
        model_telemetry_gate=_bounded_str(model_telemetry_gate),
        search_events_invocation_count=len(same_inv_invocations),
        successful_search_events_invocation_count=len(succeeded),
        evidence_count=len(evidence),
        finding_count=len(findings),
        required_role=_bounded_str(required_role),
        expected_s1_index=_bounded_str(expected_s1_index),
        expected_s1_document_id=_bounded_str(expected_s1_document_id),
        expected_control_index=_bounded_str(expected_control_index),
        expected_control_document_id=_bounded_str(expected_control_document_id),
        matched_s1_evidence_ids=matched_s1_ids,
        findings_citing_s1_evidence=findings_citing_s1,
        control_event_evidence_ids=control_event_ids,
        dangling_citation_count=dangling,
        cross_investigation_citation_count=cross,
        oracle_firewall_pass=bool(oracle_firewall_pass),
        secret_scan_pass=bool(secret_scan_pass),
        gate_status=GATE_PASS if not failures else GATE_FAIL,
        gate_failures=tuple(failures),
    )


def write_tool_evidence_quality(
    path: str | Path, quality: ToolEvidenceQuality
) -> None:
    """Atomically persist the quality sidecar (never sealed)."""
    atomic_write_json(path, quality.to_payload())


def read_tool_evidence_quality(path: str | Path) -> ToolEvidenceQuality:
    """Load + validate a persisted tool-evidence-quality artifact."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return ToolEvidenceQuality.from_payload(payload)
