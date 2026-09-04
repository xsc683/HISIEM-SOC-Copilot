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
