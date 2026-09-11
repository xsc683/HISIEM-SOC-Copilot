"""Response aggregate errors."""

from __future__ import annotations

from typing import Any

from ..shared.errors import DomainError


class ResponseProposalError(DomainError):
    code = "RESPONSE_PROPOSAL_ERROR"


class ResponseProposalNotReadyError(ResponseProposalError):
    """Raised when entering approval before all preconditions hold."""

    code = "RESPONSE_PROPOSAL_NOT_READY"

    def __init__(self, *, reasons: list[str]) -> None:
        super().__init__(
            "Response proposal cannot enter approval: " + "; ".join(reasons),
            details={"reasons": reasons},
        )


class ApprovalContractError(ResponseProposalError):
    """Raised when an approval decision no longer matches the requested content."""

    code = "APPROVAL_CONTRACT_MISMATCH"

    def __init__(self, *, approval_request_id: Any, proposal_id: Any) -> None:
        super().__init__(
            f"Approval decision {approval_request_id} no longer matches proposal "
            f"{proposal_id} content revision/hash"
        )


class ApprovalDecisionAlreadyExistsError(ResponseProposalError):
    code = "APPROVAL_DECISION_EXISTS"

    def __init__(self, *, approval_request_id: Any) -> None:
        super().__init__(
            f"Approval request {approval_request_id} already has a decision",
            details={"approval_request_id": str(approval_request_id)},
        )


class ResponseEvidenceInvalidError(ResponseProposalError):
    """Raised when requested supporting evidence does not resolve in scope.

    Deliberately INDISTINGUISHABLE across "unknown id", "another investigation's
    evidence" and "another tenant's evidence": the message states the RULE, never
    whether a foreign row exists, so the endpoint cannot be used as an existence
    oracle for other tenants' data.
    """

    code = "RESPONSE_EVIDENCE_INVALID"

    def __init__(self) -> None:
        super().__init__(
            "supporting evidence must reference evidence recorded on this investigation",
            details={"rule": "evidence_ids must resolve within the investigation"},
        )


class ResponseInvestigationNotCompletedError(ResponseProposalError):
    """A response association may only be established by a COMPLETED Investigation.

    The response workflow is an independent POST-INVESTIGATION aggregate lifecycle,
    so the investigation that a proposal is derived from must already have
    finalized (and therefore be COMPLETED).
    """

    code = "RESPONSE_INVESTIGATION_NOT_COMPLETED"

    def __init__(self, *, status: str) -> None:
        super().__init__(
            "a response proposal requires a completed investigation",
            details={"investigation_status": status},
        )
