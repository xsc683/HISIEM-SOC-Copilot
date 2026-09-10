"""E1-C4 scoring harness — READ-ONLY fact collection + deterministic scoring.

Reuses the persisted artifacts of ONE execution (``execution.json`` /
``model-telemetry.json`` / ``tool-evidence-quality.json``) and the persisted
``InvestigationResult`` / ``Finding`` rows (through READ-ONLY repository ports),
runs the pure :func:`score_gp01`, and writes ``score.json`` beside them.

The ``score-execution`` command is strictly a SCORER (§15): it NEVER runs the Agent,
NEVER calls a model, NEVER calls a tool, and NEVER mutates a production row. Running
it twice on unchanged inputs yields a semantically identical score (no generated
timestamps are part of the score payload).

Oracle firewall (§4/§18): the sealed manifest (expected verdict + required evidence
roles) is read HERE, on the evaluation side. It is recorded in the evaluation-only
``score.json`` but never reaches the production artifacts, the graph, a prompt, or a
tool.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..evaluation.contracts import SealedManifest
from .harness import dataset_manifest_path, verify_dataset_manifest
from .quality import (
    DEFAULT_REQUIRED_ROLE,
    GATE_FAIL,
    FindingFact,
    ToolEvidenceQuality,
    read_tool_evidence_quality,
    tool_evidence_quality_path,
)
from .quality_harness import (
    _finding_fact,
    _invocation_fact,
    _oracle_firewall_pass,
)
from .record import (
    EvaluationExecutionRecord,
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from .score import (
    EvaluationScore,
    ExecutionFact,
    OracleExpectation,
    ResultFact,
    score_gp01,
    score_path,
    write_score,
)
from .telemetry import (
    ModelTelemetry,
    model_telemetry_path,
    read_model_telemetry,
)

if TYPE_CHECKING:
    from ..bootstrap.container import Container

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoringOutcome:
    """Everything ONE scored execution yields (score + the facts that classify it)."""

    score: EvaluationScore
    record: EvaluationExecutionRecord
    telemetry: ModelTelemetry | None
    quality: ToolEvidenceQuality | None
    score_artifact: Path


def oracle_expectation(manifest: SealedManifest) -> OracleExpectation:
    """Resolve the machine-authoritative expectation from the sealed oracle (§5)."""
    roles = manifest.oracle.required_evidence_roles or (DEFAULT_REQUIRED_ROLE,)
    return OracleExpectation(
        expected_verdict=str(manifest.oracle.expected_verdict),
        required_evidence_roles=tuple(str(r) for r in roles),
    )


def _duration_ms(record: EvaluationExecutionRecord) -> int | None:
    """Execution wall-clock duration from the record's start/finish instants."""
    if not record.started_at or not record.finished_at:
        return None
    try:
        start = datetime.fromisoformat(record.started_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(record.finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0, int((end - start).total_seconds() * 1000))


def _empty_quality() -> ToolEvidenceQuality:
    """A fail-closed quality stand-in when an execution has no E1-C3 sidecar."""
    return ToolEvidenceQuality()


async def _read_scoring_facts(
    container: Container, *, tenant_id: str, investigation_id: UUID
) -> tuple[ResultFact, tuple[FindingFact, ...], dict[str, str], int, int]:
    """Read the InvestigationResult + Findings INVOLVED in the result (§2, §4).

    READ-ONLY. Returns the result projection, ALL persisted Findings of the
    investigation (so result→finding integrity is checked against the real rows), the
    tenant-wide owner map for result finding ids NOT among this investigation's own,
    and the tool-invocation counts (total + ``hisiem.search_events``).
    """
    from .quality import SEARCH_EVENTS_TOOL_NAME

    uow = container.unit_of_work()
    try:
        result = await uow.results.get_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )
        finding_rows = await uow.findings.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )
        invocation_rows = await uow.tool_invocations.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )

        finding_facts = tuple(_finding_fact(f) for f in finding_rows)
        invocation_facts = tuple(_invocation_fact(t) for t in invocation_rows)
        tool_calls = len(invocation_facts)
        search_events_calls = sum(
            1 for t in invocation_facts if t.tool_name == SEARCH_EVENTS_TOOL_NAME
        )

        if result is None:
            result_fact = ResultFact(
                present=False,
                investigation_id=str(investigation_id),
                disposition=None,
                confidence=None,
                finding_ids=(),
            )
        else:
            result_fact = ResultFact(
                present=True,
                investigation_id=str(result.investigation_id),
                disposition=result.verdict.disposition.value,
                confidence=result.verdict.confidence,
                finding_ids=tuple(str(f) for f in result.finding_ids),
            )

        own_finding_ids = {f.finding_id for f in finding_facts}
        unresolved: list[UUID] = []
        for fid in result_fact.finding_ids:
            if fid in own_finding_ids:
                continue
            try:
                unresolved.append(UUID(fid))
            except (ValueError, AttributeError):
                continue  # not a UUID → cannot resolve → stays dangling
        owners = await uow.findings.find_investigation_ids_by_finding_ids(
            tenant_id=tenant_id, finding_ids=unresolved
        )
        owner_map = {str(k): str(v) for k, v in owners.items()}
    finally:
        await uow.close()
    return result_fact, finding_facts, owner_map, tool_calls, search_events_calls


async def score_execution(
    *,
    dataset_run_id: str,
    execution_id: str,
    settings: Any,
    container: Container | None = None,
) -> ScoringOutcome:
    """Score ONE persisted execution deterministically (§15). READ-ONLY + offline.

    Never runs the Agent, never calls a model/tool, never mutates a production row.
    Reads the persisted execution/telemetry/quality artifacts, reads the persisted
    InvestigationResult/Findings through repository ports, scores, and writes
    ``score.json`` (deterministically replacing any prior score for this execution).
    """
    executions_dir = settings.evaluation.executions_dir
    record = read_record(
        execution_artifact_path(executions_dir, dataset_run_id, execution_id)
    )

    telemetry_artifact = model_telemetry_path(executions_dir, dataset_run_id, execution_id)
    telemetry = (
        read_model_telemetry(telemetry_artifact)
        if telemetry_artifact.is_file()
        else None
    )

    quality_artifact = tool_evidence_quality_path(
        executions_dir, dataset_run_id, execution_id
    )
    quality = (
        read_tool_evidence_quality(quality_artifact)
        if quality_artifact.is_file()
        else None
    )

    completed = record.execution_status == ExecutionStatus.COMPLETED
    investigation_id = record.investigation_id or ""

    result_fact = ResultFact(
        present=False,
        investigation_id=investigation_id,
        disposition=None,
        confidence=None,
        finding_ids=(),
    )
    findings: tuple[FindingFact, ...] = ()
    owner_map: dict[str, str] = {}
    tool_calls = 0
    search_events_calls = 0

    if completed and investigation_id:
        owned = container is None
        read_container = container or await _open_read_container(settings)
        try:
            (
                result_fact,
                findings,
                owner_map,
                tool_calls,
                search_events_calls,
            ) = await _read_scoring_facts(
                read_container,
                tenant_id=record.tenant_id,
                investigation_id=UUID(investigation_id),
            )
        finally:
            if owned:
                await read_container.close()

    # If the result read raced/absent, fall back to the record's disposition so a
    # COMPLETED execution with a persisted but unreadable result is still scored
    # honestly (result_present stays False when there is genuinely no result).
    if not result_fact.present and completed and record.result_disposition is not None:
        result_fact = ResultFact(
            present=True,
            investigation_id=investigation_id,
            disposition=record.result_disposition,
            confidence=record.result_confidence,
            finding_ids=(),
        )

    manifest = verify_dataset_manifest(
        dataset_manifest_path(settings.evaluation, dataset_run_id)
    )
    oracle = oracle_expectation(manifest)

    oracle_firewall_pass = _oracle_firewall_pass(
        executions_dir, dataset_run_id, execution_id
    )

    effective_quality = quality
    if effective_quality is None:
        effective_quality = _empty_quality()
    elif oracle_firewall_pass != effective_quality.oracle_firewall_pass:
        # Keep the freshly assessed firewall verdict — the score is authoritative.
        effective_quality = replace(
            effective_quality, oracle_firewall_pass=oracle_firewall_pass
        )

    telemetry_gate = telemetry.gate_status if telemetry is not None else GATE_FAIL
    usage_records: tuple[dict[str, Any], ...] = (
        telemetry.usage_records if telemetry is not None else ()
    )

    execution_fact = ExecutionFact(
        execution_id=execution_id,
        dataset_run_id=dataset_run_id,
        completed=completed,
        evidence_count=record.evidence_count,
        finding_count=record.finding_count,
        tool_calls=tool_calls,
        search_events_calls=search_events_calls,
        duration_ms=_duration_ms(record),
    )

    score = score_gp01(
        oracle=oracle,
        result=result_fact,
        quality=effective_quality,
        findings=findings,
        result_finding_owners=owner_map,
        telemetry_gate=telemetry_gate,
        usage_records=usage_records,
        execution=execution_fact,
    )

    artifact = score_path(executions_dir, dataset_run_id, execution_id)
    write_score(artifact, score)

    return ScoringOutcome(
        score=score,
        record=record,
        telemetry=telemetry,
        quality=quality,
        score_artifact=artifact,
    )


async def _open_read_container(settings: Any) -> Container:
    """Open a fresh read-only Container for the post-execution scoring read."""
    from ..bootstrap.container import Container

    container = Container(settings)
    await container.open()
    return container


def report_score(score: EvaluationScore) -> list[str]:
    """A concise human-readable E1-C4 score report."""
    lines = [
        f"e1c4_execution_id={score.execution_id}",
        f"e1c4_investigation_id={score.investigation_id}",
        f"e1c4_expected_verdict={score.expected_verdict} "
        f"actual_verdict={score.actual_verdict} match={score.verdict_match}",
        f"e1c4_required_roles={','.join(score.required_evidence_roles)} "
        f"matched={','.join(score.matched_evidence_roles)} "
        f"coverage={score.evidence_coverage}",
        f"e1c4_grounded_required_evidence={score.grounded_required_evidence}",
        f"e1c4_grounded_findings_in_result="
        f"{','.join(score.grounded_findings_in_result)}",
        f"e1c4_result_finding_integrity={score.result_finding_integrity_pass} "
        f"control_exclusion={score.control_exclusion_pass} "
        f"citation_integrity={score.citation_integrity_pass}",
        f"e1c4_oracle_firewall={score.oracle_firewall_pass}",
        f"e1c4_efficiency model_calls={score.model_calls} "
        f"tool_calls={score.tool_calls} search_events={score.search_events_calls} "
        f"duration_ms={score.duration_ms} total_tokens={score.total_tokens} "
        f"confidence={score.confidence}",
        f"e1c4_correctness_gate={score.correctness_gate}",
    ]
    if score.gate_failures:
        lines.append(f"e1c4_gate_failures={','.join(score.gate_failures)}")
    return lines


async def score_execution_cli(*, dataset_run_id: str, execution_id: str) -> int:
    """Operator-facing E1-C4 driver: score EXACTLY ONE persisted execution (§15).

    Reads + scores only — never runs the Agent / a model / a tool. Returns 0 only
    when the E1-C4 correctness gate PASSES.
    """
    from ..config import get_settings

    outcome = await score_execution(
        dataset_run_id=dataset_run_id, execution_id=execution_id, settings=get_settings()
    )
    for line in report_score(outcome.score):
        print(line)
    print(f"score_artifact={outcome.score_artifact}")
    return 0 if outcome.score.correctness_gate == "PASS" else 1
