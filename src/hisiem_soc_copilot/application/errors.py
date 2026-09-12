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


class KnowledgeIngestionConflictError(ApplicationError):
    """A knowledge unique constraint was violated by a concurrent writer.

    Raised by the UnitOfWork commit when one of the knowledge unique indexes
    fires (document identity, version number, content hash, chunk ordinal,
    attack-technique release, single ACTIVE embedding profile). The ingestion
    handler catches it, re-reads, and either converges on the winner's version
    (same content) or re-raises it as a deterministic conflict -- a raw
    IntegrityError must never reach a caller as a 500 (brief section 28).
    """

    code = "KNOWLEDGE_INGESTION_CONFLICT"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class KnowledgeEmbeddingProfileError(ApplicationError):
    """The configured embedding space cannot be used for this write.

    Covers "the profile for this provider is retired" and "a different profile is
    already ACTIVE and switching was not explicitly requested". Both are operator
    decisions, so both are refused loudly rather than resolved silently
    (brief sections 16/18).
    """

    code = "KNOWLEDGE_EMBEDDING_PROFILE"


class InvalidKnowledgeQueryError(ApplicationError):
    """Raised when a knowledge query violates its bounded contract.

    Query normalization is a REJECTION boundary, not a repair service: an
    over-long topic or too many context terms fails loudly instead of being
    silently truncated into a different query than the caller asked for
    (brief sections 40/41).
    """

    code = "INVALID_KNOWLEDGE_QUERY"

    def __init__(self, message: str) -> None:
        super().__init__(message)


class KnowledgeRetrievalUnavailableError(ApplicationError):
    """Raised when retrieval cannot run at all, as opposed to returning no hits.

    Distinct from "no results" on purpose: a missing ACTIVE embedding profile or
    an unconfigured provider is an operational fault an operator must fix, and
    reporting it as an empty result set would hide it (brief sections 16/31).
    """

    code = "KNOWLEDGE_RETRIEVAL_UNAVAILABLE"

    def __init__(self, message: str) -> None:
        super().__init__(message)


def to_http_error(exc: BaseException) -> tuple[int, str, str]:
    """Map domain/application errors to stable HTTP (status, code, message).

    Never leaks raw upstream bodies or internal detail.
    """
    if isinstance(exc, NotFoundError):
        return 404, exc.code, exc.args[0] if exc.args else "not found"
    if isinstance(exc, (IdempotencyConflictError, CommandReceiptConflictError)):
        return 409, exc.code, exc.args[0] if exc.args else "idempotency conflict"
    if isinstance(exc, DomainError):
        if exc.code == "SERVICE_AUTHENTICATION_FAILED":
            # Service-to-service authentication failure (missing/invalid service
            # credential, or missing server-asserted identity) → 401.
            return 401, exc.code, str(exc)
        if exc.code == "UNTRUSTED_REQUEST":
            # Authentication/authorization boundary failures are 403, not client 400.
            return 403, exc.code, str(exc)
        status = 409 if exc.code in {
            "INVALID_STATE_TRANSITION",
            "ACTIVE_INVESTIGATION_EXISTS",
            "OPTIMISTIC_CONCURRENCY",
            "APPROVAL_DECISION_EXISTS",
            "APPROVAL_CONTRACT_MISMATCH",
            "RESPONSE_PROPOSAL_CONFLICT",
        } else 400
        return status, exc.code, str(exc)
    if isinstance(exc, OptimisticConcurrencyError):
        return 409, exc.code, str(exc)
    if isinstance(exc, ExternalServiceError):
        return 502, exc.code, str(exc)
    if isinstance(exc, UnauthorizedError):
        return 403, exc.code, str(exc)
    if isinstance(exc, KnowledgeRetrievalUnavailableError):
        # A deployment/configuration fault, not a caller mistake: 503 so an
        # operator sees "knowledge retrieval is down", never a bare 400.
        return 503, exc.code, str(exc)
    if isinstance(exc, ApplicationError):
        return 400, exc.code, str(exc)
    return 500, "INTERNAL_ERROR", "internal error"
