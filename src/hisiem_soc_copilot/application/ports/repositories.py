"""Repository / UnitOfWork ports.

Application code depends on these Protocols only — never on infrastructure. Every
public repository query is tenant-scoped (python-package-boundary.md §7); bare
``get(investigation_id)`` is forbidden.
"""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ...domain.investigation.aggregate import Investigation
from ...domain.investigation.entities import (
    Evidence,
    Finding,
    Hypothesis,
    HypothesisAssessment,
    InvestigationResult,
    PlanRevision,
)
from ...domain.investigation.value_objects import (
    ExternalResourceRef,
)
from ...domain.response.aggregate import ResponseProposal
from ...domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
)


class InvestigationRepository(Protocol):
    """Loads and stores the Investigation aggregate."""

    async def get(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> Investigation | None: ...

    async def add(self, investigation: Investigation) -> None: ...

    async def update(self, investigation: Investigation) -> None: ...

    async def find_active_by_alert(
        self,
        *,
        tenant_id: str,
        source_alert_ref: ExternalResourceRef,
    ) -> Investigation | None: ...

    async def get_by_external_ref(
        self, *, tenant_id: str, provider: str, resource_type: str, address_id: str
    ) -> Investigation | None: ...

    async def find_latest_by_alert(
        self,
        *,
        tenant_id: str,
        source_alert_ref: ExternalResourceRef,
    ) -> Investigation | None:
        """The most recent Investigation of ANY status for one source alert.

        Tenant-scoped, read-only. Powers Alert re-entry so a terminal Investigation
        stays reachable after re-investigation created a newer one (docs §6/§22).
        Ordered by ``created_at`` DESC with an ``id`` tie-break (deterministic).
        """


class EvidenceRepository(Protocol):
    """Append-only evidence ledger access."""

    async def add(self, evidence: Evidence) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Evidence]: ...

    async def find_existing_dedup_keys(
        self, *, investigation_id: UUID, dedup_keys: list[str]
    ) -> set[str]: ...

    async def find_by_ids(
        self, *, tenant_id: str, investigation_id: UUID, evidence_ids: list[UUID]
    ) -> list[Evidence]: ...

    async def find_investigation_ids_by_evidence_ids(
        self, *, tenant_id: str, evidence_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        """Resolve cited Evidence ids to their owning investigation_id (READ-ONLY).

        Tenant-scoped but NOT investigation-scoped: a citation whose Evidence lives
        in ANOTHER investigation is exactly the anomaly the caller must detect, and
        the per-investigation ``find_by_ids`` is blind to it. An id present in no
        row of the tenant is absent from the returned map (a dangling citation).
        Used by the evaluation harness's Finding-citation lineage check; production
        investigation code never needs a cross-investigation Evidence read.
        """


class FindingRepository(Protocol):
    async def add(self, finding: Finding) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Finding]: ...

    async def find_investigation_ids_by_finding_ids(
        self, *, tenant_id: str, finding_ids: list[UUID]
    ) -> dict[UUID, UUID]:
        """Resolve Finding ids to their owning investigation_id (READ-ONLY).

        Tenant-scoped but NOT investigation-scoped: a result that references a
        Finding belonging to ANOTHER investigation is exactly the anomaly the caller
        must detect, and the per-investigation ``list_by_investigation`` is blind to
        it. An id present in no row of the tenant is absent from the returned map (a
        dangling result finding). Used by the evaluation harness's result→finding
        integrity check; production investigation code never needs a cross-
        investigation Finding read.
        """


class HypothesisRepository(Protocol):
    async def add(self, hypothesis: Hypothesis) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[Hypothesis]: ...

    async def get(
        self, *, tenant_id: str, investigation_id: UUID, hypothesis_id: UUID
    ) -> Hypothesis | None: ...


class HypothesisAssessmentRepository(Protocol):
    """Append-only assessment revisions; hypothesis status moves with the latest."""

    async def add(self, assessment: HypothesisAssessment) -> None: ...

    async def add_evidence_links(
        self, assessment_id: UUID, evidence_relations: list[tuple[UUID, str]]
    ) -> None: ...

    async def update_hypothesis_status(
        self, *, hypothesis_id: UUID, status: str, assessment_revision: int
    ) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[HypothesisAssessment]: ...


class ResultRepository(Protocol):
    async def add(self, result: InvestigationResult) -> None: ...

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> InvestigationResult | None: ...


class PlanRevisionRepository(Protocol):
    async def add(self, plan_revision: PlanRevision) -> None: ...

    async def list_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> list[PlanRevision]: ...


class ResponseProposalRepository(Protocol):
    """Tenant-scoped persistence for the ResponseProposal aggregate.

    ``get``/``get_by_*`` return the proposal WITH its target refs and evidence
    links loaded, so the approval content-hash contract can be re-verified and the
    workspace projection rendered without a second round-trip.
    """

    async def add(self, proposal: ResponseProposal) -> None: ...

    async def update(self, proposal: ResponseProposal) -> None: ...

    async def get(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseProposal | None: ...

    async def get_by_investigation(
        self, *, tenant_id: str, investigation_id: UUID
    ) -> ResponseProposal | None: ...

    async def get_by_result(
        self, *, tenant_id: str, result_id: UUID
    ) -> ResponseProposal | None: ...


class ResponseApprovalRepository(Protocol):
    async def add_request(self, request: ApprovalRequest) -> None: ...

    async def get_request(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalRequest | None: ...

    async def get_request_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ApprovalRequest | None: ...

    async def add_decision(self, decision: ApprovalDecision) -> None: ...

    async def get_decision(
        self, *, tenant_id: str, approval_request_id: UUID
    ) -> ApprovalDecision | None: ...


class ResponseSubmissionRepository(Protocol):
    """Local submission lifecycle, independent of the provider execution ref.

    A definitive provider rejection has a row here and NO ``response_execution_ref``
    row — which is exactly the fact the workspace must be able to show.
    """

    async def add(self, submission: ResponseSubmission) -> None: ...

    async def update(self, submission: ResponseSubmission) -> None: ...

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseSubmission | None: ...


class ResponseExecutionRepository(Protocol):
    async def add(self, execution: ResponseExecutionRef) -> None: ...

    async def update(self, execution: ResponseExecutionRef) -> None: ...

    async def get_by_proposal(
        self, *, tenant_id: str, proposal_id: UUID
    ) -> ResponseExecutionRef | None: ...

    async def get_by_execution_id(
        self, *, tenant_id: str, execution_id: str
    ) -> ResponseExecutionRef | None: ...
