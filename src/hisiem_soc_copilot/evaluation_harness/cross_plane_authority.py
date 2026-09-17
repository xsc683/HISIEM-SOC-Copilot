"""E3 authority measurement adapters: real authority facts -> E1 typed measurements.

Stage E / E3 owns the AUTHORITY and RELIABILITY families. This module is the middle
of the E3 flow, exactly as ``cross_plane_measure`` is for E2:

```text
persisted / runtime production facts
        -> THIS MODULE (measure facts)
        -> E1 typed measurement contract
        -> E1 deterministic hard gate
        -> cross-plane-gate-results/v1
```

Three rules shape every function here:

* **Measure, do not decide.** These adapters collect facts and hand them to E1's
  gates. They never compute a verdict, never score, and never authorize anything.
  ``evaluation.cross_plane.evaluate_scenario`` is the only decision point.
* **Measure production, never a second state machine.** The authority lifecycle
  (proposal -> policy -> human decision -> durable command -> submission ->
  observed execution) is owned by ``domain/response`` and the durable runners. The
  adapters read those objects and, where a projection or a rule is already
  implemented in production, they *call that implementation* rather than restate
  it: the workspace response projection and timeline come from
  ``application.services.workspace_service``, the failure-to-``ToolResult`` mapping
  comes from ``agent.tools.executor._tool_result_from_provider``, and the "a failed
  call produces no Evidence" rule comes from the real ``EvidenceNormalizer``.
* **E1 vocabulary only.** Measurements carry the frozen XP-01 fact vocabulary. An
  invariant that has no token (duplicate logical command, revision/hash binding) is
  reported as a bounded typed value the caller asserts on, never smuggled into a
  fact set as an invented token.

Read-only: nothing here writes production state, dispatches a command, calls SOAR,
opens a transaction, or emits telemetry. The only IO happens in the caller's
fixtures.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import UUID

from ..agent.evidence.normalizer import EvidenceNormalizer
from ..agent.tools.executor import _tool_result_from_provider
from ..agent.tools.providers import ProviderIdentity
from ..application.services.workspace_service import (
    TL_RESPONSE_SUBMISSION_ATTENTION_REQUIRED,
    _response_timeline_entries,
    _workspace_approval,
    _workspace_execution,
    _workspace_submission,
)
from ..application.services.workspace_service import (
    _result as _workspace_result,
)
from ..contracts.tools.types import ToolResult
from ..domain.investigation.entities import InvestigationResult
from ..domain.investigation.enums import VerdictDisposition
from ..domain.response.aggregate import ResponseProposal
from ..domain.response.enums import (
    ApprovalDecisionKind,
    PolicyDecision,
    ResponseExecutionStatus,
    ResponseSubmissionStatus,
)
from ..domain.response.events import ResponseEvent
from ..domain.response.value_objects import (
    ApprovalDecision,
    ApprovalRequest,
    ResponseExecutionRef,
    ResponseSubmission,
    submission_key,
)
from ..evaluation.cross_plane import (
    ExecutionAuthorityMeasurement,
    FactSetMeasurement,
    MeasurementReferences,
    MeasurementSource,
    SubmissionTruthMeasurement,
    TelemetryIsolationMeasurement,
    canonical_json,
    dedupe_bounded_ids,
    identity_hash,
)
from .cross_plane_measure import fact_set

#: Event type of the ONE durable local submission intent for one approval.
DURABLE_COMMAND_EVENT = "response_execution_queued"

#: Tool statuses that mean "the call did not produce a business result".
_FAILED_TOOL_STATUSES = frozenset({"UNAVAILABLE", "REJECTED"})

#: Frozen XP-01 fact tokens this module may emit. Listed so the vocabulary in use
#: is auditable from the adapter alone; every one is declared in
#: ``evaluation.cross_plane.gates``.
FACT_VERDICT_DISTINCT = "AGENT_VERDICT_DISTINCT_FROM_ANALYST_DISPOSITION"
FACT_VERDICT_CONFLATED = "AGENT_VERDICT_TREATED_AS_ANALYST_DISPOSITION"
FACT_POLICY_DECISION_RECORDED = "POLICY_DECISION_RECORDED"
FACT_APPROVAL_REQUEST_RECORDED = "APPROVAL_REQUEST_RECORDED"
FACT_POLICY_SYNTHESIZED_APPROVAL = "POLICY_SYNTHESIZED_APPROVAL"
FACT_APPROVAL_DECISION_RECORDED = "APPROVAL_DECISION_RECORDED"
FACT_DURABLE_COMMAND_RECORDED = "DURABLE_COMMAND_RECORDED"
FACT_APPROVAL_REJECTED_NO_EXECUTION = "APPROVAL_REJECTED_NO_EXECUTION"
FACT_EXECUTION_WITHOUT_APPROVAL = "EXECUTION_WITHOUT_APPROVAL_PRESENT"
FACT_SUBMISSION_ATTENTION_REQUIRED = "SUBMISSION_ATTENTION_REQUIRED"
FACT_SUBMISSION_NOT_TREATED_AS_SUCCESS = "SUBMISSION_NOT_TREATED_AS_SUCCESS"
FACT_SUBMISSION_TREATED_AS_SUCCESS = "SUBMISSION_TREATED_AS_SUCCESS_PRESENT"
FACT_EXECUTION_OBSERVED_FROM_HISIEM = "EXECUTION_OBSERVED_FROM_HISIEM"
FACT_EXECUTION_SUCCEEDED = "EXECUTION_SUCCEEDED"
FACT_EXECUTION_FAILED = "EXECUTION_FAILED"
FACT_TOOL_INVOCATION_FAILED_TYPED = "TOOL_INVOCATION_FAILED_TYPED"
FACT_RETRIEVAL_UNAVAILABLE_TYPED = "RETRIEVAL_UNAVAILABLE_TYPED"
FACT_FAILURE_NORMALIZED_AS_EMPTY = "FAILURE_NORMALIZED_AS_EMPTY"
FACT_FALSE_SUCCESS_EVIDENCE = "FALSE_SUCCESS_EVIDENCE_PRESENT"
FACT_TELEMETRY_OUTAGE_ISOLATED = "TELEMETRY_OUTAGE_ISOLATED"
FACT_TELEMETRY_ALTERED_BUSINESS_STATE = "TELEMETRY_ALTERED_BUSINESS_STATE"

#: Typed error code production uses when knowledge retrieval cannot run at all.
KNOWLEDGE_UNAVAILABLE_CODE = "KNOWLEDGE_UNAVAILABLE"


def _refs(
    *,
    investigation_id: UUID | None = None,
    proposal: ResponseProposal | None = None,
    proposal_id: UUID | None = None,
    request: ApprovalRequest | None = None,
    command_id: str | None = None,
    provider_execution_ref: str | None = None,
) -> MeasurementReferences:
    """Bounded correlation identities (E3 §30) — pointers, never embedded objects.

    ``proposal_id`` is for a record that knows the proposal by identity only (a
    submission or an execution projection); ``proposal`` is for the aggregate.
    """
    request_id: str | None = None
    if request is not None:
        request_id = str(request.id)
    elif proposal is not None and proposal.approval_request_id is not None:
        request_id = str(proposal.approval_request_id)
    return MeasurementReferences(
        investigation_id=(
            str(investigation_id)
            if investigation_id is not None
            else (str(proposal.investigation_id) if proposal is not None else None)
        ),
        response_proposal_id=(
            str(proposal.id)
            if proposal is not None
            else (str(proposal_id) if proposal_id is not None else None)
        ),
        approval_request_id=request_id,
        execution_command_id=command_id,
        provider_execution_ref=provider_execution_ref,
    )


def _facts(*, present: Sequence[str] = (), forbidden: Sequence[str] = ()) -> FactSetMeasurement:
    """A bounded fact set carrying observed facts (never a verdict)."""
    return fact_set(*present, *forbidden)


# ---------------------------------------------------------------------------
# XP-AUTH-001 — Agent verdict versus analyst disposition
# ---------------------------------------------------------------------------


def authority_role_separation(
    *,
    result: InvestigationResult,
    request: ApprovalRequest | None,
    decision: ApprovalDecision | None,
) -> FactSetMeasurement:
    """Measure whether the agent verdict and the human decision stay separate facts.

    The agent's verdict is the immutable persisted ``InvestigationResult.verdict``.
    The analyst's decisive act is an ``ApprovalDecision`` recorded against an
    ``ApprovalRequest``. They answer different questions and must never collapse
    into one field: a rejection does not rewrite the verdict, and a verdict does not
    authorize anything.

    Both planes are read through the REAL workspace projection
    (``workspace_service._result`` / ``_workspace_approval``), so a projection that
    started reporting the human decision as the verdict would be measured here
    rather than assumed away.
    """
    projected = _workspace_result(result)
    verdict_disposition = projected.verdict.disposition
    verdict_is_verdict_vocabulary = any(
        verdict_disposition == member.value for member in VerdictDisposition
    )

    if decision is None:
        # No human decision yet: nothing has been decided and nothing conflated.
        # The verdict plane is still the verdict plane.
        if verdict_is_verdict_vocabulary:
            return _facts(present=(FACT_VERDICT_DISTINCT,))
        return _facts(forbidden=(FACT_VERDICT_CONFLATED,))

    projected_approval = _workspace_approval(request, decision)
    decision_value = decision.decision
    decision_is_human_vocabulary = any(
        decision_value == member.value for member in ApprovalDecisionKind
    )
    decision_is_verdict_vocabulary = any(
        decision_value == member.value for member in VerdictDisposition
    )
    approval_projected_separately = (
        projected_approval is not None
        and projected_approval.decision is not None
        and projected_approval.decision.decision == decision_value
    )
    # A decision is a HUMAN authority fact only with an authenticated actor; an
    # empty actor is what a synthesized (non-human) decision would look like.
    authenticated_actor = bool(decision.actor_subject_id.strip())

    conflated = (
        decision_is_verdict_vocabulary
        or not decision_is_human_vocabulary
        or not verdict_is_verdict_vocabulary
        or not approval_projected_separately
        or not authenticated_actor
    )
    if conflated:
        return _facts(forbidden=(FACT_VERDICT_CONFLATED,))
    return _facts(present=(FACT_VERDICT_DISTINCT,))


# ---------------------------------------------------------------------------
# XP-AUTH-002 — Policy decision versus human approval
# ---------------------------------------------------------------------------


def policy_and_approval_facts(
    *,
    proposal: ResponseProposal,
    events: Sequence[ResponseEvent],
    request: ApprovalRequest | None,
    decision: ApprovalDecision | None,
) -> FactSetMeasurement:
    """Measure whether a policy decision was recorded as a decision, not as consent.

    V1 policy yields DENY or REQUIRE_APPROVAL and never ALLOW_AUTOMATIC, so a policy
    evaluation may never *be* the human authorization: a REQUIRE_APPROVAL outcome
    must have produced an approval REQUEST (not a decision), and a DENY outcome must
    have produced neither a request nor a decision.
    """
    present: list[str] = []
    forbidden: list[str] = []

    recorded_decision = proposal.policy_decision
    policy_events = [
        event
        for event in events
        if event.event_type == "response_policy_decided" and event.aggregate_id == proposal.id
    ]
    if recorded_decision is not None and any(
        event.payload.get("decision") == recorded_decision.value for event in policy_events
    ):
        present.append(FACT_POLICY_DECISION_RECORDED)

    # A human decision exists only when a human decided: the request must exist and
    # be bound to this proposal, and the decision must name an authenticated actor.
    human_decided = (
        decision is not None
        and request is not None
        and request.proposal_id == proposal.id
        and decision.approval_request_id == request.id
        and bool(decision.actor_subject_id.strip())
    )
    require_approval = (
        recorded_decision is not None
        and recorded_decision == PolicyDecision.REQUIRE_APPROVAL
    )
    deny = recorded_decision is not None and recorded_decision == PolicyDecision.DENY

    synthesized = (
        (decision is not None and not human_decided)
        or (deny and (request is not None or decision is not None))
        or (require_approval and request is None)
    )
    if synthesized:
        forbidden.append(FACT_POLICY_SYNTHESIZED_APPROVAL)
    elif request is not None and request.proposal_id == proposal.id:
        # The request is the AUTHORIZATION REQUEST the human decides on; it is bound
        # to the exact revision/hash the human is being asked about.
        present.append(FACT_APPROVAL_REQUEST_RECORDED)

    return _facts(present=present, forbidden=forbidden)


# ---------------------------------------------------------------------------
# XP-AUTH-003 — Human approval versus execution
# ---------------------------------------------------------------------------


def execution_authority(
    *,
    proposal: ResponseProposal | None,
    decision: ApprovalDecision | None,
    durable_command_ids: Sequence[str] = (),
    execution_observed: bool = False,
    provider_execution_ref: str | None = None,
) -> ExecutionAuthorityMeasurement:
    """Facts for the approval -> durable command -> execution chain.

    ``durable_command_ids`` are the DISTINCT logical durable submission intents for
    this authorization (see :func:`logical_command_identities`), never transport
    attempts: three retries of one command are one identity and must not read as
    three executions.
    """
    command_ids = dedupe_bounded_ids(list(durable_command_ids), what="durable_command_ids")
    return ExecutionAuthorityMeasurement(
        source=MeasurementSource.RESPONSE_LIFECYCLE_FACT,
        references=_refs(
            proposal=proposal,
            command_id=command_ids[0] if command_ids else None,
            provider_execution_ref=provider_execution_ref,
        ),
        proposal_status=proposal.status.value if proposal is not None else "",
        policy_decision=(
            proposal.policy_decision.value
            if proposal is not None and proposal.policy_decision is not None
            else ""
        ),
        approval_decision=decision.decision if decision is not None else None,
        durable_command_ids=command_ids,
        execution_observed=execution_observed,
    )


def authorized_execution_authority(
    *,
    proposal: ResponseProposal,
    request: ApprovalRequest | None,
    decision: ApprovalDecision | None,
    durable_command_ids: Sequence[str] = (),
    execution_observed: bool = False,
    provider_execution_ref: str | None = None,
) -> ExecutionAuthorityMeasurement:
    """Execution facts where the approval only counts if it authorizes THIS intent.

    An ``ApprovalDecision`` authorizes the exact revision/hash recorded on its
    ``ApprovalRequest``. If the proposal has since changed, the same decision no
    longer authorizes the live intent, so the command being authorized presents NO
    approval — which is precisely what makes a stale authorization fail the
    ``EXECUTION_WITHOUT_APPROVAL`` gate instead of silently passing it (E3 §9/§10).
    """
    authorizing = decision
    if decision is not None and request is not None:
        binding = current_authorization_binding(proposal=proposal, request=request)
        if not binding.is_bound:
            authorizing = None
    return execution_authority(
        proposal=proposal,
        decision=authorizing,
        durable_command_ids=durable_command_ids,
        execution_observed=execution_observed,
        provider_execution_ref=provider_execution_ref,
    )


def authority_command_facts(
    *,
    decision: ApprovalDecision | None,
    durable_command_ids: Sequence[str] = (),
    execution_observed: bool = False,
) -> FactSetMeasurement:
    """The declared XP-AUTH-003 facts, measured from persisted authority state."""
    present: list[str] = []
    forbidden: list[str] = []

    approved = decision is not None and decision.decision == ApprovalDecisionKind.APPROVE.value
    rejected = decision is not None and decision.decision == ApprovalDecisionKind.REJECT.value
    if decision is not None and bool(decision.actor_subject_id.strip()):
        present.append(FACT_APPROVAL_DECISION_RECORDED)
    if approved and durable_command_ids:
        present.append(FACT_DURABLE_COMMAND_RECORDED)
    if rejected and not durable_command_ids and not execution_observed:
        # Human rejection means "not authorized for execution" — it is not an
        # execution failure, and it must not leave a dispatchable command behind.
        present.append(FACT_APPROVAL_REJECTED_NO_EXECUTION)
    if (durable_command_ids or execution_observed) and not approved:
        forbidden.append(FACT_EXECUTION_WITHOUT_APPROVAL)

    return _facts(present=present, forbidden=forbidden)


# ---------------------------------------------------------------------------
# E3 §9/§10 — approval revision/hash binding and TOCTOU
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationBinding:
    """Bounded revision/hash binding facts for ONE authorization (E3 §9, §12).

    ``approved_*`` is the contract the human authorized (recorded on the approval
    request); ``authorized_*`` is the contract the command being authorized
    presents. They must describe the same intent, or the command is not authorized.
    """

    approved_revision: int
    approved_content_hash: str
    authorized_revision: int
    authorized_content_hash: str

    @property
    def is_bound(self) -> bool:
        """True only when the command authorizes the EXACT approved intent."""
        return (
            self.approved_revision == self.authorized_revision
            and bool(self.approved_content_hash)
            and self.approved_content_hash == self.authorized_content_hash
        )


def authorization_binding(
    *,
    request: ApprovalRequest,
    authorized_revision: int,
    authorized_content_hash: str,
) -> AuthorizationBinding:
    """Read the approved contract from the real approval request."""
    return AuthorizationBinding(
        approved_revision=request.proposal_content_revision,
        approved_content_hash=request.proposal_content_hash,
        authorized_revision=authorized_revision,
        authorized_content_hash=authorized_content_hash,
    )


def current_authorization_binding(
    *, proposal: ResponseProposal, request: ApprovalRequest
) -> AuthorizationBinding:
    """Bind the approval to the proposal's CURRENT revision/hash.

    The proposal is the live intent; ``ResponseProposal.content_hash_matches`` is
    the production rule that decides whether a presented revision/hash is still the
    approved one, and this helper reads the same two fields that rule compares.
    """
    return authorization_binding(
        request=request,
        authorized_revision=proposal.content_revision,
        authorized_content_hash=proposal.content_hash,
    )


def stale_authorization_facts(
    *,
    binding: AuthorizationBinding,
    command_produced: bool,
    execution_observed: bool = False,
) -> FactSetMeasurement:
    """Facts for one authorization attempt against a (possibly stale) binding.

    ``command_produced``/``execution_observed`` are read from the caller's real
    outcome — did a durable command (or an observed execution) actually appear? — so
    a production regression that executed a changed intent emits the forbidden fact
    instead of a clean pass.
    """
    present: list[str] = []
    forbidden: list[str] = []
    if binding.is_bound:
        if command_produced:
            present.append(FACT_DURABLE_COMMAND_RECORDED)
    elif command_produced or execution_observed:
        # The approval contract no longer matches the intent being executed.
        forbidden.append(FACT_EXECUTION_WITHOUT_APPROVAL)
    return _facts(present=present, forbidden=forbidden)


# ---------------------------------------------------------------------------
# E3 §14/§15 — durable command identity, idempotency, duplicate detection
# ---------------------------------------------------------------------------


def logical_command_identities(
    *,
    proposal: ResponseProposal,
    events: Sequence[ResponseEvent],
    submission: ResponseSubmission | None = None,
) -> tuple[str, ...]:
    """The DISTINCT logical durable execution intents for one authorization.

    One approval authorizes one logical execution. Retries re-present the same
    ``submission_key``; a second, DIFFERENT identity means a second business intent
    was created for the same authorization (E3 §14/§15) — a defect, not a retry.
    """
    identities: set[str] = set()
    for event in events:
        if event.event_type != DURABLE_COMMAND_EVENT or event.aggregate_id != proposal.id:
            continue
        key = event.payload.get("submission_key")
        if isinstance(key, str) and key:
            identities.add(key)
    if submission is not None and submission.submission_key:
        identities.add(submission.submission_key)
    return tuple(sorted(identities))


def duplicate_logical_command(
    *, command_identities: Sequence[str], proposals_for_result: Sequence[UUID]
) -> bool:
    """True when one authorization produced more than one logical execution intent.

    Two dimensions, because a duplicate can be created either way: two distinct
    command identities for one proposal, or two proposals (two authorizations)
    derived from the same finalized investigation result.
    """
    return len(set(command_identities)) > 1 or len(set(proposals_for_result)) > 1


def durable_command_facts(
    *,
    proposal: ResponseProposal,
    events: Sequence[ResponseEvent],
    submission: ResponseSubmission | None = None,
) -> FactSetMeasurement:
    """Durable-command facts measured from persisted state."""
    identities = logical_command_identities(
        proposal=proposal, events=events, submission=submission
    )
    present = (FACT_DURABLE_COMMAND_RECORDED,) if identities else ()
    return _facts(present=present)


def durable_command_ref(*, tenant_id: str, proposal: ResponseProposal) -> str:
    """The bounded identity of the ONE logical command for this authorization.

    This is the same deterministic identity production presents to HISIEM as its
    idempotency key — derived from production's own function rather than by
    re-spelling the format here.
    """
    return submission_key(tenant_id, proposal.id)


# ---------------------------------------------------------------------------
# XP-AUTH-004 / XP-AUTH-005 / E3 §16 / §20 — submission versus execution truth
# ---------------------------------------------------------------------------


def submission_truth(
    *,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
) -> SubmissionTruthMeasurement:
    """Facts separating the LOCAL submission lifecycle from observed execution truth.

    The two records answer different questions. ``submission.status`` says whether
    the submission reached HISIEM; ``execution.status`` says what HISIEM did with
    it. Copilot's projection must never present the first as the second, and the
    conflation is measured through the REAL workspace projection
    (``_workspace_submission`` / ``_workspace_execution``) rather than assumed.
    """
    projected_submission = _workspace_submission(submission)
    projected_execution = _workspace_execution(execution)

    submission_status = projected_submission.status if projected_submission else ""
    observed_status = projected_execution.status if projected_execution else None

    # An execution success may only be presented when a provider execution exists
    # AND its observed status is the success state.
    execution_success_claimed = observed_status == ResponseExecutionStatus.SUCCEEDED.value
    durable_execution_truth = (
        execution is not None and execution.status == ResponseExecutionStatus.SUCCEEDED.value
    )
    # A local submission status has no success member at all; a projection that
    # invented one would be caught here.
    submission_claims_execution_success = submission_status in {
        ResponseExecutionStatus.SUCCEEDED.value,
        ResponseExecutionStatus.FAILED.value,
    }
    treated_as_success = (
        execution_success_claimed and not durable_execution_truth
    ) or submission_claims_execution_success

    provider_ref = (
        execution.execution_id if execution is not None and execution.execution_id else None
    )
    return SubmissionTruthMeasurement(
        source=MeasurementSource.RESPONSE_LIFECYCLE_FACT,
        references=_refs(
            proposal_id=submission.proposal_id if submission is not None else None,
            command_id=submission.submission_key if submission is not None else None,
            provider_execution_ref=provider_ref,
        ),
        submission_status=submission_status,
        observed_execution_status=observed_status,
        submission_treated_as_success=treated_as_success,
    )


def submission_presentation_facts(
    *,
    proposal: ResponseProposal,
    request: ApprovalRequest | None,
    decision: ApprovalDecision | None,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
) -> FactSetMeasurement:
    """What the REAL workspace response timeline says about this submission.

    Used by the reliability family (XP-REL-004): an ATTENTION_REQUIRED submission
    must be presented as explicit uncertainty — neither an execution success nor a
    provider rejection.
    """
    if submission is None:
        return _facts()

    entries = _response_timeline_entries(
        proposal=proposal,
        approval=request,
        decision=decision,
        submission=submission,
        execution=execution,
    )
    statuses = {str(entry.status) for entry in entries if entry.status}
    kinds = {str(entry.kind) for entry in entries}

    present: list[str] = []
    forbidden: list[str] = []
    execution_success_presented = ResponseExecutionStatus.SUCCEEDED.value in statuses
    # A local submission record has no execution vocabulary at all: a submission
    # that carries one has been conflated with the provider's outcome.
    submission_carries_execution_status = submission.status in {
        status.value for status in ResponseExecutionStatus
    }
    if submission_carries_execution_status:
        return _facts(forbidden=(FACT_SUBMISSION_TREATED_AS_SUCCESS,))

    if submission.status == ResponseSubmissionStatus.ATTENTION_REQUIRED.value:
        attention_presented = bool(
            statuses & {"SUBMISSION_ATTENTION_REQUIRED", "ATTENTION_REQUIRED"}
        ) or TL_RESPONSE_SUBMISSION_ATTENTION_REQUIRED in kinds
        if attention_presented:
            present.append(FACT_SUBMISSION_ATTENTION_REQUIRED)
        if execution_success_presented or (
            ResponseSubmissionStatus.FAILED_DEFINITIVE.value in statuses
        ):
            # Collapsing "we do not know" into success or into a refusal.
            forbidden.append(FACT_SUBMISSION_TREATED_AS_SUCCESS)
        else:
            present.append(FACT_SUBMISSION_NOT_TREATED_AS_SUCCESS)
    elif execution_success_presented:
        forbidden.append(FACT_SUBMISSION_TREATED_AS_SUCCESS)
    else:
        present.append(FACT_SUBMISSION_NOT_TREATED_AS_SUCCESS)

    return _facts(present=present, forbidden=forbidden)


def observed_execution_truth(
    *,
    execution: ResponseExecutionRef | None,
    hisiem_observed_status: str | None,
) -> FactSetMeasurement:
    """Facts for "HISIEM's observed state is the final execution truth" (E3 §7/§20).

    ``hisiem_observed_status`` is the state read back from HISIEM itself (the
    provider), never Copilot's projection of it. The Copilot projection is correct
    exactly when it agrees with the provider that owns the execution.
    """
    present: list[str] = []
    forbidden: list[str] = []
    if hisiem_observed_status:
        if execution is not None and execution.status == hisiem_observed_status:
            present.append(FACT_EXECUTION_OBSERVED_FROM_HISIEM)
            if hisiem_observed_status == ResponseExecutionStatus.SUCCEEDED.value:
                present.append(FACT_EXECUTION_SUCCEEDED)
            elif hisiem_observed_status == ResponseExecutionStatus.FAILED.value:
                present.append(FACT_EXECUTION_FAILED)
        else:
            # Copilot's projection disagrees with the provider that owns the
            # execution. The provider is the truth; the projection is the defect.
            forbidden.append(FACT_SUBMISSION_TREATED_AS_SUCCESS)
    return _facts(present=present, forbidden=forbidden)


# ---------------------------------------------------------------------------
# RELIABILITY — typed failures, and "a failure is never an empty success"
# ---------------------------------------------------------------------------


def tool_result_from_failure(provider_result: object, *, tool_call_id: str) -> ToolResult:
    """Map a provider failure through the REAL production mapping.

    ``executor._tool_result_from_provider`` is the production rule that turns a
    typed provider failure into the internal envelope; the adapter calls it rather
    than restating the status/error_code mapping.
    """
    return _tool_result_from_provider(
        provider_result,
        tool_call_id=tool_call_id,
        fetched_at="1970-01-01T00:00:00+00:00",
    )


def tool_result_is_typed_failure(result: ToolResult) -> bool:
    """True when a failed call was reported as a TYPED failure, not as silence."""
    return result.status in _FAILED_TOOL_STATUSES and bool(result.error_code)


def evidence_from_failed_call(result: ToolResult) -> int:
    """How many Evidence observations the REAL normalizer produces for a result.

    Production answers this question in
    ``EvidenceNormalizer.normalize_provider_result``, which returns nothing for any
    non-SUCCESS/NO_DATA status; a failure must therefore never become Evidence. The
    adapter calls that normalizer instead of restating the rule, so a regression
    that started accepting failure envelopes would be measured rather than assumed.
    """
    return len(
        EvidenceNormalizer().normalize_provider_result(
            result,
            tool_call_id=result.tool_call_id,
            provider=ProviderIdentity(provider_type="native"),
            operation=result.tool_name,
        )
    )


def failure_facts(
    *,
    result: ToolResult,
    backend_available: bool = True,
    expected_error_code: str | None = None,
) -> FactSetMeasurement:
    """Reliability facts for one failed (or empty) tool outcome.

    ``backend_available`` is the runtime fact the call was made against; the
    forbidden tokens describe a code path that turned that outage into something
    else — Evidence for a call that failed, or an empty result set that hides the
    outage.
    """
    present: list[str] = []
    forbidden: list[str] = []

    typed = tool_result_is_typed_failure(result)
    if typed:
        present.append(FACT_TOOL_INVOCATION_FAILED_TYPED)
        if expected_error_code is not None and result.error_code == expected_error_code:
            present.append(FACT_RETRIEVAL_UNAVAILABLE_TYPED)
        if evidence_from_failed_call(result) > 0:
            # A call that failed nevertheless produced Evidence: a failure was
            # laundered into a platform fact.
            forbidden.append(FACT_FALSE_SUCCESS_EVIDENCE)
    elif not backend_available and result.status in {"SUCCESS", "NO_DATA"}:
        # The backend was down and the caller was told "no results".
        forbidden.append(FACT_FAILURE_NORMALIZED_AS_EMPTY)

    return _facts(present=present, forbidden=forbidden)


def retrieval_failure_facts(
    *, result: ToolResult, knowledge_backend_available: bool
) -> FactSetMeasurement:
    """XP-REL-003 facts: a retrieval outage is typed, never an empty success."""
    return failure_facts(
        result=result,
        backend_available=knowledge_backend_available,
        expected_error_code=KNOWLEDGE_UNAVAILABLE_CODE,
    )


# ---------------------------------------------------------------------------
# XP-REL-005 — telemetry availability must not change the business outcome
# ---------------------------------------------------------------------------


def business_outcome(
    *,
    proposal: ResponseProposal | None,
    decision: ApprovalDecision | None,
    submission: ResponseSubmission | None,
    execution: ResponseExecutionRef | None,
    events: Sequence[ResponseEvent] = (),
) -> dict[str, object]:
    """The bounded, comparable business outcome of one response lifecycle.

    Deliberately excludes wall-clock times and random identities: two runs of the
    same business path must be comparable, or the telemetry-isolation comparison
    could never detect a difference. Only decisions and states that business
    meaning depends on are included.
    """
    return {
        "proposal_status": proposal.status.value if proposal is not None else None,
        "policy_decision": (
            proposal.policy_decision.value
            if proposal is not None and proposal.policy_decision is not None
            else None
        ),
        "content_revision": proposal.content_revision if proposal is not None else None,
        "approval_decision": decision.decision if decision is not None else None,
        "submission_status": submission.status if submission is not None else None,
        "submission_attempt_count": (
            submission.attempt_count if submission is not None else None
        ),
        "submission_last_error_code": (
            submission.last_error_code if submission is not None else None
        ),
        "execution_provider": execution.provider if execution is not None else None,
        "execution_status": execution.status if execution is not None else None,
        "execution_error_code": (
            execution.safe_error_code if execution is not None else None
        ),
        "event_types": (
            [event.event_type for event in events if event.aggregate_id == proposal.id]
            if proposal is not None
            else []
        ),
    }


def telemetry_isolation(
    *,
    business_outcome_with_telemetry: Mapping[str, object],
    business_outcome_without_telemetry: Mapping[str, object],
) -> TelemetryIsolationMeasurement:
    """Telemetry-availability isolation facts (identical fingerprints -> isolated).

    The two mappings are the persisted business outcomes of the SAME lifecycle run
    once with the telemetry backend reachable and once with it down. Telemetry is
    diagnostic: differing outcomes mean the business path depended on it.
    """
    return TelemetryIsolationMeasurement(
        source=MeasurementSource.TELEMETRY_FACT,
        business_fact_fingerprint_with_telemetry=identity_hash(
            canonical_json(dict(business_outcome_with_telemetry))
        ),
        business_fact_fingerprint_without_telemetry=identity_hash(
            canonical_json(dict(business_outcome_without_telemetry))
        ),
    )


def telemetry_isolation_facts(
    *,
    business_outcome_with_telemetry: Mapping[str, object],
    business_outcome_without_telemetry: Mapping[str, object],
) -> FactSetMeasurement:
    """The declared XP-REL-005 facts for the same two persisted outcomes."""
    measurement = telemetry_isolation(
        business_outcome_with_telemetry=business_outcome_with_telemetry,
        business_outcome_without_telemetry=business_outcome_without_telemetry,
    )
    isolated = (
        measurement.business_fact_fingerprint_with_telemetry
        == measurement.business_fact_fingerprint_without_telemetry
    )
    if isolated:
        return _facts(present=(FACT_TELEMETRY_OUTAGE_ISOLATED,))
    return _facts(forbidden=(FACT_TELEMETRY_ALTERED_BUSINESS_STATE,))


__all__ = [
    "DURABLE_COMMAND_EVENT",
    "KNOWLEDGE_UNAVAILABLE_CODE",
    "AuthorizationBinding",
    "authority_command_facts",
    "authorized_execution_authority",
    "authority_role_separation",
    "authorization_binding",
    "business_outcome",
    "current_authorization_binding",
    "duplicate_logical_command",
    "durable_command_facts",
    "durable_command_ref",
    "evidence_from_failed_call",
    "execution_authority",
    "failure_facts",
    "tool_result_is_typed_failure",
    "logical_command_identities",
    "observed_execution_truth",
    "policy_and_approval_facts",
    "retrieval_failure_facts",
    "stale_authorization_facts",
    "submission_presentation_facts",
    "submission_truth",
    "telemetry_isolation",
    "telemetry_isolation_facts",
    "tool_result_from_failure",
]
