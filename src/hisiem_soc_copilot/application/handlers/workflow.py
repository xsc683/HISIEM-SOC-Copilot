"""Investigation workflow command handler (orchestrator/system commands).

Thin orchestration over the read-only Investigation loop: load the aggregate by
id → validate domain rules → persist child rows → commit. No SQL, no ORM, no
infrastructure imports here (python-package-boundary.md §8).

The handler is stateless: every public method creates a FRESH UnitOfWork from the
injected factory, runs exactly ONE transaction, and closes it. This lets one
handler instance serve a whole LangGraph run (each node = one transaction) rather
than owning a single request-scoped session.

Domain events: workflow events (HypothesisRegistered, EvidenceRecorded,
FindingRecorded, ...) accumulate on the aggregate's ``_pending_events`` exactly as
the lifecycle handler already does for start/cancel; their ``domain_event`` /
``outbox_message`` persistence is a future outbox/dispatcher round.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from ...domain.investigation import events as ev
from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.content import compute_content_hash, compute_dedup_key
from ...domain.investigation.entities import (
    AttackMapping,
    EntityRef,
    Evidence,
    EvidenceSource,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    HypothesisAssessmentEvidence,
    InvestigationResult,
    PlanRevision,
    PlanStep,
    ResponseRecommendation,
    Uncertainty,
    Verdict,
)
from ...domain.investigation.enums import (
    EvidenceRelation,
    EvidenceSourceType,
    HypothesisStatus,
    InvestigationStatus,
    PlanStepStatus,
    ProvenanceAuthority,
    TerminationReason,
    VerdictDisposition,
)
from ...domain.investigation.events import (
    evidence_recorded,
    finding_recorded,
    hypothesis_assessed,
    hypothesis_registered,
    investigation_plan_revised,
    investigation_result_finalized,
)
from ...domain.shared.identifiers import utc_now
from ..commands.investigation import (
    AssessHypotheses,
    ChangeInvestigationPhase,
    CompleteInvestigation,
    EvidenceObservation,
    FailInvestigation,
    FinalizeInvestigationResult,
    RecordEvidenceBatch,
    RecordFindings,
    RegisterHypotheses,
    ReviseInvestigationPlan,
    StartInvestigation,
)
from ..errors import NotFoundError
from ..ports.unit_of_work import UnitOfWork
from .durable_support import run_idempotent_command


class _CommandT(Protocol):
    @property
    def tenant_id(self) -> str: ...

    @property
    def investigation_id(self) -> UUID: ...

    @property
    def command_id(self) -> UUID: ...

    @property
    def idempotency_key(self) -> str | None: ...

    @property
    def correlation_id(self) -> UUID | None: ...

    @property
    def causation_id(self) -> UUID | None: ...


class InvestigationWorkflowHandler:
    """Coordinates the read-only investigation commands against the UoW."""

    def __init__(
        self, *, unit_of_work_factory: Callable[[], UnitOfWork]
    ) -> None:
        self._uow_factory = unit_of_work_factory

    def _meta(self, command: _CommandT) -> dict[str, Any]:
        """Common run_idempotent_command kwargs derived from a command."""
        return {
            "tenant_id": command.tenant_id,
            "investigation_id": command.investigation_id,
            "command_type": type(command).__name__,
            "command_id": command.command_id,
            "idempotency_key": command.idempotency_key,
            "correlation_id": command.correlation_id,
            "causation_id": command.causation_id,
            "actor_subject_id": None,
        }

    # ------------------------------------------------------------------
    # public command entry points (one fresh transaction each)
    # ------------------------------------------------------------------
    async def change_phase(self, command: ChangeInvestigationPhase) -> Investigation:
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> Investigation:
                inv.update_phase(command.phase)
                await uow.investigations.update(inv)
                return inv

            async def _replay(inv: Investigation) -> Investigation:
                return inv

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def revise_plan(
        self, command: ReviseInvestigationPlan
    ) -> tuple[Investigation, PlanRevision]:
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> tuple[Investigation, PlanRevision]:
                now = utc_now()
                plan = PlanRevision(
                    id=uuid4(),
                    investigation_id=inv.id,
                    revision=command.revision,
                    goal=command.goal,
                    steps=[
                        PlanStep(
                            step_id=step.step_key,
                            objective=step.objective,
                            ordinal=step.ordinal,
                            status=PlanStepStatus.PENDING,
                        )
                        for step in command.steps
                    ],
                    generated_by="system",
                    created_at=now,
                )
                inv.current_plan_revision = command.revision
                self._emit(
                    inv,
                    investigation_plan_revised(
                        aggregate_id=inv.id,
                        plan_revision_id=plan.id,
                        revision=command.revision,
                        tenant_id=inv.tenant_id,
                    ),
                )
                await uow.plan_revisions.add(plan)
                await uow.investigations.update(inv)
                return inv, plan

            async def _replay(
                inv: Investigation,
            ) -> tuple[Investigation, PlanRevision]:
                revisions = await uow.plan_revisions.list_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )
                for existing in revisions:
                    if existing.revision == command.revision:
                        return inv, existing
                raise NotFoundError(
                    "plan revision not found on receipt replay",
                    resource_type="plan_revision",
                    resource_id=str(command.revision),
                )

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def register_hypotheses(
        self, command: RegisterHypotheses
    ) -> tuple[Investigation, list[Hypothesis]]:
        uow = self._uow_factory()
        try:
            async def _apply(
                inv: Investigation,
            ) -> tuple[Investigation, list[Hypothesis]]:
                now = utc_now()
                hypotheses: list[Hypothesis] = []
                for candidate in command.hypotheses:
                    statement = candidate.statement.strip()
                    if not statement:
                        raise ValueError("hypothesis statement must not be empty")
                    hypothesis = Hypothesis(
                        id=uuid4(),
                        investigation_id=inv.id,
                        statement=statement,
                        status=HypothesisStatus.OPEN,
                        created_at=now,
                        updated_at=now,
                    )
                    hypotheses.append(hypothesis)
                    self._emit(
                        inv,
                        hypothesis_registered(
                            aggregate_id=inv.id,
                            hypothesis_id=hypothesis.id,
                            statement=statement,
                            tenant_id=inv.tenant_id,
                        ),
                    )
                    await uow.hypotheses.add(hypothesis)
                return inv, hypotheses

            async def _replay(
                inv: Investigation,
            ) -> tuple[Investigation, list[Hypothesis]]:
                return inv, await uow.hypotheses.list_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def record_evidence_batch(
        self, command: RecordEvidenceBatch
    ) -> tuple[Investigation, list[Evidence]]:
        uow = self._uow_factory()
        try:
            async def _apply(
                inv: Investigation,
            ) -> tuple[Investigation, list[Evidence]]:
                now = utc_now()
                recorded: list[Evidence] = []
                if command.observations:
                    dedup_keys = [
                        compute_dedup_key(
                            source_provider=obs.source_provider,
                            source_operation=obs.source_operation,
                            raw_reference=obs.raw_reference,
                        )
                        for obs in command.observations
                    ]
                    existing = await uow.evidence.find_existing_dedup_keys(
                        investigation_id=inv.id, dedup_keys=dedup_keys
                    )
                    for obs, dedup_key in zip(
                        command.observations, dedup_keys, strict=True
                    ):
                        if dedup_key in existing:
                            continue
                        evidence = self._evidence_from_observation(
                            inv, obs, dedup_key, now
                        )
                        recorded.append(evidence)
                        existing.add(dedup_key)
                        self._emit(
                            inv,
                            evidence_recorded(
                                aggregate_id=inv.id,
                                evidence_ids=[evidence.id],
                                source_provider=evidence.source.provider,
                                source_operation=evidence.source.operation,
                                tenant_id=inv.tenant_id,
                            ),
                        )
                        await uow.evidence.add(evidence)
                return inv, recorded

            async def _replay(
                inv: Investigation,
            ) -> tuple[Investigation, list[Evidence]]:
                return inv, []

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def assess_hypotheses(
        self, command: AssessHypotheses
    ) -> tuple[Investigation, list[HypothesisAssessment]]:
        uow = self._uow_factory()
        try:
            async def _apply(
                inv: Investigation,
            ) -> tuple[Investigation, list[HypothesisAssessment]]:
                now = utc_now()
                by_id = {
                    h.id: h
                    for h in await uow.hypotheses.list_by_investigation(
                        tenant_id=command.tenant_id, investigation_id=inv.id
                    )
                }
                evidence_ids = {
                    rel.evidence_id
                    for assessment in command.assessments
                    for rel in assessment.evidence_relations
                }
                existing = await uow.evidence.find_by_ids(
                    tenant_id=command.tenant_id,
                    investigation_id=inv.id,
                    evidence_ids=list(evidence_ids),
                )
                existing_ids = {e.id for e in existing}
                if not evidence_ids.issubset(existing_ids):
                    missing = sorted(str(x) for x in evidence_ids - existing_ids)
                    raise ValueError(
                        "assessment references unknown evidence for this "
                        f"investigation: {missing}"
                    )

                assessments: list[HypothesisAssessment] = []
                for candidate in command.assessments:
                    hypothesis = by_id.get(candidate.hypothesis_id)
                    if hypothesis is None:
                        raise ValueError(
                            f"unknown hypothesis {candidate.hypothesis_id} for "
                            "this investigation"
                        )
                    status = HypothesisStatus(candidate.status)
                    if status in (
                        HypothesisStatus.SUPPORTED,
                        HypothesisStatus.CONTRADICTED,
                    ) and not candidate.evidence_relations:
                        raise ValueError(
                            "SUPPORTED/CONTRADICTED assessment requires an "
                            "EvidenceRelation"
                        )
                    # Directional invariant (strict grounding): SUPPORTED needs at
                    # least one SUPPORTS relation; CONTRADICTED needs at least one
                    # CONTRADICTS. CONTEXT / wrong-direction relations alone can
                    # never ground a firm hypothesis verdict.
                    if status == HypothesisStatus.SUPPORTED and not any(
                        rel.relation == EvidenceRelation.SUPPORTS
                        for rel in candidate.evidence_relations
                    ):
                        raise ValueError(
                            "SUPPORTED assessment requires at least one SUPPORTS "
                            "EvidenceRelation"
                        )
                    if status == HypothesisStatus.CONTRADICTED and not any(
                        rel.relation == EvidenceRelation.CONTRADICTS
                        for rel in candidate.evidence_relations
                    ):
                        raise ValueError(
                            "CONTRADICTED assessment requires at least one "
                            "CONTRADICTS EvidenceRelation"
                        )
                    revision = hypothesis.assessment_revision + 1
                    relations = [
                        HypothesisAssessmentEvidence(
                            evidence_id=rel.evidence_id, relation=rel.relation
                        )
                        for rel in candidate.evidence_relations
                    ]
                    assessment = HypothesisAssessment(
                        id=uuid4(),
                        hypothesis_id=hypothesis.id,
                        investigation_id=inv.id,
                        revision=revision,
                        status=status,
                        evidence_relations=relations,
                        reason_summary=candidate.reason_summary,
                        created_at=now,
                    )
                    assessments.append(assessment)
                    self._emit(
                        inv,
                        hypothesis_assessed(
                            aggregate_id=inv.id,
                            hypothesis_id=hypothesis.id,
                            assessment_id=assessment.id,
                            revision=revision,
                            status=status.value,
                            tenant_id=inv.tenant_id,
                        ),
                    )
                    await uow.hypothesis_assessments.add(assessment)
                    await uow.hypothesis_assessments.add_evidence_links(
                        assessment.id,
                        [(rel.evidence_id, rel.relation.value) for rel in relations],
                    )
                    await uow.hypothesis_assessments.update_hypothesis_status(
                        hypothesis_id=hypothesis.id,
                        status=status.value,
                        assessment_revision=revision,
                    )
                return inv, assessments

            async def _replay(
                inv: Investigation,
            ) -> tuple[Investigation, list[HypothesisAssessment]]:
                return inv, await uow.hypothesis_assessments.list_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def record_findings(
        self, command: RecordFindings
    ) -> tuple[Investigation, list[Finding]]:
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> tuple[Investigation, list[Finding]]:
                now = utc_now()
                findings: list[Finding] = []
                for candidate in command.findings:
                    if not candidate.evidence_citations:
                        raise ValueError("Finding must cite at least one Evidence")
                    evidence = await uow.evidence.find_by_ids(
                        tenant_id=command.tenant_id,
                        investigation_id=inv.id,
                        evidence_ids=candidate.evidence_citations,
                    )
                    if len(evidence) != len(candidate.evidence_citations):
                        cited = {e.id for e in evidence}
                        missing = sorted(
                            str(x)
                            for x in candidate.evidence_citations
                            if x not in cited
                        )
                        raise ValueError(f"Finding cites unknown evidence: {missing}")
                    finding = Finding(
                        id=uuid4(),
                        investigation_id=inv.id,
                        statement=candidate.statement.strip(),
                        evidence_citations=[e.id for e in evidence],
                        created_at=now,
                    )
                    findings.append(finding)
                    self._emit(
                        inv,
                        finding_recorded(
                            aggregate_id=inv.id,
                            finding_id=finding.id,
                            statement=finding.statement,
                            evidence_ids=finding.evidence_citations,
                            tenant_id=inv.tenant_id,
                        ),
                    )
                    await uow.findings.add(finding)
                return inv, findings

            async def _replay(inv: Investigation) -> tuple[Investigation, list[Finding]]:
                return inv, await uow.findings.list_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def finalize_result(
        self, command: FinalizeInvestigationResult
    ) -> tuple[Investigation, InvestigationResult]:
        uow = self._uow_factory()
        try:
            async def _apply(
                inv: Investigation,
            ) -> tuple[Investigation, InvestigationResult]:
                if inv.status != InvestigationStatus.RUNNING:
                    raise ValueError(
                        "Investigation must be RUNNING to finalize a result"
                    )
                existing = await uow.results.get_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )
                if existing is not None:
                    # Idempotent: a finalized result is immutable and never replaced.
                    return inv, existing

                disposition = command.verdict.disposition
                findings = await self._grounded_findings(
                    uow, inv, command.finding_ids, command.tenant_id
                )
                if disposition in (
                    VerdictDisposition.MALICIOUS,
                    VerdictDisposition.BENIGN,
                ) and not findings:
                    raise ValueError(
                        "MALICIOUS/BENIGN verdict requires at least one grounded "
                        "Finding"
                    )
                if (
                    disposition == VerdictDisposition.INCONCLUSIVE
                    and not command.uncertainties
                ):
                    raise ValueError(
                        "INCONCLUSIVE verdict requires at least one Uncertainty "
                        "explanation"
                    )

                result = InvestigationResult(
                    id=uuid4(),
                    investigation_id=inv.id,
                    verdict=Verdict(
                        disposition=disposition,
                        summary=command.verdict.summary,
                        confidence=command.verdict.confidence,
                    ),
                    finding_ids=[f.id for f in findings],
                    uncertainties=[
                        Uncertainty(
                            description=u.description,
                            missing_information=u.missing_information,
                        )
                        for u in command.uncertainties
                    ],
                    attack_mappings=[
                        AttackMapping(
                            framework=m.framework,
                            technique_id=m.technique_id,
                            name=m.name,
                            version=m.version,
                            source=m.source,
                        )
                        for m in command.attack_mappings
                    ],
                    response_recommendations=[
                        ResponseRecommendation(
                            description=r.description, reason=r.reason
                        )
                        for r in command.response_recommendations
                    ],
                    created_at=utc_now(),
                    content_hash=compute_content_hash(_result_payload(command)),
                )
                inv.result_id = result.id
                self._emit(
                    inv,
                    investigation_result_finalized(
                        aggregate_id=inv.id,
                        result_id=result.id,
                        verdict_disposition=disposition.value,
                        confidence=result.verdict.confidence,
                        finding_ids=result.finding_ids,
                        tenant_id=inv.tenant_id,
                    ),
                )
                await uow.results.add(result)
                await uow.investigations.update(inv)
                return inv, result

            async def _replay(
                inv: Investigation,
            ) -> tuple[Investigation, InvestigationResult]:
                result = await uow.results.get_by_investigation(
                    tenant_id=command.tenant_id, investigation_id=inv.id
                )
                if result is None:
                    raise NotFoundError(
                        "result not found on receipt replay",
                        resource_type="investigation_result",
                        resource_id=str(inv.id),
                    )
                return inv, result

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def complete(self, command: CompleteInvestigation) -> Investigation:
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> Investigation:
                inv.complete_without_response()
                await uow.investigations.update(inv)
                return inv

            async def _replay(inv: Investigation) -> Investigation:
                return inv

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def fail(self, command: FailInvestigation) -> Investigation:
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> Investigation:
                inv.fail(reason=TerminationReason(command.reason))
                await uow.investigations.update(inv)
                return inv

            async def _replay(inv: Investigation) -> Investigation:
                return inv

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    async def start_investigation(self, command: StartInvestigation) -> Investigation:
        """CREATED → RUNNING (issued by the durable runner on outbox dispatch).

        Idempotent: the node sets a deterministic key; a duplicate dispatch that
        already started the investigation simply replays the RUNNING aggregate.
        """
        uow = self._uow_factory()
        try:
            async def _apply(inv: Investigation) -> Investigation:
                if inv.status == InvestigationStatus.CREATED:
                    inv.start(actor=inv.initiated_by)
                await uow.investigations.update(inv)
                return inv

            async def _replay(inv: Investigation) -> Investigation:
                return inv

            result = await run_idempotent_command(
                uow=uow, apply=_apply, replay=_replay, **self._meta(command)
            )
            await uow.commit()
            return result
        finally:
            await uow.close()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _emit(self, investigation: Investigation, event: ev.InvestigationEvent) -> None:
        # Workflow events accumulate on the aggregate; run_idempotent_command
        # flushes them (as domain_event + outbox rows) atomically with the commit.
        investigation._pending_events.append(event)

    @staticmethod
    async def _grounded_findings(
        uow: UnitOfWork,
        investigation: Investigation,
        finding_ids: list[UUID],
        tenant_id: str,
    ) -> list[Finding]:
        if not finding_ids:
            return []
        findings = await uow.findings.list_by_investigation(
            tenant_id=tenant_id, investigation_id=investigation.id
        )
        by_id = {f.id: f for f in findings}
        missing = [str(x) for x in finding_ids if x not in by_id]
        if missing:
            raise ValueError(f"Result references unknown findings: {missing}")
        return [by_id[x] for x in finding_ids]

    @staticmethod
    def _evidence_from_observation(
        investigation: Investigation,
        obs: EvidenceObservation,
        dedup_key: str,
        now: datetime,
    ) -> Evidence:
        content_hash = compute_content_hash(obs.observation)
        return Evidence(
            id=uuid4(),
            investigation_id=investigation.id,
            source=EvidenceSource(
                type=EvidenceSourceType(obs.source_type),
                provider=obs.source_provider,
                operation=obs.source_operation,
            ),
            collected_at=now,
            observation=obs.observation,
            source_tool_call_id=obs.source_tool_invocation_id,
            observed_at=obs.observed_at,
            summary=_observation_summary(obs),
            raw_reference=obs.raw_reference,
            entity_refs=[
                EntityRef(kind=item["kind"], value=item["value"])
                for item in obs.entity_refs
            ],
            provenance_authority=ProvenanceAuthority(obs.provenance_authority),
            content_hash=content_hash,
            dedup_key=dedup_key,
        )


def _result_payload(command: FinalizeInvestigationResult) -> dict[str, object]:
    return {
        "verdict_disposition": command.verdict.disposition.value,
        "verdict_summary": command.verdict.summary,
        "confidence": command.verdict.confidence,
        "finding_ids": [str(f) for f in command.finding_ids],
        "uncertainties": [u.description for u in command.uncertainties],
        "attack_mappings": [m.technique_id for m in command.attack_mappings],
    }


def _observation_summary(obs: EvidenceObservation) -> str | None:
    text = _str_of(obs.observation.get("message")) or _str_of(
        obs.observation.get("description")
    )
    if not text and obs.observation:
        text = str(obs.observation)[:200]
    return text[:500] if text else None


def _str_of(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
