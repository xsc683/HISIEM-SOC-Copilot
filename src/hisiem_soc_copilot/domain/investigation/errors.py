"""Investigation aggregate errors."""

from __future__ import annotations

from typing import Any

from ..shared.errors import DomainError, StateTransitionError


class InvestigationNotFoundError(DomainError):
    code = "INVESTIGATION_NOT_FOUND"


class ActiveInvestigationExistsError(DomainError):
    """Raised when a start command targets an alert that already has an active run."""

    code = "ACTIVE_INVESTIGATION_EXISTS"

    def __init__(self, *, alert_ref: str) -> None:
        super().__init__(
            f"An active investigation already exists for alert {alert_ref}",
            details={"source_alert_ref": alert_ref},
        )


class InvestigationStateError(StateTransitionError):
    """Raised when a command would perform an illegal investigation transition."""

    def __init__(
        self,
        *,
        investigation_id: Any,
        current_status: str,
        command: str,
    ) -> None:
        super().__init__(
            aggregate_type="investigation",
            current_status=current_status,
            command=command,
            message=(
                f"investigation {investigation_id} cannot {command} "
                f"from status {current_status}"
            ),
        )
        self.investigation_id = investigation_id
