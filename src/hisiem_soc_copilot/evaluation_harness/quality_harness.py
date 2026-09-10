"""E1-C3 tool/evidence investigation quality harness.

Reuses the E1-C2 real-model orchestration UNCHANGED (``execute_real_model_run``)
and, AFTER the production execution finishes, evaluates the persisted
investigation through READ-ONLY repository ports against the sealed manifest's
expected S1 (and W1 control) provider identity — writing the
``tool-evidence-quality.json`` sidecar beside the execution + telemetry artifacts.

Oracle firewall (E1-C3 §4): the sealed manifest is read HERE, on the evaluation
side, only after the production run completes. The production execution received
only the bounded launch projection (provider/resource_type/address_id/business_id)
— the expected S1/W1 identity, oracle, and event ids never entered the
graph/model/prompt/tool/executor/normalizer/production repositories.

Provider-transient policy (§12): the E1-C3 gate is only meaningfully assessed when
the E1-C2 model telemetry gate PASSed and the execution COMPLETED. A telemetry FAIL
(the Command Code 503 flake) is an INVALID quality attempt — the sidecar records it
with ``quality_attempt_valid=False`` so the operator keeps the artifact, re-runs
after provider recovery, and never misreads a provider outage as an Agent quality
failure. A telemetry PASS + quality FAIL is a VALID failure and must NOT be retried
until it happens to find S1.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

from ..evaluation.contracts import SealedManifest
from .harness import (
    dataset_manifest_path,
    execute_real_model_run,
    verify_dataset_manifest,
)
from .quality import (
    DEFAULT_CONTROL_ROLE,
    DEFAULT_REQUIRED_ROLE,
    GATE_FAIL,
    GATE_PASS,
    EvidenceFact,
    FindingFact,
    ToolEvidenceQuality,
    ToolInvocationFact,
    build_tool_evidence_quality,
    tool_evidence_quality_path,
    write_tool_evidence_quality,
)
from .record import (
    ExecutionStatus,
    execution_artifact_path,
    read_record,
)
from .telemetry import model_telemetry_path

if TYPE_CHECKING:
    from ..bootstrap.container import Container

logger = logging.getLogger(__name__)

# Failure code when the sealed manifest cannot resolve the required S1 identity.
CAT_S1_UNRESOLVABLE = "SOURCE_DATASET_RUNTIME_MISSING"

# Tokens that must NEVER appear in a production artifact (oracle firewall). The
# expectation identities are evaluation-only; their absence from the production
# execution + telemetry artifacts is the structural firewall proof.
_ORACLE_MARKERS: tuple[str, ...] = (
    "expected_verdict",
    "required_evidence_roles",
    "control_events",
    '"oracle"',
    '"S1"',
    '"W1"',
    '"F1"',
    '"F2"',
    '"F3"',
    '"F4"',
    '"F5"',
)

# Tokens that must NEVER appear in the quality artifact itself (secret scan).
_SECRET_MARKERS: tuple[str, ...] = (
    "api_key",
    "CMD_API_KEY",
    "Authorization",
    "Bearer",
    "password",
    "postgresql://",
    "sk-",
)


class QualityEvaluationError(RuntimeError):
    """A bounded E1-C3 evaluation failure (never carries secrets/oracle data)."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ScenarioIdentities:
    """Evaluation-side resolution of the expected S1 + W1 provider identity."""

    required_role: str
    s1_index: str
    s1_document_id: str
    control_index: str
    control_document_id: str


@dataclass(frozen=True)
class ToolEvidenceRunResult:
    """Outcome of ONE E1-C3 orchestration (§3/§11).

    ``exit_code`` is 0 only when the E1-C3 gate PASSED. ``quality`` is always
    populated once the quality phase ran (None only for a pre-open fail-closed
    where no execution directory exists).
    """

    exit_code: int
    execution_id: str | None
    quality: ToolEvidenceQuality | None


# ---------------------------------------------------------------------------
# S1 / W1 resolution (evaluation side only — §6, §10)
# ---------------------------------------------------------------------------


def resolve_scenario_identities(manifest: SealedManifest) -> ScenarioIdentities:
    """Resolve the expected S1 (required) + W1 (control) index/document_id.

    Uses the typed sealed contracts only: the required role comes from the sealed
    oracle, and the exact provider identity is read from the resolved ground-truth
    event / control event — never guessed, never derived from a role label or
    prose. Fails closed when the required role is absent from the manifest.
    """
    required_roles = manifest.oracle.required_evidence_roles or (DEFAULT_REQUIRED_ROLE,)
    required_role = required_roles[0]
    s1 = next(
        (e for e in manifest.events if e.logical_role == required_role), None
    )
    if s1 is None:
        raise QualityEvaluationError(
            f"sealed manifest does not resolve the required evidence role "
            f"{required_role!r}",
            code=CAT_S1_UNRESOLVABLE,
        )
    control_role = manifest.scenario.control_role or DEFAULT_CONTROL_ROLE
    w1 = next(
        (e for e in manifest.control_events if e.logical_role == control_role), None
    )
    return ScenarioIdentities(
        required_role=required_role,
        s1_index=s1.index,
        s1_document_id=s1.document_id,
        control_index=w1.index if w1 is not None else "",
        control_document_id=w1.document_id if w1 is not None else "",
    )


# ---------------------------------------------------------------------------
# Read-only fact projection through repository ports (§7, §8, §9)
# ---------------------------------------------------------------------------


def _evidence_fact(evidence: Any) -> EvidenceFact:
    raw = evidence.raw_reference or {}
    tool_id = evidence.source_tool_call_id
    return EvidenceFact(
        evidence_id=str(evidence.id),
        investigation_id=str(evidence.investigation_id),
        source_type=str(evidence.source.type.value),
        source_provider=evidence.source.provider,
        source_operation=evidence.source.operation,
        source_tool_invocation_id=str(tool_id) if tool_id is not None else None,
        raw_index=raw.get("index"),
        raw_document_id=raw.get("document_id"),
        raw_query_fingerprint=raw.get("query_fingerprint"),
        content_hash=evidence.content_hash,
    )


def _finding_fact(finding: Any) -> FindingFact:
    return FindingFact(
        finding_id=str(finding.id),
        investigation_id=str(finding.investigation_id),
        cited_evidence_ids=tuple(str(c) for c in finding.evidence_citations),
    )


def _invocation_fact(record: Any) -> ToolInvocationFact:
    return ToolInvocationFact(
        invocation_id=str(record.id),
        investigation_id=str(record.investigation_id),
        tool_name=record.tool_name,
        status=record.status,
    )


async def _read_quality_facts(
    container: Container, *, tenant_id: str, investigation_id: UUID
) -> tuple[
    tuple[EvidenceFact, ...],
    tuple[FindingFact, ...],
    tuple[ToolInvocationFact, ...],
    dict[str, str],
]:
    """Read Evidence / Finding / ToolInvocation facts through repository ports.

    Strictly READ-ONLY (no mutation). The cross-investigation citation lineage is
    resolved by one tenant-wide Evidence-id lookup for the cited ids that are NOT
    part of this Investigation's own evidence set.
    """
    uow = container.unit_of_work()
    try:
        evidence_rows = await uow.evidence.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )
        finding_rows = await uow.findings.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )
        invocation_rows = await uow.tool_invocations.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation_id
        )

        evidence_facts = tuple(_evidence_fact(e) for e in evidence_rows)
        finding_facts = tuple(_finding_fact(f) for f in finding_rows)
        invocation_facts = tuple(_invocation_fact(t) for t in invocation_rows)

        own_ids = {e.evidence_id for e in evidence_facts}
        cited: set[str] = set()
        for finding in finding_facts:
            cited.update(finding.cited_evidence_ids)
        unresolved_uuids: list[UUID] = []
        for cited_id in cited - own_ids:
            try:
                unresolved_uuids.append(UUID(cited_id))
            except (ValueError, AttributeError):
                continue  # not a UUID → cannot resolve → stays dangling
        owners = await uow.evidence.find_investigation_ids_by_evidence_ids(
            tenant_id=tenant_id, evidence_ids=unresolved_uuids
        )
        owner_map = {str(k): str(v) for k, v in owners.items()}
    finally:
        await uow.close()
    return evidence_facts, finding_facts, invocation_facts, owner_map


# ---------------------------------------------------------------------------
# Oracle-firewall + secret-scan invariants (§11)
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _oracle_firewall_pass(
    executions_dir: str | Path, dataset_run_id: str, execution_id: str
) -> bool:
    """True when no production artifact leaked an oracle marker (§4).

    The production execution + telemetry artifacts must never carry the sealed
    oracle, the required-evidence-role set, control events, or any F1-F5/S1/W1
    role label — they are evaluation-only.
    """
    execution_text = _read_text(
        execution_artifact_path(executions_dir, dataset_run_id, execution_id)
    )
    telemetry_text = _read_text(
        model_telemetry_path(executions_dir, dataset_run_id, execution_id)
    )
    for text in (execution_text, telemetry_text):
        if any(marker in text for marker in _ORACLE_MARKERS):
            return False
    return True


def _secret_scan_pass(quality: ToolEvidenceQuality) -> bool:
    """True when the serialized quality payload carries no secret marker."""
    text = json.dumps(quality.to_payload())
    return not any(marker in text for marker in _SECRET_MARKERS)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


async def execute_tool_evidence_run(
    *,
    dataset_run_id: str,
    settings: Any,
    provider: Any | None = None,
    hisiem: Any | None = None,
    execution_code: tuple[str, bool] | None = None,
    container: Container | None = None,
) -> ToolEvidenceRunResult:
    """Run ONE E1-C3 execution, reusing the E1-C2 orchestration UNCHANGED (§3).

    Calls :func:`execute_real_model_run` (which enforces the exact Command Code
    baseline + dispatcher-disabled isolation BEFORE ``Container.open()`` and drives
    the REAL production pipeline), then evaluates the persisted investigation for
    the golden S1 evidence chain and persists ``tool-evidence-quality.json``.

    ``provider``/``hisiem``/``execution_code``/``container`` are injection seams for
    tests; the operator CLI passes none, so the production path is identical.
    """
    result = await execute_real_model_run(
        dataset_run_id=dataset_run_id,
        settings=settings,
        provider=provider,
        hisiem=hisiem,
        execution_code=execution_code,
    )
    execution_id = result.execution_id
    if execution_id is None:
        return ToolEvidenceRunResult(exit_code=1, execution_id=None, quality=None)

    executions_dir = settings.evaluation.executions_dir
    record = read_record(execution_artifact_path(executions_dir, dataset_run_id, execution_id))
    investigation_id = record.investigation_id or ""
    execution_completed = record.execution_status == ExecutionStatus.COMPLETED
    result_present = execution_completed and record.result_disposition is not None
    telemetry_gate = (
        result.telemetry.gate_status if result.telemetry is not None else GATE_FAIL
    )

    # Resolve the expected S1/W1 identity on the EVALUATION side (§6, §10). A
    # manifest that cannot resolve the required role is a fail-closed evaluation
    # error — never a guess.
    manifest = verify_dataset_manifest(
        dataset_manifest_path(settings.evaluation, dataset_run_id)
    )
    identities = resolve_scenario_identities(manifest)

    evidence: tuple[EvidenceFact, ...] = ()
    findings: tuple[FindingFact, ...] = ()
    invocations: tuple[ToolInvocationFact, ...] = ()
    citation_owners: dict[str, str] = {}
    if execution_completed and investigation_id:
        # ``execute_real_model_run`` opened + closed its OWN container, so the
        # read-only quality pass opens a FRESH one unless the caller injected an
        # OPEN container (tests). The dispatcher is disabled by the E1-C2 isolation
        # gate (already enforced), so opening a container never launches a
        # background worker here.
        owned = container is None
        read_container = container or await _open_read_container(settings)
        try:
            (
                evidence,
                findings,
                invocations,
                citation_owners,
            ) = await _read_quality_facts(
                read_container,
                tenant_id=record.tenant_id,
                investigation_id=UUID(investigation_id),
            )
        finally:
            if owned:
                await read_container.close()

    oracle_firewall_pass = _oracle_firewall_pass(
        executions_dir, dataset_run_id, execution_id
    )

    def _build(secret_scan_pass: bool) -> ToolEvidenceQuality:
        return build_tool_evidence_quality(
            execution_id=execution_id,
            dataset_run_id=dataset_run_id,
            investigation_id=investigation_id,
            execution_completed=execution_completed,
            result_present=result_present,
            model_telemetry_gate=telemetry_gate,
            required_role=identities.required_role,
            expected_s1_index=identities.s1_index,
            expected_s1_document_id=identities.s1_document_id,
            expected_control_index=identities.control_index,
            expected_control_document_id=identities.control_document_id,
            evidence=evidence,
            findings=findings,
            tool_invocations=invocations,
            citation_owners=citation_owners,
            oracle_firewall_pass=oracle_firewall_pass,
            secret_scan_pass=secret_scan_pass,
        )

    # secret_scan_pass is self-referential (it scans this artifact); compute it
    # against the default-True payload and re-build on violation so the persisted
    # artifact records the true result.
    quality = _build(secret_scan_pass=True)
    if not _secret_scan_pass(quality):
        quality = _build(secret_scan_pass=False)

    quality_artifact = tool_evidence_quality_path(
        executions_dir, dataset_run_id, execution_id
    )
    write_tool_evidence_quality(quality_artifact, quality)

    for line in report_tool_evidence(quality):
        print(line)
    print(f"tool_evidence_quality_artifact={quality_artifact}")

    exit_code = 0 if quality.gate_status == GATE_PASS else 1
    return ToolEvidenceRunResult(
        exit_code=exit_code, execution_id=execution_id, quality=quality
    )


async def _open_read_container(settings: Any) -> Container:
    """Open a fresh read-only Container for the post-execution quality read."""
    from ..bootstrap.container import Container

    container = Container(settings)
    await container.open()
    return container


def report_tool_evidence(quality: ToolEvidenceQuality) -> list[str]:
    """A concise human-readable E1-C3 quality report."""
    lines = [
        f"e1c3_execution_id={quality.execution_id}",
        f"e1c3_investigation_id={quality.investigation_id}",
        f"e1c3_execution_completed={quality.execution_completed} "
        f"result_present={quality.result_present}",
        f"e1c3_quality_attempt_valid={quality.quality_attempt_valid}",
        f"e1c3_model_telemetry_gate={quality.model_telemetry_gate}",
        f"e1c3_search_events total={quality.search_events_invocation_count} "
        f"succeeded={quality.successful_search_events_invocation_count}",
        f"e1c3_expected_s1_index={quality.expected_s1_index} "
        f"document_id={quality.expected_s1_document_id}",
        f"e1c3_matched_s1_evidence_ids={','.join(quality.matched_s1_evidence_ids)}",
        f"e1c3_findings_citing_s1={','.join(quality.findings_citing_s1_evidence)}",
        f"e1c3_control_event_evidence_ids={','.join(quality.control_event_evidence_ids)}",
        f"e1c3_dangling_citations={quality.dangling_citation_count} "
        f"cross_investigation_citations={quality.cross_investigation_citation_count}",
        f"e1c3_oracle_firewall={quality.oracle_firewall_pass} "
        f"secret_scan={quality.secret_scan_pass}",
        f"tool_evidence_quality_gate={quality.gate_status}",
    ]
    if quality.gate_failures:
        lines.append(f"tool_evidence_quality_failures={','.join(quality.gate_failures)}")
    return lines


async def execute_tool_evidence_cli(*, dataset_run_id: str) -> int:
    """Operator-facing E1-C3 driver: reuse E1-C2 then evaluate the S1 chain.

    Thin wrapper over :func:`execute_tool_evidence_run` using the process settings,
    so the operator CLI and integration tests exercise the SAME orchestration path.
    Returns 0 only when the E1-C3 gate PASSES.
    """
    from ..config import get_settings

    result = await execute_tool_evidence_run(
        dataset_run_id=dataset_run_id, settings=get_settings()
    )
    return result.exit_code
