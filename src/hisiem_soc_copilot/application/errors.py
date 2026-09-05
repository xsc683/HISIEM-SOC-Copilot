"""Application-level errors for command handling and query read models."""

from __future__ import annotations

from ..domain.shared.errors import DomainError, OptimisticConcurrencyError


class ApplicationError(Exception):
    """Base for application-layer errors not already represented in the domain."""

    code = "APPLICATION_ERROR"


class IdempotencyConflictError(ApplicationError):
    """Raised when an Idempotency-Key is reused for a DIFFERENT business request.

    Same key must always mean the same logical operation; binding it to a different
    source_alert_ref is a deterministic conflict (never a silent wrong replay).
    """

    code = "IDEMPOTENCY_CONFLICT"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class CommandReceiptConflictError(ApplicationError):
    """Infrastructure-translated marker for a command_receipt scoped-unique conflict.

    Raised by the UnitOfWork commit when the ``(tenant_id, command_type,
    idempotency_key)`` unique constraint is violated by a CONCURRENT same-key
    request. The investigation handler resolves it deterministically (reload the
    winning receipt → same request: return the original aggregate; different
    request: raise IdempotencyConflictError). It is caught before it can ever leak
    to HTTP; mapping it to 409 here is a defensive backstop so a bug can never
    surface a raw IntegrityError → 500.
    """

    code = "COMMAND_RECEIPT_CONFLICT"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class NotFoundError(ApplicationError):
    """Raised when a requested aggregate/read model does not exist."""

    code = "NOT_FOUND"

    def __init__(self, message: str, *, resource_type: str, resource_id: str) -> None:
        super().__init__(message)
        self.resource_type = resource_type
        self.resource_id = resource_id


class UnauthorizedError(ApplicationError):
    code = "UNAUTHORIZED"


class ExternalServiceError(ApplicationError):
    """Raised when a HISIEM/threat-intel/knowledge call fails (mapped, no leak)."""

    code = "EXTERNAL_SERVICE_ERROR"

    def __init__(self, message: str, *, service: str, code: str | None = None) -> None:
        super().__init__(message)
        self.service = service
        self.upstream_code = code


def to_http_error(exc: BaseException) -> tuple[int, str, str]:
    """Map domain/application errors to stable HTTP (status, code, message).

    Never leaks raw upstream bodies or internal detail.
    """
    if isinstance(exc, NotFoundError):
        return 404, exc.code, exc.args[0] if exc.args else "not found"
    if isinstance(exc, (IdempotencyConflictError, CommandReceiptConflictError)):
        return 409, exc.code, exc.args[0] if exc.args else "idempotency conflict"
    if isinstance(exc, DomainError):
        if exc.code == "UNTRUSTED_REQUEST":
            # Authentication/authorization boundary failures are 403, not client 400.
            return 403, exc.code, str(exc)
        status = 409 if exc.code in {
            "INVALID_STATE_TRANSITION",
            "ACTIVE_INVESTIGATION_EXISTS",
            "OPTIMISTIC_CONCURRENCY",
            "APPROVAL_DECISION_EXISTS",
            "APPROVAL_CONTRACT_MISMATCH",
        } else 400
        return status, exc.code, str(exc)
    if isinstance(exc, OptimisticConcurrencyError):
        return 409, exc.code, str(exc)
    if isinstance(exc, ExternalServiceError):
        return 502, exc.code, str(exc)
    if isinstance(exc, UnauthorizedError):
        return 403, exc.code, str(exc)
    if isinstance(exc, ApplicationError):
        return 400, exc.code, str(exc)
    return 500, "INTERNAL_ERROR", "internal error"
