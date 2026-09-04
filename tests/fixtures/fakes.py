"""In-memory fakes of application ports, shared across application unit tests.

These satisfy the application repository protocols structurally. The fake stores
persisted domain objects in-memory and mirrors the invariant lookups the real
SQLAlchemy repositories implement (tenant scoping, dedup-key existence, finding
citations, hypothesis status updates, result-finding links).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

from hisiem_soc_copilot.application.ports.durable import (
    DomainEventEnvelope,
    DurableCommand,
    OrchestrationBinding,
    OutboxRecord,
    ToolInvocationRecord,
)
from hisiem_soc_copilot.application.ports.unit_of_work import UnitOfWork
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    InvestigationResult,
    PlanRevision,
)
from hisiem_soc_copilot.domain.investigation.events import InvestigationEvent
from hisiem_soc_copilot.domain.investigation.value_objects import ExternalResourceRef


@dataclass
class FakeOutboxStore:
    """In-memory outbox for dispatcher tests; keeps a PENDING/PROCESSING/PUBLISHED log.

    Claiming moves ready rows to PROCESSING and records them so tests can assert
    delivery happened exactly once.
    """

    rows: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    published_ids: list[UUID] = field(default_factory=list)
    failed_ids: list[UUID] = field(default_factory=list)
    _seq = 0

    def enqueue(
        self, event_id: UUID, *, destination: str = "investigation.graph.run"
    ) -> None:
        self._seq += 1
        self.rows[event_id] = {
            "id": uuid4(),
            "event_id": event_id,
            "destination": destination,
            "status": "PENDING",
            "attempt_count": 0,
        }

    async def claim_batch(
        self, *, worker: str, limit: int, available_before: Any
    ) -> list[OutboxRecord]:
        ready = [
            OutboxRecord(
                id=row["id"],
                event_id=row["event_id"],
                destination=row["destination"],
                status="PENDING",
                attempt_count=row["attempt_count"],
                available_at=available_before,
            )
            for row in self.rows.values()
            if row["status"] == "PENDING"
        ][:limit]
        for rec in ready:
            row = self.rows[rec.event_id]
            row["status"] = "PROCESSING"
            row["attempt_count"] += 1
        return ready

    async def mark_published(self, *, outbox_id: UUID, published_at: Any) -> bool:
        for row in self.rows.values():
            if row["status"] == "PROCESSING" and row["id"] == outbox_id:
                row["status"] = "PUBLISHED"
                self.published_ids.append(outbox_id)
                return True
        return False

    async def mark_failed(
        self, *, outbox_id: UUID, error_code: str, next_available_at: Any
    ) -> bool:
        for row in self.rows.values():
            if row["status"] == "PROCESSING" and row["id"] == outbox_id:
                row["status"] = "FAILED"
                self.failed_ids.append(outbox_id)
                return True
        return False

    async def settle_dead_letter(self, *, outbox_id: UUID, error_code: str) -> bool:
        for row in self.rows.values():
            if row["status"] == "PROCESSING" and row["id"] == outbox_id:
                row["status"] = "DEAD"
                return True
        return False




@dataclass
class FakeEventLedger:
    """In-memory domain_event store (no real outbox dispatch)."""

    events: list[InvestigationEvent] = field(default_factory=list)
    revisions: dict[UUID, int] = field(default_factory=dict)

    async def append(self, event: InvestigationEvent, *, aggregate_revision: int) -> None:
        self.events.append(event)
        self.revisions[event.aggregate_id] = aggregate_revision

    async def get(self, *, event_id: UUID) -> DomainEventEnvelope | None:
        for event in self.events:
            if event.event_id == event_id:
                return DomainEventEnvelope(
                    event_id=event.event_id,
                    event_type=event.event_type,
                    aggregate_type=event.aggregate_type,
                    aggregate_id=event.aggregate_id,
                    tenant_id=event.tenant_id or "",
                    correlation_id=event.correlation_id,
                    causation_id=event.causation_id,
                    actor_subject_id=event.actor_subject_id,
                    payload=event.payload,
                    occurred_at=event.occurred_at,
                )
        return None

    def by_investigation(self, investigation_id: UUID) -> list[InvestigationEvent]:
        return [
            e for e in self.events if e.aggregate_id == investigation_id
        ]


@dataclass
class FakeCommandReceiptStore:
    """In-memory command_receipt store."""

    _receipts: dict[str, DurableCommand] = field(default_factory=dict)

    async def exists(self, *, idempotency_key: str) -> bool:
        return idempotency_key in self._receipts

    async def record(self, receipt: DurableCommand) -> None:
        self._receipts[receipt.idempotency_key] = receipt

    async def get_safe_result(self, *, idempotency_key: str) -> dict[str, Any] | None:
        receipt = self._receipts.get(idempotency_key)
        return dict(receipt.safe_result) if receipt and receipt.safe_result else None

    def receipts(self) -> list[DurableCommand]:
        return list(self._receipts.values())


@dataclass
class FakeOrchestrationBindingStore:
    """In-memory investigation↔thread binding store."""

    bindings: dict[UUID, OrchestrationBinding] = field(default_factory=dict)

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> OrchestrationBinding | None:
        return self.bindings.get(investigation_id)

    async def get_by_thread_id(self, *, thread_id: str) -> OrchestrationBinding | None:
        for binding in self.bindings.values():
            if binding.thread_id == thread_id:
                return binding
        return None

    async def put(self, binding: OrchestrationBinding) -> None:
        self.bindings[binding.investigation_id] = binding


@dataclass
class FakeToolInvocationStore:
    """In-memory tool_invocation audit store."""

    rows: dict[tuple[UUID, str], ToolInvocationRecord] = field(default_factory=dict)

    async def add_started(
        self, *, tenant_id: str, record: ToolInvocationRecord
    ) -> None:
        key = (record.investigation_id, record.idempotency_key)
        if key not in self.rows:
            self.rows[key] = record

    async def finish(
        self,
        *,
        tenant_id: str,
        investigation_id: UUID,
        idempotency_key: str,
        status: str,
        finished_at: Any,
        error_code: str | None = None,
        safe_error_message: str | None = None,
        result_metadata: dict[str, Any] | None = None,
    ) -> None:
        if status not in ("SUCCEEDED", "FAILED"):
            raise ValueError(f"tool invocation must finish SUCCEEDED or FAILED, got {status}")
        key = (investigation_id, idempotency_key)
        if key not in self.rows:
            raise KeyError(f"tool invocation {investigation_id}/{idempotency_key} not found")
        existing = self.rows[key]
        self.rows[key] = ToolInvocationRecord(
            id=existing.id,
            investigation_id=existing.investigation_id,
            tool_name=existing.tool_name,
            idempotency_key=existing.idempotency_key,
            status=status,
            started_at=existing.started_at,
            finished_at=finished_at,
            arguments=existing.arguments,
            tool_version=existing.tool_version,
            provider_request_id=existing.provider_request_id,
            error_code=error_code,
            safe_error_message=safe_error_message,
            result_metadata=result_metadata,
        )

    async def find_by_key(
        self, *, tenant_id: str, investigation_id: UUID, idempotency_key: str
    ) -> ToolInvocationRecord | None:
        return self.rows.get((investigation_id, idempotency_key))

    def by_investigation(self, investigation_id: UUID) -> list[ToolInvocationRecord]:
        return [
            r for (iid, _key), r in self.rows.items() if iid == investigation_id
        ]



class FakeInvestigationRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Investigation] = {}
        self.added: list[Investigation] = []
        self.updated: list[Investigation] = []

    async def get(self, *, tenant_id: str, investigation_id: UUID) -> Investigation | None:
        inv = self._store.get(investigation_id)
        if inv is None or inv.tenant_id != tenant_id:
            return None
        return inv

    async def add(self, investigation: Investigation) -> None:
        self._store[investigation.id] = investigation
        self.added.append(investigation)

    async def update(self, investigation: Investigation) -> None:
        if investigation.id not in self._store:
            raise KeyError("update of missing investigation")
        self._store[investigation.id] = investigation
        self.updated.append(investigation)

    async def find_active_by_alert(
        self, *, tenant_id: str, source_alert_ref: ExternalResourceRef
    ) -> Investigation | None:
        for inv in self._store.values():
            if inv.tenant_id != tenant_id:
                continue
            if inv.source_alert_ref != source_alert_ref:
                continue
            if inv.status.is_active:
                return inv
        return None

    async def get_by_external_ref(
        self, *, tenant_id: str, provider: str, resource_type: str, address_id: str
    ) -> Investigation | None:
        for inv in self._store.values():
            if (
                inv.tenant_id == tenant_id
                and inv.source_alert_ref.provider == provider
                and inv.source_alert_ref.resource_type == resource_type
                and inv.source_alert_ref.address_id == address_id
            ):
                return inv
        return None


class FakeEvidenceRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Evidence] = {}

    async def add(self, evidence: Evidence) -> None:
        self._store[evidence.id] = evidence

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        return [e for e in self._store.values() if e.investigation_id == investigation_id]

    async def find_existing_dedup_keys(
        self, *, investigation_id: UUID, dedup_keys: list[str]
    ) -> set[str]:
        return {
            e.dedup_key
            for e in self._store.values()
            if e.investigation_id == investigation_id and e.dedup_key in dedup_keys
        }

    async def find_by_ids(
        self, *, tenant_id: str, investigation_id: UUID, evidence_ids: list[UUID]
    ) -> list[Evidence]:
        return [
            e
            for e in self._store.values()
            if e.investigation_id == investigation_id and e.id in evidence_ids
        ]


class FakeFindingRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Finding] = {}

    async def add(self, finding: Finding) -> None:
        self._store[finding.id] = finding

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Finding]:
        return [f for f in self._store.values() if f.investigation_id == investigation_id]


class FakeHypothesisRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Hypothesis] = {}

    async def add(self, hypothesis: Hypothesis) -> None:
        self._store[hypothesis.id] = hypothesis

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]:
        return [h for h in self._store.values() if h.investigation_id == investigation_id]

    async def get(
        self, *, tenant_id: str, investigation_id: UUID, hypothesis_id: UUID
    ) -> Hypothesis | None:
        h = self._store.get(hypothesis_id)
        if h is None or h.investigation_id != investigation_id:
            return None
        return h

    async def update_status(
        self, *, hypothesis_id: UUID, status: str, assessment_revision: int
    ) -> None:
        """Mutate the shared hypothesis row (frozen → dataclasses.replace)."""
        from dataclasses import replace
        from datetime import UTC, datetime

        from hisiem_soc_copilot.domain.investigation.enums import HypothesisStatus

        hypothesis = self._store.get(hypothesis_id)
        if hypothesis is None:
            raise KeyError("update of missing hypothesis")
        self._store[hypothesis_id] = replace(
            hypothesis,
            status=HypothesisStatus(status),
            assessment_revision=assessment_revision,
            updated_at=datetime.now(UTC),
        )


class FakeHypothesisAssessmentRepository:
    def __init__(self, hypotheses: FakeHypothesisRepository | None = None) -> None:
        self._store: dict[UUID, HypothesisAssessment] = {}
        self._hypotheses = hypotheses or FakeHypothesisRepository()

    async def add(self, assessment: HypothesisAssessment) -> None:
        self._store[assessment.id] = assessment

    async def add_evidence_links(
        self, assessment_id: UUID, evidence_relations: list[tuple[UUID, str]]
    ) -> None:
        pass

    async def update_hypothesis_status(
        self, *, hypothesis_id: UUID, status: str, assessment_revision: int
    ) -> None:
        await self._hypotheses.update_status(
            hypothesis_id=hypothesis_id,
            status=status,
            assessment_revision=assessment_revision,
        )

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[HypothesisAssessment]:
        return [
            a
            for a in self._store.values()
            if a.investigation_id == investigation_id
        ]


class FakePlanRevisionRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, PlanRevision] = {}

    async def add(self, plan_revision: PlanRevision) -> None:
        self._store[plan_revision.id] = plan_revision

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[PlanRevision]:
        return [
            p for p in self._store.values() if p.investigation_id == investigation_id
        ]


class FakeResultRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, InvestigationResult] = {}

    async def add(self, result: InvestigationResult) -> None:
        self._store[result.id] = result

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> InvestigationResult | None:
        for r in self._store.values():
            if r.investigation_id == investigation_id:
                return r
        return None


class FakeUnitOfWork:
    """In-memory UoW over all child repos (no real transaction).

    Attributes are annotated with the application Protocol types so this class
    structurally satisfies the ``UnitOfWork`` Protocol; the fake repo classes
    satisfy those repository Protocols structurally.
    """

    def __init__(self) -> None:
        from hisiem_soc_copilot.application.ports.repositories import (
            EvidenceRepository,
            FindingRepository,
            HypothesisAssessmentRepository,
            HypothesisRepository,
            InvestigationRepository,
            PlanRevisionRepository,
            ResultRepository,
        )

        self.investigations: InvestigationRepository = FakeInvestigationRepository()
        self.evidence: EvidenceRepository = FakeEvidenceRepository()
        self.findings: FindingRepository = FakeFindingRepository()
        self.hypotheses: HypothesisRepository = FakeHypothesisRepository()
        self.hypothesis_assessments: HypothesisAssessmentRepository = (
            FakeHypothesisAssessmentRepository()
        )
        self.plan_revisions: PlanRevisionRepository = FakePlanRevisionRepository()
        self.results: ResultRepository = FakeResultRepository()
        self.events = FakeEventLedger()
        self.command_receipts = FakeCommandReceiptStore()
        self.bindings = FakeOrchestrationBindingStore()
        self.tool_invocations = FakeToolInvocationStore()
        self._commits = 0

    @property
    def commits(self) -> int:
        return self._commits

    async def commit(self) -> None:
        self._commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def __aenter__(self) -> FakeUnitOfWork:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass


class FakeUnitOfWorkFactory:
    """Builds fresh FakeUnitOfWork transactions over ONE shared in-memory store.

    This mirrors the real database (each UoW = one session/transaction over the
    same tables), so state written by one command is visible to the next.
    """

    def __init__(self) -> None:
        self._investigations = FakeInvestigationRepository()
        self._evidence = FakeEvidenceRepository()
        self._findings = FakeFindingRepository()
        self._hypotheses = FakeHypothesisRepository()
        self._assessments = FakeHypothesisAssessmentRepository(self._hypotheses)
        self._plans = FakePlanRevisionRepository()
        self._results = FakeResultRepository()
        self.events = FakeEventLedger()
        self.command_receipts = FakeCommandReceiptStore()
        self.bindings = FakeOrchestrationBindingStore()
        self.tool_invocations = FakeToolInvocationStore()
        self.instances: list[FakeUnitOfWork] = []

    def __call__(self) -> UnitOfWork:
        uow = FakeUnitOfWork()
        uow.investigations = self._investigations
        uow.evidence = self._evidence
        uow.findings = self._findings
        uow.hypotheses = self._hypotheses
        uow.hypothesis_assessments = self._assessments
        uow.plan_revisions = self._plans
        uow.results = self._results
        uow.events = self.events
        uow.command_receipts = self.command_receipts
        uow.bindings = self.bindings
        uow.tool_invocations = self.tool_invocations
        self.instances.append(uow)
        return uow
