"""E5 workspace measurement adapters: what the workspace PRESENTS -> E1 measurements.

Stage E / E5 owns the WORKSPACE family. This module is the middle of the E5 flow,
exactly as ``cross_plane_measure`` / ``cross_plane_authority`` /
``cross_plane_observability`` are for E2 / E3 / E4:

```text
persisted authority facts + the REAL workspace projection + the REAL frontend
derivation
        -> THIS MODULE (measure presentation facts)
        -> E1 typed measurement contract
        -> E1 deterministic hard gate
        -> cross-plane-gate-results/v1
```

The frozen rule this module exists to keep measurable (E5 §8):

```text
Workspace projects truth.
Workspace does not create truth.
```

Two rules shape every function here:

* **Measure, do not re-derive.** The workspace projection is built by
  ``application.services.workspace_service`` and the authority classes are derived
  by the frontend's own ``utils/copilot.js``. This module receives both and compares
  them against the persisted facts — it never recomputes a projection or a label,
  so evaluation can never disagree with production about what the workspace shows.
* **Bounded projections only.** No raw workspace payload, no evidence body, no
  timeline dump: the adapters carry ids, classes, and status words (E5 §22).

Read-only: nothing here writes, renders, or authorizes anything.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from ..evaluation.cross_plane import (
    FactSetMeasurement,
    MeasurementSource,
    dedupe_bounded_ids,
)

#: Frozen XP-01 fact tokens this module may emit. Every one is declared in
#: ``evaluation.cross_plane.gates``.
FACT_AUTHORITY_LABELS_PRESENT = "WORKSPACE_AUTHORITY_LABELS_PRESENT"
FACT_RECONSTRUCTED_FROM_DURABLE_STATE = "WORKSPACE_RECONSTRUCTED_FROM_DURABLE_STATE"
FACT_STALE_OVERRIDDEN_BY_REFRESH = "WORKSPACE_STALE_OVERRIDDEN_BY_REFRESH"
FACT_INVENTED_AUTHORITY = "WORKSPACE_INVENTED_AUTHORITY"

#: The authority classes the workspace must keep distinct (E5 §10). Class names are
#: the frontend's own vocabulary (``AUTHORITY_LABELS`` keys) plus the presentation
#: planes the response lifecycle adds.
PLATFORM_FACT = "PLATFORM_FACT"
KNOWLEDGE_CONTEXT = "KNOWLEDGE_CONTEXT"
MODEL_FINDING = "MODEL_FINDING"
AGENT_VERDICT = "AGENT_VERDICT"
POLICY_DECISION = "POLICY_DECISION"
HUMAN_DECISION = "HUMAN_DECISION"
EXECUTION_RESULT = "EXECUTION_RESULT"

REQUIRED_AUTHORITY_CLASSES: tuple[str, ...] = (
    PLATFORM_FACT,
    KNOWLEDGE_CONTEXT,
    MODEL_FINDING,
    AGENT_VERDICT,
    POLICY_DECISION,
    HUMAN_DECISION,
    EXECUTION_RESULT,
)

#: Supporting-context authority classes: valid as CONTEXT, never as observed fact.
SUPPORTING_CONTEXT_CLASSES: frozenset[str] = frozenset({KNOWLEDGE_CONTEXT})

#: The status words a workspace may present for an execution result. Only these may
#: justify a terminal execution presentation (E5 §14).
EXECUTION_TERMINAL_STATUSES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED"})

#: Local submission states. NONE of them is an execution outcome (E5 §14).
SUBMISSION_STATUSES: frozenset[str] = frozenset(
    {"PENDING", "RETRYING", "SUBMITTED", "FAILED_DEFINITIVE", "ATTENTION_REQUIRED"}
)


@dataclass(frozen=True)
class PresentedWorkspace:
    """Bounded projection of what the workspace PRESENTS for one investigation."""

    evidence_authority: Mapping[str, str] = field(default_factory=dict)
    finding_ids: tuple[str, ...] = ()
    agent_verdict: str = ""
    policy_decision: str | None = None
    human_decision: str | None = None
    submission_status: str | None = None
    execution_status: str | None = None
    proposal_status: str = ""
    timeline_statuses: tuple[str, ...] = ()

    def presented_classes(self) -> tuple[str, ...]:
        """Which authority classes the workspace actually shows, in catalog order."""
        classes: list[str] = []
        values = set(self.evidence_authority.values())
        if PLATFORM_FACT in values:
            classes.append(PLATFORM_FACT)
        if KNOWLEDGE_CONTEXT in values:
            classes.append(KNOWLEDGE_CONTEXT)
        if self.finding_ids:
            classes.append(MODEL_FINDING)
        if self.agent_verdict:
            classes.append(AGENT_VERDICT)
        if self.policy_decision:
            classes.append(POLICY_DECISION)
        if self.human_decision:
            classes.append(HUMAN_DECISION)
        # The execution plane is presented by the provider execution when one exists,
        # and by the LOCAL submission lifecycle before that (PENDING / RETRYING /
        # ATTENTION_REQUIRED are the workspace's execution-plane facts in those
        # states). E5 §13 requires both to be shown distinctly, never collapsed.
        if self.execution_status or self.submission_status:
            classes.append(EXECUTION_RESULT)
        return tuple(classes)


@dataclass(frozen=True)
class PersistedWorkspaceFacts:
    """The persisted truth the presentation is measured against."""

    evidence_source_types: Mapping[str, str] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    agent_verdict: str = ""
    policy_decision: str | None = None
    human_decision: str | None = None
    submission_status: str | None = None
    execution_status: str | None = None
    proposal_status: str = ""


def _facts(*, present: Sequence[str] = (), forbidden: Sequence[str] = ()) -> FactSetMeasurement:
    """A bounded fact set sourced from the workspace projection (E1 §26)."""
    return FactSetMeasurement(
        source=MeasurementSource.WORKSPACE_PROJECTION_FACT,
        observed_facts=dedupe_bounded_ids([*present, *forbidden], what="observed_facts"),
    )


# ---------------------------------------------------------------------------
# XP-UX-001 — workspace authority semantics
# ---------------------------------------------------------------------------


def invented_authority(
    *,
    presented: PresentedWorkspace,
    persisted: PersistedWorkspaceFacts,
) -> tuple[str, ...]:
    """The authority claims the workspace presents WITHOUT a persisted source.

    A workspace may only present what persisted truth supports: an authority class
    with no persisted origin, a verdict the result never carried, a human decision no
    ApprovalDecision recorded, or an execution result no provider execution exists
    for are all "invented authority".
    """
    reasons: list[str] = []

    # (a) an evidence authority class that the persisted source type cannot support
    for evidence_id, authority in presented.evidence_authority.items():
        source_type = persisted.evidence_source_types.get(evidence_id)
        if source_type is None:
            reasons.append("evidence_without_persisted_source")
            continue
        if authority in SUPPORTING_CONTEXT_CLASSES and source_type.startswith("HISIEM_"):
            reasons.append("supporting_context_labelled_as_platform_fact")
        if authority == PLATFORM_FACT and source_type == "KNOWLEDGE":
            reasons.append("knowledge_context_labelled_as_platform_fact")

    # (b) findings, verdict, policy, decision and execution must all be persisted
    if set(presented.finding_ids) - set(persisted.finding_ids):
        reasons.append("finding_without_persisted_row")
    if presented.agent_verdict and presented.agent_verdict != persisted.agent_verdict:
        reasons.append("verdict_not_from_persisted_result")
    if presented.policy_decision and presented.policy_decision != persisted.policy_decision:
        reasons.append("policy_decision_not_from_persisted_proposal")
    if presented.human_decision and presented.human_decision != persisted.human_decision:
        reasons.append("human_decision_without_persisted_decision")
    if presented.execution_status and presented.execution_status != persisted.execution_status:
        reasons.append("execution_result_without_persisted_execution")
    if (
        presented.submission_status
        and presented.submission_status != persisted.submission_status
    ):
        # Presenting a submission state the local lifecycle never recorded — most
        # importantly claiming a provider REFUSAL while the truth is that the retry
        # budget ran out and nobody knows (E5 §13).
        reasons.append("submission_state_not_from_persisted_lifecycle")

    # (c) a terminal execution presentation with no observed terminal truth
    if presented.execution_status in EXECUTION_TERMINAL_STATUSES and (
        persisted.execution_status not in EXECUTION_TERMINAL_STATUSES
    ):
        reasons.append("terminal_execution_without_observed_truth")

    return tuple(dedupe_bounded_ids(sorted(set(reasons)), what="reasons"))


def workspace_authority_facts(
    *,
    presented: PresentedWorkspace,
    persisted: PersistedWorkspaceFacts,
) -> FactSetMeasurement:
    """XP-UX-001 facts: every authority class present, none invented."""
    if invented_authority(presented=presented, persisted=persisted):
        return _facts(forbidden=(FACT_INVENTED_AUTHORITY,))
    classes = set(presented.presented_classes())
    if not set(REQUIRED_AUTHORITY_CLASSES) <= classes:
        # A class the investigation genuinely has is not shown at all: the workspace
        # is collapsing distinctions rather than presenting them.
        return _facts()
    return _facts(present=((FACT_AUTHORITY_LABELS_PRESENT,)))


def missing_authority_classes(*, presented: PresentedWorkspace) -> tuple[str, ...]:
    """The authority classes the investigation has but the workspace does not show."""
    return tuple(sorted(set(REQUIRED_AUTHORITY_CLASSES) - set(presented.presented_classes())))


# ---------------------------------------------------------------------------
# XP-UX-002 — refresh and stale reconstruction
# ---------------------------------------------------------------------------


def presentation_identity(presented: PresentedWorkspace) -> tuple[object, ...]:
    """The semantic identity of a presentation (stable order, no timestamps)."""
    return (
        tuple(sorted(presented.evidence_authority.items())),
        tuple(sorted(presented.finding_ids)),
        presented.agent_verdict,
        presented.policy_decision,
        presented.human_decision,
        presented.submission_status,
        presented.execution_status,
        presented.proposal_status,
        tuple(sorted(presented.timeline_statuses)),
    )


def workspace_reconstruction_facts(
    *,
    replayed: PresentedWorkspace,
    original: PresentedWorkspace,
    stale_client: PresentedWorkspace,
    server_truth: PresentedWorkspace,
    persisted: PersistedWorkspaceFacts,
) -> FactSetMeasurement:
    """XP-UX-002 facts: durable reconstruction, and server truth over stale client state.

    ``replayed`` is a SECOND projection built from persisted state alone (a fresh
    load / page refresh / reopened investigation); ``server_truth`` is the persisted
    truth that projection must reproduce; ``stale_client`` is an older client snapshot
    whose state is behind the server. ``original`` records what the session saw before
    the refresh and is reported for context — reconstruction is measured against
    PERSISTED truth, never against transient browser memory.
    """
    present: list[str] = []
    forbidden: list[str] = []

    if invented_authority(presented=replayed, persisted=persisted):
        forbidden.append(FACT_INVENTED_AUTHORITY)
    if presentation_identity(replayed) == presentation_identity(server_truth):
        present.append(FACT_RECONSTRUCTED_FROM_DURABLE_STATE)

    if presentation_identity(stale_client) != presentation_identity(server_truth):
        # The stale snapshot differs from the server: the refresh must land on the
        # SERVER's presentation, and must not resurrect the client's.
        if presentation_identity(replayed) == presentation_identity(server_truth):
            present.append(FACT_STALE_OVERRIDDEN_BY_REFRESH)
        elif presentation_identity(replayed) == presentation_identity(stale_client):
            forbidden.append(FACT_INVENTED_AUTHORITY)

    return _facts(present=present, forbidden=forbidden)


def stale_client_wins(*, replayed: PresentedWorkspace, stale_client: PresentedWorkspace) -> bool:
    """True when a refresh landed on the stale client state instead of server truth."""
    return presentation_identity(replayed) == presentation_identity(stale_client)


__all__ = [
    "AGENT_VERDICT",
    "EXECUTION_RESULT",
    "HUMAN_DECISION",
    "KNOWLEDGE_CONTEXT",
    "MODEL_FINDING",
    "PLATFORM_FACT",
    "POLICY_DECISION",
    "PresentedWorkspace",
    "PersistedWorkspaceFacts",
    "REQUIRED_AUTHORITY_CLASSES",
    "SUBMISSION_STATUSES",
    "SUPPORTING_CONTEXT_CLASSES",
    "invented_authority",
    "missing_authority_classes",
    "presentation_identity",
    "stale_client_wins",
    "workspace_authority_facts",
    "workspace_reconstruction_facts",
]
