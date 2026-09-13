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

    Covers "the profile for this provider is retired" and "the configured provider
    has no ACTIVE profile yet". Both are operator decisions, so both are refused
    loudly rather than resolved silently (brief sections 16/18).
    """

    code = "KNOWLEDGE_EMBEDDING_PROFILE"


class EmbeddingProfileSwitchRequiresReindexError(KnowledgeEmbeddingProfileError):
    """An ordinary ingest cannot move the corpus to a different embedding space.

    Switching the ACTIVE profile is a CORPUS-wide act: every vector in the
    existing corpus was produced by the old model, and the new one's vectors are
    not comparable with them. Allowing one document's ingest to retire the old
    profile and create a new ACTIVE one would leave the corpus half-embedded in
    two incomparable spaces while retrieval happily compared them -- the precise
    failure the ACTIVE-profile model exists to prevent (brief section 4).

    P3-A therefore refuses the switch outright: no flag enables it, and the old
    ACTIVE profile stays active with its corpus still vector-retrievable. The
    correct corpus-wide flow (stage -> full reindex -> validate -> atomic
    activation -> retire) is documented but deliberately NOT implemented here.
    """

    code = "EMBEDDING_PROFILE_SWITCH_REQUIRES_CORPUS_REINDEX"


class AttackReleaseContentConflictError(ApplicationError):
    """A pinned ATT&CK release already exists with different content.

    The release name is an immutability claim: re-importing different bytes under
    an existing release name would rewrite what an already-pinned release means,
    silently invalidating every citation that named it. Detected BEFORE any
    mutation, so a refused import leaves no canonical row, no knowledge document,
    no document version, no active-release change and no embedding projection
    behind (brief sections 2.2/2.3).
    """

    code = "ATTACK_RELEASE_CONTENT_CONFLICT"


class AttackReleaseAuthorityAmbiguousError(ApplicationError):
    """More than one release of a framework is authoritative.

    Impossible for releases imported or migrated by this code -- the per-framework
    partial unique index forbids it -- but data written by an earlier schema could
    contain it. The ambiguity is reported for an operator to resolve explicitly
    rather than guessed away, because guessing would silently choose which
    canonical knowledge is authoritative (brief section 9.2).
    """

    code = "ATTACK_RELEASE_AUTHORITY_AMBIGUOUS"


class AttackReleaseProjectionIncompleteError(ApplicationError):
    """A release cannot be made authoritative until its projection is complete.

    Authority is a claim about what retrieval serves. Switching the release while
    some of its techniques have no staged projection would make the framework
    authoritative for content retrieval cannot return -- the exact divergence
    between canonical authority and the retrieval projection this closure exists
    to close (brief sections 2.5/2.7).

    Detected BEFORE any mutation, inside the cutover transaction, so a refused
    activation leaves the authoritative release and every document pointer
    exactly as they were.
    """

    code = "ATTACK_RELEASE_PROJECTION_INCOMPLETE"


class AttackReleaseProjectionMissingVersionError(ApplicationError):
    """A staged projection names a document version that does not resolve.

    Defensive: the projection's foreign key makes this state unreachable, and the
    check exists so that "unreachable" is verified at cutover time rather than
    assumed. Cutting over to a version that cannot be read would serve nothing
    while claiming authority (brief section 2.7).
    """

    code = "ATTACK_RELEASE_PROJECTION_MISSING_VERSION"


class SystemManagedKnowledgeSourceError(ApplicationError):
    """Refused: this source kind is owned by a system workflow, not by callers.

    ``MITRE_ATTACK`` knowledge is written and retired only by the ATT&CK
    import/stage/cutover application path. Letting an ordinary ingest or retire
    touch it would hand a second writer the authoritative projection -- the
    closure-2 cutover would then be a suggestion rather than the single writer
    the invariant requires (closure-3 section 4).
    """

    code = "SYSTEM_MANAGED_KNOWLEDGE_SOURCE"


class AttackReleaseProjectionInvalidBindingError(ApplicationError):
    """A staged binding names the right rows but the wrong relationship.

    The binding row exists and its document and version resolve, yet they are not
    THIS release's projection: the version does not belong to the bound document,
    the document is not a GLOBAL MITRE_ATTACK projection target, its external
    key is not the canonical ``mitre-attack:<technique_id>`` for the bound
    technique, or the content-hash chain
    canonical == binding == version is broken. This is a different defect from a
    missing version -- the row exists, the relationship is invalid -- so it gets
    its own code rather than borrowing one (closure-3 section 13).
    """

    code = "ATTACK_RELEASE_PROJECTION_INVALID_BINDING"


class KnowledgeCorpusPreconditionError(ApplicationError):
    """The corpus under evaluation is not the corpus the baseline expects.

    A sealed evaluation must refuse to score an ambient or drifted corpus: an
    unexpected document, a missing expected one, or a changed version/chunk
    projection changes what is being measured, and reporting a metric from it
    would be a number that describes something else entirely (brief section 5.3).
    """

    code = "CORPUS_PRECONDITION_FAILED"


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
