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
    CommandReceiptRecord,
    DomainEventEnvelope,
    DurableCommand,
    OrchestrationBinding,
    OutboxRecord,
    ToolInvocationRecord,
)
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
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
from hisiem_soc_copilot.domain.response.aggregate import ResponseProposal
from hisiem_soc_copilot.domain.response.errors import (
    ResponseProposalConflictError,
)
from hisiem_soc_copilot.domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)


@dataclass
class FakeOutboxStore:
    """In-memory outbox with lease fencing + dead-letter semantics.

    Mirrors the real store: claim moves ready rows (PENDING / FAILED retry-due /
    PROCESSING with an expired lease) to PROCESSING with a fresh lease_token; every
    settlement (published / failed / dead-letter) and every renewal must present the
    SAME token — a worker whose lease was reclaimed holds a stale token and is
    rejected (rowcount == 0). Tracks a clock so tests can simulate lease expiry.
    """

    rows: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    published_ids: list[UUID] = field(default_factory=list)
    failed_ids: list[UUID] = field(default_factory=list)
    dead_letter_ids: list[UUID] = field(default_factory=list)
    #: ``(outbox_id, error_code, next_available_at)`` for every FAILED settlement,
    #: so a test can assert the retry schedule (future, monotone, capped) without
    #: reaching into the row dict.
    failed_schedules: list[tuple[UUID, str, Any]] = field(default_factory=list)
    now: Any = field(default_factory=lambda: __import__("datetime").datetime.now(
        __import__("datetime").timezone.utc
    ))
    _seq = 0

    def enqueue(
        self,
        event_id: UUID,
        *,
        destination: str = "investigation.graph.run",
        available_at: Any = None,
    ) -> None:
        self._seq += 1
        self.rows[event_id] = {
            "id": uuid4(),
            "event_id": event_id,
            "destination": destination,
            "status": "PENDING",
            "attempt_count": 0,
            "locked_at": None,
            "locked_by": None,
            "lease_token": None,
            "available_at": available_at if available_at is not None else self.now,
        }

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self.now = self.now + timedelta(seconds=seconds)

    async def claim_batch(
        self,
        *,
        worker: str,
        limit: int,
        available_before: Any,
        lease_timeout_seconds: int = 60,
        destination: str | None = None,
    ) -> list[OutboxRecord]:
        # The fake owns its own clock (``self.now``) so tests can drive lease
        # expiry via ``advance()`` deterministically.
        del available_before
        ready = [
            self.rows[row_event_id]
            for row_event_id, row in self.rows.items()
            if self._claimable(row, lease_timeout_seconds)
            and (destination is None or row["destination"] == destination)
        ]
        ready.sort(key=lambda r: r["event_id"])  # deterministic order
        claimed: list[OutboxRecord] = []
        for row in ready[:limit]:
            row["status"] = "PROCESSING"
            row["attempt_count"] += 1
            row["locked_at"] = self.now
            row["locked_by"] = worker
            row["lease_token"] = uuid4().hex
            claimed.append(
                OutboxRecord(
                    id=row["id"],
                    event_id=row["event_id"],
                    destination=row["destination"],
                    status="PROCESSING",
                    attempt_count=row["attempt_count"],
                    available_at=row.get("available_at", self.now),
                    lease_token=row["lease_token"],
                    locked_at=self.now,
                    locked_by=worker,
                )
            )
        return claimed

    def _claimable(self, row: dict[str, Any], lease_timeout_seconds: int) -> bool:
        from datetime import timedelta

        status = row["status"]
        if status in ("PENDING", "FAILED"):
            return row.get("available_at", self.now) <= self.now
        if status == "PROCESSING":
            locked_at = row.get("locked_at")
            if locked_at is None:
                return False
            return locked_at <= self.now - timedelta(seconds=lease_timeout_seconds)
        return False

    async def renew_lease(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        lease_timeout_seconds: int,
        now: Any,
    ) -> bool:
        for row in self.rows.values():
            if (
                row["status"] == "PROCESSING"
                and row["id"] == outbox_id
                and row.get("lease_token") == lease_token
            ):
                # locked_at = the timestamp of this successful renewal (never the
                # future). The expiry check is locked_at <= now - lease_timeout, so a
                # renewal re-arms the 60s window from NOW, exactly as the real store.
                row["locked_at"] = now
                return True
        return False

    async def mark_published(
        self, *, outbox_id: UUID, lease_token: str, published_at: Any
    ) -> bool:
        for row in self.rows.values():
            if (
                row["status"] == "PROCESSING"
                and row["id"] == outbox_id
                and row.get("lease_token") == lease_token
            ):
                row["status"] = "PUBLISHED"
                row["lease_token"] = None
                self.published_ids.append(outbox_id)
                return True
        return False

    async def mark_failed(
        self,
        *,
        outbox_id: UUID,
        lease_token: str,
        error_code: str,
        next_available_at: Any,
        attempt_count: int,
    ) -> bool:
        for row in self.rows.values():
            if (
                row["status"] == "PROCESSING"
                and row["id"] == outbox_id
                and row.get("lease_token") == lease_token
            ):
                row["status"] = "FAILED"
                row["available_at"] = next_available_at
                row["lease_token"] = None
                self.failed_ids.append(outbox_id)
                self.failed_schedules.append((outbox_id, error_code, next_available_at))
                return True
        return False

    async def mark_dead_letter(
        self, *, outbox_id: UUID, lease_token: str, error_code: str
    ) -> bool:
        for row in self.rows.values():
            if (
                row["status"] == "PROCESSING"
                and row["id"] == outbox_id
                and row.get("lease_token") == lease_token
            ):
                row["status"] = "DEAD_LETTER"
                row["lease_token"] = None
                self.dead_letter_ids.append(outbox_id)
                return True
        return False




#: Mirrors ``_EVENT_DESTINATIONS`` in the SQLAlchemy ledger. Kept in sync by
#: ``tests/unit/agent/test_fake_ledger_destinations.py`` so the fake can never
#: silently stop enqueueing a delivery the real ledger would enqueue.
FAKE_EVENT_DESTINATIONS: dict[str, str] = {
    "investigation_created": "investigation.graph.run",
    "response_execution_queued": "response.execution.submit",
    "response_execution_submitted": "response.execution.observe",
    "response_execution_observed": "response.execution.observe",
    "response_execution_observation_failed": "response.execution.observe",
}


@dataclass
class FakeEventLedger:
    """In-memory domain_event store.

    When an ``outbox`` store is attached it mirrors the real ledger's behaviour:
    an event whose type has a destination also enqueues one outbox delivery,
    honouring a delayed ``available_at`` (used by durable reconciliation).
    """

    events: list[Any] = field(default_factory=list)
    revisions: dict[UUID, int] = field(default_factory=dict)
    outbox: Any = None

    async def append(
        self,
        event: Any,
        *,
        aggregate_revision: int,
        available_at: Any = None,
    ) -> None:
        self.events.append(event)
        self.revisions[event.aggregate_id] = aggregate_revision
        destination = FAKE_EVENT_DESTINATIONS.get(event.event_type)
        if destination is not None and self.outbox is not None:
            self.outbox.enqueue(
                event.event_id, destination=destination, available_at=available_at
            )

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
    """In-memory command_receipt store.

    Mirrors the real (tenant, command_type, idempotency_key)-scoped identity: the
    backing dict is keyed by that triple, so the same key in two tenants (or two
    command types) are distinct idempotency spaces.
    """

    _receipts: dict[tuple[str, str, str], DurableCommand] = field(default_factory=dict)

    async def exists(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> bool:
        return (tenant_id, command_type, idempotency_key) in self._receipts

    async def record(self, receipt: DurableCommand) -> None:
        self._receipts[(receipt.tenant_id, receipt.command_type, receipt.idempotency_key)] = (
            receipt
        )

    async def find(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> CommandReceiptRecord | None:
        receipt = self._receipts.get((tenant_id, command_type, idempotency_key))
        if receipt is None:
            return None
        return CommandReceiptRecord(
            idempotency_key=receipt.idempotency_key,
            command_type=receipt.command_type,
            tenant_id=receipt.tenant_id,
            aggregate_id=receipt.aggregate_id,
            request_fingerprint=receipt.request_fingerprint,
            safe_result=dict(receipt.safe_result) if receipt.safe_result else None,
        )

    async def get_safe_result(
        self, *, tenant_id: str, command_type: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        receipt = self._receipts.get((tenant_id, command_type, idempotency_key))
        return dict(receipt.safe_result) if receipt and receipt.safe_result else None

    async def list_for_aggregate(
        self, *, tenant_id: str, aggregate_type: str, aggregate_id: UUID
    ) -> list[CommandReceiptRecord]:
        return [
            CommandReceiptRecord(
                idempotency_key=r.idempotency_key,
                command_type=r.command_type,
                tenant_id=r.tenant_id,
                aggregate_id=r.aggregate_id,
                request_fingerprint=r.request_fingerprint,
                safe_result=dict(r.safe_result) if r.safe_result else None,
            )
            for r in self._receipts.values()
            if r.tenant_id == tenant_id
            and r.aggregate_type == aggregate_type
            and r.aggregate_id == aggregate_id
        ]

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

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[ToolInvocationRecord]:
        return self.by_investigation(investigation_id)

    def by_investigation(self, investigation_id: UUID) -> list[ToolInvocationRecord]:
        return [
            r for (iid, _key), r in self.rows.items() if iid == investigation_id
        ]



def _same_alert(a: ExternalResourceRef, b: ExternalResourceRef) -> bool:
    """Alert addressing identity — mirrors the SQL WHERE (business_id excluded)."""
    return (
        a.provider == b.provider
        and a.resource_type == b.resource_type
        and a.address_id == b.address_id
    )


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
            if not _same_alert(inv.source_alert_ref, source_alert_ref):
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

    async def find_latest_by_alert(
        self, *, tenant_id: str, source_alert_ref: ExternalResourceRef
    ) -> Investigation | None:
        matches = [
            inv
            for inv in self._store.values()
            if inv.tenant_id == tenant_id
            and _same_alert(inv.source_alert_ref, source_alert_ref)
        ]
        if not matches:
            return None
        return max(matches, key=lambda inv: (inv.created_at, str(inv.id)))


class FakeEvidenceRepository:
    """In-memory evidence store, tenant-scoped exactly like the SQL implementation.

    The SQL repository resolves a tenant by JOINing ``investigation``; the fake
    mirrors that by consulting the (shared) investigation store, so a foreign
    tenant's evidence can never resolve through ``find_by_ids``. Without the
    investigations reference the fake would be MORE permissive than production and
    would silently pass tenant-isolation tests it should fail.
    """

    def __init__(
        self, investigations: FakeInvestigationRepository | None = None
    ) -> None:
        self._store: dict[UUID, Evidence] = {}
        self._investigations = investigations

    def _in_tenant(self, *, tenant_id: str, investigation_id: UUID) -> bool:
        if self._investigations is None:
            return True
        inv = self._investigations._store.get(investigation_id)
        return inv is not None and inv.tenant_id == tenant_id

    async def add(self, evidence: Evidence) -> None:
        self._store[evidence.id] = evidence

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]:
        if not self._in_tenant(
            tenant_id=tenant_id, investigation_id=investigation_id
        ):
            return []
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
        if not self._in_tenant(
            tenant_id=tenant_id, investigation_id=investigation_id
        ):
            return []
        return [
            e
            for e in self._store.values()
            if e.investigation_id == investigation_id and e.id in evidence_ids
        ]

    async def find_investigation_ids_by_evidence_ids(
        self, *, tenant_id: str, evidence_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        wanted = set(evidence_ids)
        return {
            e.id: e.investigation_id
            for e in self._store.values()
            if e.id in wanted
        }


class FakeFindingRepository:
    def __init__(self) -> None:
        self._store: dict[UUID, Finding] = {}

    async def add(self, finding: Finding) -> None:
        self._store[finding.id] = finding

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Finding]:
        return [f for f in self._store.values() if f.investigation_id == investigation_id]

    async def find_investigation_ids_by_finding_ids(
        self, *, tenant_id: str, finding_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        wanted = set(finding_ids)
        return {
            f.id: f.investigation_id
            for f in self._store.values()
            if f.id in wanted
        }


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


class FakeResponseProposalRepository:
    """In-memory proposal store, tenant-scoped exactly like the SQL repository.

    The SQL repository scopes every read by JOINing ``investigation`` for the
    tenant; the fake mirrors that through the (shared) investigation store so a
    wrong-tenant lookup can never resolve. Without the reference the fake would be
    MORE permissive than production and would silently pass tenant-isolation tests
    it should fail.
    """

    def __init__(self, investigations: FakeInvestigationRepository | None = None) -> None:
        self._store: dict[UUID, ResponseProposal] = {}
        self.pending_conflicts: list[ResponseProposal] = []
        self._investigations = investigations

    def in_tenant(self, proposal: ResponseProposal, tenant_id: str) -> bool:
        if self._investigations is None:
            return True
        inv = self._investigations._store.get(proposal.investigation_id)
        return inv is not None and inv.tenant_id == tenant_id

    async def add(self, proposal: ResponseProposal) -> None:
        # The real table has UNIQUE(investigation_id) and UNIQUE(result_id). Because
        # the ORM flushes inside commit(), a duplicate INSERT only fails THERE — so
        # the fake must not silently overwrite the first proposal either. It keeps
        # the winner and remembers the conflict for commit() to raise, exactly like
        # the SQLAlchemy unit of work translates the IntegrityError.
        for existing in self._store.values():
            if (
                existing.investigation_id == proposal.investigation_id
                or existing.result_id == proposal.result_id
            ):
                self.pending_conflicts.append(proposal)
                return
        self._store[proposal.id] = proposal

    async def update(self, proposal: ResponseProposal) -> None:
        self._store[proposal.id] = proposal

    def take_conflict(self) -> ResponseProposal | None:
        """Pop a pending unique-constraint conflict, if one was staged."""
        if not self.pending_conflicts:
            return None
        return self.pending_conflicts.pop(0)

    async def get(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseProposal | None:
        proposal = self._store.get(proposal_id)
        if proposal is None or not self.in_tenant(proposal, tenant_id):
            return None
        return proposal

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> ResponseProposal | None:
        for proposal in self._store.values():
            if proposal.investigation_id == investigation_id and self.in_tenant(
                proposal, tenant_id
            ):
                return proposal
        return None

    async def get_by_result(
        self, *, tenant_id: str, result_id: UUID
    ) -> ResponseProposal | None:
        for proposal in self._store.values():
            if proposal.result_id == result_id and self.in_tenant(proposal, tenant_id):
                return proposal
        return None


class FakeResponseApprovalRepository:
    """In-memory approval store, tenant-scoped via the owning proposal."""

    def __init__(
        self, proposals: FakeResponseProposalRepository | None = None
    ) -> None:
        self._requests: dict[UUID, ApprovalRequest] = {}
        self._decisions: dict[UUID, ApprovalDecision] = {}
        self._proposals = proposals

    def _request_in_tenant(self, request: ApprovalRequest, tenant_id: str) -> bool:
        if self._proposals is None:
            return True
        proposal = self._proposals._store.get(request.proposal_id)
        return proposal is not None and self._proposals.in_tenant(proposal, tenant_id)

    async def add_request(self, request: ApprovalRequest) -> None:
        self._requests[request.id] = request

    async def get_request(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalRequest | None:
        request = self._requests.get(approval_request_id)
        if request is None or not self._request_in_tenant(request, tenant_id):
            return None
        return request

    async def get_request_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ApprovalRequest | None:
        for request in self._requests.values():
            if request.proposal_id == proposal_id and self._request_in_tenant(
                request, tenant_id
            ):
                return request
        return None

    async def add_decision(self, decision: ApprovalDecision) -> None:
        if decision.approval_request_id in self._decisions:
            raise KeyError("approval decision already exists")
        self._decisions[decision.approval_request_id] = decision

    async def get_decision(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalDecision | None:
        return self._decisions.get(approval_request_id)


class FakeResponseExecutionRepository:
    """In-memory execution-ref store, tenant-scoped via the owning proposal."""

    def __init__(
        self, proposals: FakeResponseProposalRepository | None = None
    ) -> None:
        self._store: dict[UUID, ResponseExecutionRef] = {}
        self._proposals = proposals

    def _in_tenant(self, execution: ResponseExecutionRef, tenant_id: str) -> bool:
        if self._proposals is None:
            return True
        proposal = self._proposals._store.get(execution.proposal_id)
        return proposal is not None and self._proposals.in_tenant(proposal, tenant_id)

    async def add(self, execution: ResponseExecutionRef) -> None:
        self._store[execution.proposal_id] = execution

    async def update(self, execution: ResponseExecutionRef) -> None:
        self._store[execution.proposal_id] = execution

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseExecutionRef | None:
        execution = self._store.get(proposal_id)
        if execution is None or not self._in_tenant(execution, tenant_id):
            return None
        return execution

    async def get_by_execution_id(
        self, *, tenant_id: str, execution_id: str
    ) -> ResponseExecutionRef | None:
        for execution in self._store.values():
            if execution.execution_id == execution_id and self._in_tenant(
                execution, tenant_id
            ):
                return execution
        return None


class FakeSoar:
    """In-memory SoarPort for response-worker tests.

    ``submit_result`` is returned by ``submit_execution``; ``status_sequence`` is
    drained by ``get_execution_status`` (polling). ``raise_on_submit`` /
    ``raise_on_status`` inject transport/definitive failures.
    """

    def __init__(
        self,
        *,
        submit_result: SoarExecutionResult | None = None,
        status_sequence: list[str] | None = None,
        submit_sequence: list[SoarExecutionResult] | None = None,
        raise_on_submit: Exception | None = None,
        raise_on_status: Exception | None = None,
    ) -> None:
        self.submit_result = submit_result or SoarExecutionResult(
            execution_id="hisiem-exec-1", status="SUCCEEDED"
        )
        #: Successive submit results, drained one per ``submit_execution`` call. Used
        #: to prove two proposals get DISTINCT real provider execution identities.
        self.submit_sequence = list(submit_sequence or [])
        self.status_sequence = list(status_sequence or [])
        self.raise_on_submit = raise_on_submit
        self.raise_on_status = raise_on_status
        self.submitted: list[dict[str, object]] = []
        self.status_calls: list[str] = []

    async def submit_execution(
        self,
        *,
        tenant_id: str,
        proposal_id: UUID,
        submission_key: str,
        action_key: str,
        parameters: dict[str, object],
        target_ref: object,
    ) -> SoarExecutionResult:
        self.submitted.append(
            {
                "tenant_id": tenant_id,
                "proposal_id": proposal_id,
                "submission_key": submission_key,
                "action_key": action_key,
                "parameters": dict(parameters),
            }
        )
        if self.raise_on_submit is not None:
            raise self.raise_on_submit
        if self.submit_sequence:
            return self.submit_sequence.pop(0)
        return self.submit_result

    async def get_execution_status(
        self, *, tenant_id: str, execution_id: str
    ) -> SoarExecutionResult:
        self.status_calls.append(execution_id)
        if self.raise_on_status is not None:
            raise self.raise_on_status
        status = self.status_sequence.pop(0) if self.status_sequence else "SUCCEEDED"
        return SoarExecutionResult(execution_id=execution_id, status=status)


class FakeResponseSubmissionRepository:
    """In-memory local submission lifecycle, tenant-scoped via the owning proposal."""

    def __init__(
        self, proposals: FakeResponseProposalRepository | None = None
    ) -> None:
        self._store: dict[UUID, ResponseSubmission] = {}
        self._proposals = proposals

    def _in_tenant(self, submission: ResponseSubmission, tenant_id: str) -> bool:
        if self._proposals is None:
            return True
        proposal = self._proposals._store.get(submission.proposal_id)
        return proposal is not None and self._proposals.in_tenant(proposal, tenant_id)

    async def add(self, submission: ResponseSubmission) -> None:
        self._store[submission.proposal_id] = submission

    async def update(self, submission: ResponseSubmission) -> None:
        self._store[submission.proposal_id] = submission

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseSubmission | None:
        submission = self._store.get(proposal_id)
        if submission is None or not self._in_tenant(submission, tenant_id):
            return None
        return submission


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
            ResponseApprovalRepository,
            ResponseExecutionRepository,
            ResponseProposalRepository,
            ResponseSubmissionRepository,
            ResultRepository,
        )

        self.investigations: InvestigationRepository = FakeInvestigationRepository()
        self.evidence: EvidenceRepository = FakeEvidenceRepository(self.investigations)
        self.findings: FindingRepository = FakeFindingRepository()
        self.hypotheses: HypothesisRepository = FakeHypothesisRepository()
        self.hypothesis_assessments: HypothesisAssessmentRepository = (
            FakeHypothesisAssessmentRepository()
        )
        self.plan_revisions: PlanRevisionRepository = FakePlanRevisionRepository()
        self.results: ResultRepository = FakeResultRepository()
        self.response_proposals: ResponseProposalRepository = (
            FakeResponseProposalRepository(self.investigations)
        )
        self.response_approvals: ResponseApprovalRepository = (
            FakeResponseApprovalRepository(self.response_proposals)
        )
        self.response_executions: ResponseExecutionRepository = (
            FakeResponseExecutionRepository(self.response_proposals)
        )
        self.response_submissions: ResponseSubmissionRepository = (
            FakeResponseSubmissionRepository(self.response_proposals)
        )
        self.outbox = FakeOutboxStore()
        self.events = FakeEventLedger(outbox=self.outbox)
        self.command_receipts = FakeCommandReceiptStore()
        self.bindings = FakeOrchestrationBindingStore()
        self.tool_invocations = FakeToolInvocationStore()
        self._commits = 0
        self._closed = False

    @property
    def commits(self) -> int:
        return self._commits

    @property
    def is_closed(self) -> bool:
        return self._closed

    async def commit(self) -> None:
        # Mirror the SQLAlchemy unit of work: a staged UNIQUE violation on
        # response_proposal surfaces AT COMMIT as a deterministic conflict (never a
        # raw IntegrityError, never a silent second proposal).
        conflict = self.response_proposals.take_conflict()
        if conflict is not None:
            raise ResponseProposalConflictError(
                investigation_id=conflict.investigation_id
            )
        self._commits += 1

    async def rollback(self) -> None:
        pass

    async def close(self) -> None:
        self._closed = True

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
        self._evidence = FakeEvidenceRepository(self._investigations)
        self._findings = FakeFindingRepository()
        self._hypotheses = FakeHypothesisRepository()
        self._assessments = FakeHypothesisAssessmentRepository(self._hypotheses)
        self._plans = FakePlanRevisionRepository()
        self._results = FakeResultRepository()
        self._response_proposals = FakeResponseProposalRepository(
            self._investigations
        )
        self._response_approvals = FakeResponseApprovalRepository(
            self._response_proposals
        )
        self._response_executions = FakeResponseExecutionRepository(
            self._response_proposals
        )
        self._response_submissions = FakeResponseSubmissionRepository(
            self._response_proposals
        )
        self.outbox = FakeOutboxStore()
        self.events = FakeEventLedger(outbox=self.outbox)
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
        uow.response_proposals = self._response_proposals
        uow.response_approvals = self._response_approvals
        uow.response_executions = self._response_executions
        uow.response_submissions = self._response_submissions
        uow.events = self.events
        uow.command_receipts = self.command_receipts
        uow.bindings = self.bindings
        uow.tool_invocations = self.tool_invocations
        self.instances.append(uow)
        return uow
