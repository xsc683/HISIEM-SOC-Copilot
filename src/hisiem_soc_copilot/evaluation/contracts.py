"""Evaluation working-context contracts — the SINGLE authoritative shared boundary.

This module is the frozen interface contract for the GP-01 dataset materializer
(E1-B.3) and the manifest sealer (E1-B.4). It lives in its own top-level
``evaluation`` package (not under domain/application/agent/infrastructure) and may
depend on production public contracts (e.g. ``contracts``), never the reverse:
production packages MUST NOT import evaluation oracle code.

Type separation is load-bearing and must be preserved:

    MaterializationDraft  --recoverable mutable run ledger, never sealed/scored.
            |  (DatasetVerifier only)
            v
    VerifiedDataset  ----sealed from here, never from a Draft.----
            |
            v
    SealedManifest   --immutable; its launch projection is the ONLY thing a
                       production investigation may ever see.

``Sealer``/``ManifestBuilder`` accept only a ``VerifiedDataset``; sealing an
unverified ``MaterializationDraft`` is not a supported operation.

Secrets, bearer tokens, API keys, Authorization material, and raw environment
dumps must never appear in any object defined here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

# ---------------------------------------------------------------------------
# Scenario identity + logical event definitions (E1-B.3 §3, §5)
# ---------------------------------------------------------------------------

# The committed GP-01 detection-rule contract the materializer MUST preflight
# against (E1-B.3 §10.3, verified against the deployed SIEM rule). A mismatch
# raises ``RuleContractMismatch`` and nothing is written.
GP01_RULE_ID = "rule-ssh-brute-force-001"
GP01_RULE_KEY_FIELD = "source.ip"
GP01_RULE_CONDITION = "authentication_failure"
GP01_RULE_THRESHOLD = 5
GP01_RULE_WINDOW_MINUTES = 5

# The real SSH TCP log input (E1-B.3 §2; SIEM log-source ``ls-54fc7d96``,
# template ``ssh-auth``) and the parser timezone the syslog form requires.
# The SSH syslog form does not carry a year; the SIEM Logstash date filter is
# configured for ``Asia/Shanghai`` and auto-completes the current year.
GP01_SSH_TCP_HOST = "127.0.0.1"
GP01_SSH_TCP_PORT = 5007
GP01_SYSLOG_TIMEZONE = "Asia/Shanghai"

GP01_EVENT_ORDER: tuple[str, ...] = ("F1", "F2", "F3", "F4", "F5", "S1", "W1")
GP01_SEMANTIC_ROLES: tuple[str, ...] = ("F1", "F2", "F3", "F4", "F5", "S1")
GP01_FAILURE_ROLES: tuple[str, ...] = ("F1", "F2", "F3", "F4", "F5")

# The GP-01 evidence contract (E1-B.4 §12, correctness round): the SOURCE ALERT
# authoritatively proves the brute-force threshold (>= 5 failures on one source),
# so the Agent must DISCOVER the subsequent S1 success — NOT re-retrieve F1..F5.
# F1..F5 remain GROUND_TRUTH dataset events, but only S1 is a required
# agent-discovered evidence role. This must NOT be treated as "which events belong
# to the dataset".
GP01_REQUIRED_EVIDENCE_ROLES: tuple[str, ...] = ("S1",)


@dataclass(frozen=True)
class ScenarioSpec:
    """Provider-neutral description of the committed logical GP-01 scenario.

    This is the parsed, canonical scenario the materializer binds concrete
    timestamps/identities to. It deliberately contains NO fixed Finding/verdict
    wording — the oracle records semantic facts (E1-B.4 §12), never sentences.
    """

    id: str = "gp-01"
    version: str = "1"
    rule_id: str = GP01_RULE_ID
    semantic_roles: tuple[str, ...] = GP01_SEMANTIC_ROLES
    failure_roles: tuple[str, ...] = GP01_FAILURE_ROLES
    control_role: str = "W1"
    expected_verdict: str = "MALICIOUS"
    # Ordered semantic facts the oracle persists (E1-B.4 §12). Each is a bounded
    # (id, description) pair; list order is part of the canonical manifest.
    facts: tuple[tuple[str, str], ...] = (
        (
            "FAILURE_SEQUENCE",
            "at least five SSH authentication failures, same attack source, "
            "same target account, same target host",
        ),
        (
            "POST_FAILURE_SUCCESS",
            "an SSH authentication success exists for the same source, account, "
            "and host and occurs after the failure sequence",
        ),
    )
    # Evidence roles the AGENT must independently discover (E1-B.4 §12). The source
    # alert proves the failure threshold, so this is the S1 success only — NOT the
    # full semantic ground-truth set (F1..F5 stay GROUND_TRUTH dataset events but
    # are not "must re-retrieve" provider events).
    required_evidence_roles: tuple[str, ...] = GP01_REQUIRED_EVIDENCE_ROLES


@dataclass(frozen=True)
class LogicalEvent:
    """One committed logical GP-01 event (F1..F5, S1) or control (W1)."""

    role: str
    action: str  # authentication_failure | authentication_success
    outcome: str  # failure | success
    classification: Literal["GROUND_TRUTH", "WATERMARK_CONTROL"]


# The committed logical dataset. The materializer binds a RunIdentity + time plan
# to these; it never mutates the committed scenario.
GP01_LOGICAL_DATASET: tuple[LogicalEvent, ...] = (
    LogicalEvent("F1", "authentication_failure", "failure", "GROUND_TRUTH"),
    LogicalEvent("F2", "authentication_failure", "failure", "GROUND_TRUTH"),
    LogicalEvent("F3", "authentication_failure", "failure", "GROUND_TRUTH"),
    LogicalEvent("F4", "authentication_failure", "failure", "GROUND_TRUTH"),
    LogicalEvent("F5", "authentication_failure", "failure", "GROUND_TRUTH"),
    LogicalEvent("S1", "authentication_success", "success", "GROUND_TRUTH"),
    LogicalEvent("W1", "authentication_failure", "failure", "WATERMARK_CONTROL"),
)


@dataclass(frozen=True)
class RunIdentity:
    """Deterministic runtime identity derived from a ``run_id`` (E1-B.3 §5).

    Invariant: ``attack_source_ip != watermark_source_ip``. Same ``run_id`` MUST
    yield the same identities; different run_ids normally yield different attack
    sources so prior detection suppression state cannot contaminate a new run.
    """

    run_id: str
    run_tag: str
    attack_source_ip: str
    watermark_source_ip: str
    user_name: str
    host_name: str


# ---------------------------------------------------------------------------
# Time plan (E1-B.3 §6)
# ---------------------------------------------------------------------------

# Detection-window anchor offsets live in ``time_plan.py`` (the builder owns the
# exact anchor math); the contracts only fix the invariants + the year-boundary
# rejection code (the SSH syslog form has no year; see E1-B.3 §6).
EVENT_PLAN_CROSSES_YEAR_BOUNDARY = "EVENT_PLAN_CROSSES_YEAR_BOUNDARY"


@dataclass(frozen=True)
class EventTimePlan:
    """Past-bound timestamps per logical role in ``Asia/Shanghai`` wall time.

    ``anchor_local`` is the base wall clock (timezone-aware, Asia/Shanghai). Each
    role has an RFC3339 UTC instant (the provider ingests/returns RFC3339 UTC) and
    a local wall-clock datetime used to render the (year-less) syslog line the
    parser re-interprets in the configured timezone.
    """

    anchor_local: datetime
    events: dict[str, datetime]  # role -> RFC3339 UTC instant (deterministic)
    wall_clock: dict[str, datetime]  # role -> Asia/Shanghai local wall clock

    def failure_window_start(self) -> datetime:
        return self.events["F1"]

    def max_failure_time(self) -> datetime:
        return max(self.events[role] for role in GP01_FAILURE_ROLES)

    def success_time(self) -> datetime:
        return self.events["S1"]

    def watermark_time(self) -> datetime:
        return self.events["W1"]


# ---------------------------------------------------------------------------
# Rendered / injected / resolved (E1-B.3 §7, §8, §11, §12, §13)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderedEvent:
    """One real OpenSSH-style syslog line bound to a logical role."""

    role: str
    action: str
    outcome: str
    host_name: str
    source_ip: str
    user_name: str
    wall_second: int
    line: str  # the exact bytes sent to the TCP socket
    payload_sha256: str


@dataclass(frozen=True)
class InjectionAttempt:
    """Bounded audit of ONE attempted TCP injection (E1-B.3 §11, §12).

    ``write_status`` is one of ``accepted`` / ``indeterminate`` / ``rejected`` /
    ``connection_error``. Secrets/authorization material are never recorded.
    """

    logical_role: str
    attempted_at: str  # RFC3339 UTC
    payload_sha256: str
    socket_target: str
    write_status: str


@dataclass(frozen=True)
class ResolvedEvent:
    """A real HISIEM event reference proven to correspond to one logical role.

    Contains ONLY bounded normalized fields needed to prove scenario identity and
    later correlation — never a complete Elasticsearch document (E1-B.3 §8).
    ``index``/``document_id`` are the exact values returned by HISIEM; they MUST
    NOT be derived from logical roles or hashes. ``message_fingerprint`` is the
    materializer-only correlation aid the rendered line carries (it is a resolver
    aid, never provider identity).
    """

    logical_role: str
    provider: str
    index: str
    document_id: str
    timestamp: str  # RFC3339 UTC @timestamp from HISIEM
    event_category: str
    event_action: str
    event_outcome: str | None
    source_ip: str
    user_name: str
    host_name: str
    log_source_id: str | None
    message_fingerprint: str | None


# ---------------------------------------------------------------------------
# Alert resolution (E1-B.3 §14, §15)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelatedEventRef:
    """A stable provider reference to an event the alert correlates to."""

    index: str
    document_id: str


@dataclass(frozen=True)
class ResolvedAlert:
    """A real, stable HISIEM alert bound to the current run (E1-B.3 §8).

    ``address_id`` MUST be the actual identifier accepted by the HISIEM alert
    detail API (for the current SIEM alert implementation, the Elasticsearch
    document ``_id``). It is NEVER derived from ``alert.id``/``business_id``,
    run_id, scenario ids, timestamps, or hashes. ``business_id`` is optional
    display/correlation metadata only.
    """

    provider: str
    address_id: str
    business_id: str | None
    rule_id: str
    rule_name: str | None
    entity: str | None
    created_at: str
    event_count: int
    status: str
    related_event_refs: list[RelatedEventRef] = field(default_factory=list)

    @property
    def fingerprint(self) -> tuple[str, ...]:
        """Stability-barrier fingerprint (E1-B.3 §15): address_id, rule_id,
        source/entity, event_count, related-event identity set, status."""
        return (
            self.address_id,
            self.rule_id,
            str(self.entity or ""),
            str(self.event_count),
            ",".join(sorted(f"{r.index}:{r.document_id}" for r in self.related_event_refs)),
            str(self.status or ""),
        )


# ---------------------------------------------------------------------------
# Dataset states (E1-B.3 §8, §17; E1-B.4 §2)
# ---------------------------------------------------------------------------


@dataclass
class MaterializationDraft:
    """The mutable recovery ledger for one run (materialization.json).

    Records state, attempted injections, resolution progress, and failure
    diagnostics so a resume/reconciliation can pick up where a run stopped. It is
    NOT an evaluation manifest and MUST NEVER be sealed or scored. ``state`` moves
    along the MaterializationState machine (E1-B.3 §9).
    """

    run_id: str
    scenario_id: str
    tenant_id: str
    state: str = "NEW"
    created_at: str = ""  # RFC3339 UTC
    updated_at: str = ""
    bound: bool = False
    identity: RunIdentity | None = None
    time_plan: EventTimePlan | None = None
    injected: list[InjectionAttempt] = field(default_factory=list)
    rendered: list[RenderedEvent] = field(default_factory=list)
    resolved_events: dict[str, ResolvedEvent] = field(default_factory=dict)
    resolved_alert: ResolvedAlert | None = None
    failure: str | None = None  # typed error code + bounded context
    # Frozen instant the DatasetVerifier produced a VerifiedDataset (RFC3339 UTC).
    # Distinct from ``updated_at`` (later ledger checkpoints change that); the
    # sealed ``VerifiedDataset.materialized_at`` is recovered from THIS value, never
    # regenerated at seal time. Lives only in the mutable ledger — no sealed
    # manifest schema change.
    verified_at: str = ""


@dataclass(frozen=True)
class VerifiedDataset:
    """The ONLY input the Sealer accepts (E1-B.4 §2).

    Produced by the DatasetVerifier when every materialization invariant holds
    (E1-B.3 §16). Immutable; the Sealer/ManifestBuilder never mutate it. ``scope``
    preserves the evaluation scope used to resolve provider resources.
    """

    scenario: ScenarioSpec
    run: RunIdentity
    tenant_id: str
    time_plan: EventTimePlan
    source_alert: ResolvedAlert
    resolved_events: dict[str, ResolvedEvent]  # keyed by logical role
    materialized_at: str = ""  # RFC3339 UTC

    @property
    def events(self) -> list[ResolvedEvent]:
        """Semantic ground-truth events in committed role order (F1..F5, S1)."""
        return [self.resolved_events[r] for r in GP01_SEMANTIC_ROLES]

    @property
    def control_events(self) -> list[ResolvedEvent]:
        return [self.resolved_events[r] for r in (GP01_LOGICAL_DATASET[-1].role,)]

    def integrity_identity(self) -> str:
        """Deterministic run+scenario identity used by the sealer/manifest."""
        return f"{self.scenario.id}/{self.run.run_id}"


# ---------------------------------------------------------------------------
# Canonical JSON + hashing primitives (E1-B.4 §15, §16; scenario semantic hash)
# ---------------------------------------------------------------------------

# The project-owned, versioned canonical-serialization identifier. Any manifest
# that uses it MUST be canonicalized with exactly these rules.
CANONICALIZATION_ID = "json-sort-keys-v1"

# The immutable sealed-manifest schema version (E1-B.4 §5).
MANIFEST_SCHEMA_VERSION = "gp-eval-manifest/v1"


def canonical_json(payload: dict[str, Any] | list[Any]) -> str:
    """Serialize to the canonical UTF-8 compact form (E1-B.4 §15).

    Rules: UTF-8 (``ensure_ascii=False``), lexical sorted keys, compact
    separators, finite JSON numbers only (NaN/Infinity rejected via
    ``allow_nan=False``), no implementation-dependent Unicode re-encoding.
    List ordering MUST be established by the caller BEFORE calling this (sort_keys
    does not order arrays). Timestamps must already be canonical RFC3339 UTC.
    """
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: str) -> str:
    """UTF-8 SHA-256 hex digest of canonical string content."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Materialization state machine + typed errors (E1-B.3 §9, §19)
# ---------------------------------------------------------------------------


class MaterializationState(StrEnum):
    """Explicit materialization state machine.

    Normal path: NEW -> PREFLIGHTED -> EVENTS_RENDERED -> EVENTS_INJECTED ->
    EVENTS_RESOLVED -> ALERT_RESOLVED -> VERIFIED -> MATERIALIZED.
    Failure states: FAILED, INDETERMINATE (non-idempotent TCP write whose server
    outcome cannot be proven — see E1-B.3 §12).
    """

    NEW = "NEW"
    PREFLIGHTED = "PREFLIGHTED"
    EVENTS_RENDERED = "EVENTS_RENDERED"
    EVENTS_INJECTED = "EVENTS_INJECTED"
    EVENTS_RESOLVED = "EVENTS_RESOLVED"
    ALERT_RESOLVED = "ALERT_RESOLVED"
    VERIFIED = "VERIFIED"
    MATERIALIZED = "MATERIALIZED"
    FAILED = "FAILED"
    INDETERMINATE = "INDETERMINATE"


class EvaluationError(Exception):
    """Base for all evaluation-package typed failures (bounded, no secrets)."""

    code = "EVALUATION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.code


class PreflightError(EvaluationError):
    code = "PREFLIGHT_ERROR"


class InvalidMaterializationTransition(EvaluationError):
    """A state-machine call is illegal for the current MaterializationState.

    The state machine is a real transition gate (not merely a record): calling
    e.g. ``inject_events()`` from NEW, or ``resolve_events()`` before the events
    are injected, is a typed failure — never a bare ``RuntimeError``.
    """

    code = "INVALID_MATERIALIZATION_TRANSITION"


class RuleContractMismatch(PreflightError):
    code = "RULE_CONTRACT_MISMATCH"


class RunIdentityCollision(PreflightError):
    code = "RUN_IDENTITY_COLLISION"


class EventInjectionError(EvaluationError):
    code = "EVENT_INJECTION_ERROR"


class InjectionOutcomeIndeterminate(EventInjectionError):
    """A TCP write whose server-side outcome cannot be proven (E1-B.3 §12).

    The run MUST go INDETERMINATE; the event MUST NOT be blindly re-sent. A rerun
    with the same run_id reconciles and resolves instead of re-injecting.
    """

    code = "INDETERMINATE"


class EventResolutionTimeout(EvaluationError):
    code = "EVENT_RESOLUTION_TIMEOUT"


class AmbiguousEventError(EvaluationError):
    code = "AMBIGUOUS_EVENT"


class AlertResolutionTimeout(EvaluationError):
    code = "ALERT_RESOLUTION_TIMEOUT"


class AmbiguousSourceAlertError(EvaluationError):
    code = "AMBIGUOUS_SOURCE_ALERT"


class AlertNotStableError(EvaluationError):
    code = "ALERT_NOT_STABLE"


class DatasetInvariantViolation(EvaluationError):
    code = "DATASET_INVARIANT_VIOLATION"


# ---------------------------------------------------------------------------
# Canonical manifest payload (E1-B.4 §5) + sealed artifact
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeRevision:
    """Code revision under which a manifest was generated (E1-B.4 §17).

    An authoritative evaluation record requires ``dirty=False``; a dirty worktree
    may produce a non-authoritative development artifact but must never be labeled
    equivalent to a clean sealed benchmark record.
    """

    git_commit: str
    dirty: bool


@dataclass(frozen=True)
class ScenarioOracle:
    """Private GP-01 oracle for the scorer (E1-B.4 §12).

    Describes semantic FACTS and evidence requirements — never fixed Finding
    wording. Grounded investigation evaluation, not prompt memorization.
    """

    expected_verdict: str = "MALICIOUS"
    facts: tuple[tuple[str, str], ...] = ()
    required_evidence_roles: tuple[str, ...] = ()


def oracle_from_scenario(scenario: ScenarioSpec) -> ScenarioOracle:
    """Derive the canonical oracle from a ScenarioSpec (E1-B.4 §12)."""
    return ScenarioOracle(
        expected_verdict=scenario.expected_verdict,
        facts=scenario.facts,
        required_evidence_roles=scenario.required_evidence_roles,
    )


@dataclass(frozen=True)
class EvaluationLaunchRef:
    """The ONLY projection of a SealedManifest a production investigation may see
    (E1-B.4 §13, §14). Never contains oracle data. Maps 1:1 onto the production
    ``ExternalResourceRef`` used to start an investigation."""

    provider: str
    resource_type: str
    address_id: str
    business_id: str | None = None


@dataclass(frozen=True)
class SealedManifest:
    """An immutable sealed evaluation artifact (manifest.json).

    Integrity: ``manifest_sha256`` = SHA256(canonical_json(manifest with
    integrity.manifest_sha256 omitted)). ``code``/``dirty`` mark whether this is an
    authoritative clean-benchmark record. The oracle is present here for the
    harness/scorer; production investigation code receives only the
    ``launch_projection``.
    """

    schema_version: str
    scenario: ScenarioSpec
    run: RunIdentity
    scope: dict[str, str]  # provider/tenant_id etc.
    entities: dict[str, str]
    events: list[ResolvedEvent]  # GROUND_TRUTH semantic roles, committed order
    control_events: list[ResolvedEvent]
    source_alert: ResolvedAlert
    oracle: ScenarioOracle
    code: CodeRevision
    integrity: dict[str, str]  # canonicalization id + manifest_sha256
    sealed_at: str = ""  # RFC3339 UTC
    materialized_at: str = ""

    @property
    def launch_projection(self) -> EvaluationLaunchRef:
        return EvaluationLaunchRef(
            provider=self.source_alert.provider,
            resource_type="alert",
            address_id=self.source_alert.address_id,
            business_id=self.source_alert.business_id,
        )


# Re-export the authoritative run identity + canonical markers used by the sealer.
__all__ = [
    "ScenarioSpec",
    "RunIdentity",
    "LogicalEvent",
    "GP01_LOGICAL_DATASET",
    "EventTimePlan",
    "RenderedEvent",
    "InjectionAttempt",
    "ResolvedEvent",
    "RelatedEventRef",
    "ResolvedAlert",
    "MaterializationDraft",
    "VerifiedDataset",
    "MaterializationState",
    "ScenarioOracle",
    "CodeRevision",
    "EvaluationLaunchRef",
    "SealedManifest",
    "EvaluationError",
    "PreflightError",
    "InvalidMaterializationTransition",
    "RuleContractMismatch",
    "RunIdentityCollision",
    "EventInjectionError",
    "InjectionOutcomeIndeterminate",
    "EventResolutionTimeout",
    "AmbiguousEventError",
    "AlertResolutionTimeout",
    "AmbiguousSourceAlertError",
    "AlertNotStableError",
    "DatasetInvariantViolation",
    "GP01_RULE_ID",
    "GP01_RULE_KEY_FIELD",
    "GP01_RULE_CONDITION",
    "GP01_RULE_THRESHOLD",
    "GP01_RULE_WINDOW_MINUTES",
    "GP01_SSH_TCP_HOST",
    "GP01_SSH_TCP_PORT",
    "GP01_SYSLOG_TIMEZONE",
    "GP01_EVENT_ORDER",
    "GP01_SEMANTIC_ROLES",
    "GP01_FAILURE_ROLES",
    "GP01_REQUIRED_EVIDENCE_ROLES",
    "EVENT_PLAN_CROSSES_YEAR_BOUNDARY",
    "CANONICALIZATION_ID",
    "MANIFEST_SCHEMA_VERSION",
    "canonical_json",
    "sha256_hex",
    "oracle_from_scenario",
]
