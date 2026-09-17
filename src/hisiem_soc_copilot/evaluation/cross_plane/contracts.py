"""XP-01 cross-plane evaluation contracts (Stage E / E1).

XP-01 is a **sibling scenario family inside the existing Evaluation Plane**, not a
second evaluation framework. It is evaluation-only: nothing here is a Domain
entity, nothing is persisted into production tables, and no production layer may
import this package (enforced by ``tests/architecture/test_evaluation_boundary.py``
and ``tests/architecture/test_knowledge_boundary.py``).

Scope of this module: the frozen *shape* of XP-01 — pack identity, gate families,
execution profiles, measurement source categories, the immutable scenario spec, the
immutable gate result, and the per-scenario gate-results payload. Gate *semantics*
(gate ids, reason codes, measurement variants, evaluators) live in ``gates.py``;
the audited scenario catalog lives in ``catalog.py``; artifact build/validate lives
in ``artifacts.py``.

Two rules this module exists to make structural rather than aspirational:

* **Expected facts are machine tokens, never prose.** ``expected_facts`` /
  ``forbidden_facts`` carry stable fact codes, so no natural-language answer string
  can become an oracle (06 §5.2, §14).
* **Gate results carry no clock and no randomness** — a result is a pure function of
  the scenario and the measurements (06 §4.1, E1 §44). Every id that appears is an
  explicit input, never a generated one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from typing import Any, Self

# ---------------------------------------------------------------------------
# Pack identity
# ---------------------------------------------------------------------------

#: The Stage E cross-plane evaluation pack. Stable; changing it is a new pack.
XP_PACK_ID = "XP-01"

#: Semantic version of the pack *contract* (scenario set + gate model).
XP_PACK_VERSION = "1"

#: Schema of a persisted per-scenario scenario-result payload.
SCENARIO_RESULT_SCHEMA_VERSION = "cross-plane-scenario-result/v1"

#: Schema of the persisted ``gate-results.json`` artifact.
GATE_RESULTS_SCHEMA_VERSION = "cross-plane-gate-results/v1"

#: Bounds. Every one of these is a REJECTION threshold, never a truncation budget:
#: a value that does not fit is a contract error, not something to silently clip.
MAX_ID_LEN = 120
MAX_TITLE_LEN = 200
MAX_ITEMS = 64
MAX_REASON_CODES = 32
MAX_REFERENCE_IDS = 64


class CrossPlaneContractError(ValueError):
    """Base error for an XP-01 contract violation."""


class BoundsViolation(CrossPlaneContractError):
    """A value exceeded its declared bound instead of being truncated."""


class ScenarioSchemaError(CrossPlaneContractError):
    """A persisted payload carries an unsupported/unknown schema version."""


class UnknownGateError(CrossPlaneContractError):
    """A scenario required a gate id that is not in the frozen gate catalog."""


class UnknownFactError(CrossPlaneContractError):
    """A scenario declared a fact code outside the frozen fact vocabulary."""


# ---------------------------------------------------------------------------
# Bounded coercion (bounded, not truncating)
# ---------------------------------------------------------------------------


def bounded_id(value: object, *, what: str) -> str:
    """Return a bounded opaque identifier, or raise.

    Deliberately NOT a truncating coercion: an over-long id means the caller passed
    something that is not an id, and silently clipping it would corrupt identity.
    """
    text = value if isinstance(value, str) else str(value)
    text = text.strip()
    if not text:
        raise BoundsViolation(f"{what} must be a non-empty identifier")
    if len(text) > MAX_ID_LEN:
        raise BoundsViolation(f"{what} exceeds {MAX_ID_LEN} characters")
    return text


def bounded_ids(values: Iterable[object], *, what: str) -> tuple[str, ...]:
    """Return a bounded, order-preserving, de-duplicated sequence of ids."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = bounded_id(value, what=what)
        if text not in seen:
            seen.add(text)
            out.append(text)
    if len(out) > MAX_REFERENCE_IDS:
        raise BoundsViolation(f"{what} exceeds {MAX_REFERENCE_IDS} entries")
    return tuple(out)


def enum_member[T: StrEnum](enum_type: type[T], raw: object, *, what: str) -> T:
    """Coerce ``raw`` to a member of ``enum_type``, or raise a contract error.

    A single typed path for "this field must be one of the frozen values": the
    reader never accepts an unknown member and never silently defaults one.
    """
    try:
        return enum_type(str(raw))
    except ValueError as exc:
        raise CrossPlaneContractError(f"unknown {what} {raw!r}") from exc


def canonical_json(payload: object) -> str:
    """Canonical JSON for hashing/identity: sorted keys, no incidental whitespace.

    Reuses the repository's existing canonicalization discipline (``sort_keys``,
    compact separators, ``ensure_ascii=False``) rather than inventing a second
    standard, so an XP-01 identity is reproducible across processes.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def identity_hash(payload: object) -> str:
    """Deterministic SHA-256 identity of ``payload`` (no clock, no randomness)."""
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frozen vocabularies
# ---------------------------------------------------------------------------


class GateFamily(StrEnum):
    """Audited Stage E gate families (06 §5.2). No other family may be added."""

    KNOWLEDGE = "KNOWLEDGE"
    CAPABILITY = "CAPABILITY"
    MCP = "MCP"
    TENANT = "TENANT"
    SECURITY = "SECURITY"
    AUTHORITY = "AUTHORITY"
    RELIABILITY = "RELIABILITY"
    OBSERVABILITY = "OBSERVABILITY"
    WORKSPACE = "WORKSPACE"


class ExecutionProfile(StrEnum):
    """Stage E execution profiles (06 §9, E1 §17).

    ``DETERMINISTIC`` is the required profile for security/authority hard gates;
    ``RUNTIME_INTEGRATED`` is reserved for facts that genuinely need real processes;
    ``LIVE_MODEL`` is informational and never the sole basis of a hard gate.
    """

    DETERMINISTIC = "deterministic"
    RUNTIME_INTEGRATED = "runtime-integrated"
    LIVE_MODEL = "live-model"


#: Profile strength ordering — a scenario may be executed at or above its minimum.
_PROFILE_RANK: Mapping[ExecutionProfile, int] = {
    ExecutionProfile.DETERMINISTIC: 0,
    ExecutionProfile.RUNTIME_INTEGRATED: 1,
    ExecutionProfile.LIVE_MODEL: 2,
}


def profile_satisfies(executed: ExecutionProfile, minimum: ExecutionProfile) -> bool:
    """True when ``executed`` is at least as strong as ``minimum``."""
    return _PROFILE_RANK[executed] >= _PROFILE_RANK[minimum]


class Plane(StrEnum):
    """The planes named by the architecture freeze (00 §0). Not a new taxonomy."""

    KNOWLEDGE = "KNOWLEDGE"
    CAPABILITY = "CAPABILITY"
    OBSERVABILITY = "OBSERVABILITY"
    ANALYST_EXPERIENCE = "ANALYST_EXPERIENCE"
    DOMAIN = "DOMAIN"
    AGENT_RUNTIME = "AGENT_RUNTIME"
    PERSISTENCE = "PERSISTENCE"
    DURABLE_EXECUTION = "DURABLE_EXECUTION"
    HUMAN_AUTHORITY = "HUMAN_AUTHORITY"
    HISIEM_INTEGRATION = "HISIEM_INTEGRATION"
    EVALUATION = "EVALUATION"


class GateStatus(StrEnum):
    """A hard gate is Boolean. There is no third state and no weighted value."""

    PASS = "PASS"
    FAIL = "FAIL"


class MeasurementSource(StrEnum):
    """Where a gate's facts came from (E1 §26).

    Auditability metadata only. The category is NOT authority: it records the
    provenance of a measurement, it never decides a gate by itself.
    """

    PERSISTED_DOMAIN_FACT = "PERSISTED_DOMAIN_FACT"
    TOOL_INVOCATION_FACT = "TOOL_INVOCATION_FACT"
    EVIDENCE_GRAPH_FACT = "EVIDENCE_GRAPH_FACT"
    CAPABILITY_ADMISSION_FACT = "CAPABILITY_ADMISSION_FACT"
    TENANT_SCOPE_FACT = "TENANT_SCOPE_FACT"
    RESPONSE_LIFECYCLE_FACT = "RESPONSE_LIFECYCLE_FACT"
    TELEMETRY_FACT = "TELEMETRY_FACT"
    WORKSPACE_PROJECTION_FACT = "WORKSPACE_PROJECTION_FACT"
    RUNTIME_PROCESS_FACT = "RUNTIME_PROCESS_FACT"


# ---------------------------------------------------------------------------
# Measurement references (bounded durable/evaluation identities)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasurementReferences:
    """Bounded correlation identities a gate result may cite.

    ``reference != truth``: these are pointers for audit and drill-down. A gate
    artifact never embeds the referenced object, and ``trace_id``/``span_id`` are
    deliberately absent as *metric* labels (00 §5.5) — they may appear here, in an
    evaluation artifact, never in a metric dimension.
    """

    investigation_id: str | None = None
    evidence_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    tool_invocation_ids: tuple[str, ...] = ()
    response_proposal_id: str | None = None
    approval_request_id: str | None = None
    execution_command_id: str | None = None
    provider_execution_ref: str | None = None
    trace_id: str | None = None
    span_id: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "investigation_id": self.investigation_id,
            "evidence_ids": list(self.evidence_ids),
            "finding_ids": list(self.finding_ids),
            "tool_invocation_ids": list(self.tool_invocation_ids),
            "response_proposal_id": self.response_proposal_id,
            "approval_request_id": self.approval_request_id,
            "execution_command_id": self.execution_command_id,
            "provider_execution_ref": self.provider_execution_ref,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        def _opt(name: str) -> str | None:
            raw = payload.get(name)
            return None if raw is None else bounded_id(raw, what=name)

        def _many(name: str) -> tuple[str, ...]:
            raw = payload.get(name) or ()
            if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
                raise BoundsViolation(f"{name} must be a sequence of identifiers")
            return bounded_ids(raw, what=name)

        return cls(
            investigation_id=_opt("investigation_id"),
            evidence_ids=_many("evidence_ids"),
            finding_ids=_many("finding_ids"),
            tool_invocation_ids=_many("tool_invocation_ids"),
            response_proposal_id=_opt("response_proposal_id"),
            approval_request_id=_opt("approval_request_id"),
            execution_command_id=_opt("execution_command_id"),
            provider_execution_ref=_opt("provider_execution_ref"),
            trace_id=_opt("trace_id"),
            span_id=_opt("span_id"),
        )


# ---------------------------------------------------------------------------
# Scenario contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossPlaneScenarioSpec:
    """Immutable, evaluation-only description of one XP-01 scenario.

    This is NOT a Domain entity and is never persisted into a production table. It
    describes *facts and invariants*, never the sentence an LLM is expected to
    produce: ``expected_facts`` / ``forbidden_facts`` hold stable fact codes from
    the frozen vocabulary in ``gates.py``.
    """

    scenario_id: str
    title: str
    gate_family: GateFamily
    minimum_profile: ExecutionProfile
    required_planes: tuple[Plane, ...]
    required_gate_ids: tuple[str, ...]
    expected_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    fixture_refs: tuple[str, ...] = ()
    scenario_version: str = "1"
    pack_id: str = XP_PACK_ID
    pack_version: str = XP_PACK_VERSION
    blocking: bool = True

    def __post_init__(self) -> None:
        bounded_id(self.scenario_id, what="scenario_id")
        if not self.title.strip():
            raise CrossPlaneContractError("title must be non-empty")
        if len(self.title) > MAX_TITLE_LEN:
            raise BoundsViolation(f"title exceeds {MAX_TITLE_LEN} characters")
        bounded_id(self.scenario_version, what="scenario_version")
        if self.pack_id != XP_PACK_ID:
            raise CrossPlaneContractError(
                f"pack_id must be {XP_PACK_ID!r}, got {self.pack_id!r}"
            )
        bounded_id(self.pack_version, what="pack_version")
        if len(self.required_gate_ids) > MAX_ITEMS:
            raise BoundsViolation(f"required_gate_ids exceeds {MAX_ITEMS} entries")
        if len(set(self.required_gate_ids)) != len(self.required_gate_ids):
            raise CrossPlaneContractError(
                f"{self.scenario_id}: required_gate_ids contains duplicates"
            )
        if self.blocking and not self.required_gate_ids:
            raise CrossPlaneContractError(
                f"{self.scenario_id}: a blocking scenario must require at least one "
                "hard gate"
            )
        for name, values in (
            ("expected_facts", self.expected_facts),
            ("forbidden_facts", self.forbidden_facts),
            ("fixture_refs", self.fixture_refs),
        ):
            if len(values) > MAX_ITEMS:
                raise BoundsViolation(f"{name} exceeds {MAX_ITEMS} entries")
            if len(set(values)) != len(values):
                raise CrossPlaneContractError(f"{self.scenario_id}: {name} has duplicates")

    def identity_payload(self) -> dict[str, Any]:
        """The semantic content that defines this scenario's identity."""
        return {
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "gate_family": self.gate_family.value,
            "minimum_profile": self.minimum_profile.value,
            "required_planes": sorted(plane.value for plane in self.required_planes),
            "required_gate_ids": sorted(self.required_gate_ids),
            "expected_facts": sorted(self.expected_facts),
            "forbidden_facts": sorted(self.forbidden_facts),
            "fixture_refs": sorted(self.fixture_refs),
            "blocking": self.blocking,
        }

    def identity(self) -> str:
        """Deterministic scenario identity — independent of timestamps and order."""
        return identity_hash(self.identity_payload())

    def content_identity(self) -> str:
        """Identity of the scenario's CONTENT, excluding ``scenario_id``.

        Two catalog entries with different ids but identical content are the same
        contract shipped twice. ``identity()`` cannot detect that because the id is
        part of it, so the catalog validator compares this instead.
        """
        payload = self.identity_payload()
        payload.pop("scenario_id")
        return identity_hash(payload)

    def to_payload(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload["title"] = self.title
        payload["identity"] = self.identity()
        return payload


# ---------------------------------------------------------------------------
# Gate result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CrossPlaneGateResult:
    """One hard gate's Boolean outcome plus bounded machine reason codes.

    No score, no weight, no confidence. ``reason_codes`` are stable tokens from the
    frozen vocabulary; prose never decides a gate.
    """

    gate_id: str
    status: GateStatus
    reason_codes: tuple[str, ...] = ()
    measurement_source: MeasurementSource = MeasurementSource.PERSISTED_DOMAIN_FACT
    references: MeasurementReferences = field(default_factory=MeasurementReferences)

    def __post_init__(self) -> None:
        bounded_id(self.gate_id, what="gate_id")
        if len(self.reason_codes) > MAX_REASON_CODES:
            raise BoundsViolation(f"reason_codes exceeds {MAX_REASON_CODES} entries")
        if self.status is GateStatus.PASS and self.reason_codes:
            raise CrossPlaneContractError(
                f"{self.gate_id}: a PASS gate must not carry failure reason codes"
            )
        if self.status is GateStatus.FAIL and not self.reason_codes:
            raise CrossPlaneContractError(
                f"{self.gate_id}: a FAIL gate must carry at least one reason code"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status.value,
            "reason_codes": list(self.reason_codes),
            "measurement_source": self.measurement_source.value,
            "references": self.references.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        status = payload.get("status")
        source = payload.get("measurement_source")
        gate_status = enum_member(GateStatus, status, what="gate status")
        measurement_source = enum_member(
            MeasurementSource, source, what="measurement source"
        )
        raw_codes = payload.get("reason_codes") or ()
        if isinstance(raw_codes, (str, bytes)) or not isinstance(raw_codes, Sequence):
            raise BoundsViolation("reason_codes must be a sequence")
        raw_refs = payload.get("references") or {}
        if not isinstance(raw_refs, Mapping):
            raise BoundsViolation("references must be an object")
        return cls(
            gate_id=bounded_id(payload.get("gate_id"), what="gate_id"),
            status=gate_status,
            reason_codes=tuple(str(code) for code in raw_codes),
            measurement_source=measurement_source,
            references=MeasurementReferences.from_payload(raw_refs),
        )


@dataclass(frozen=True)
class CrossPlaneScenarioResult:
    """The result of evaluating one scenario's required hard gates.

    ``overall_gate`` is FAIL if ANY required gate is FAIL. There is deliberately no
    field that can compensate one gate with another: the only inputs to the verdict
    are the per-gate Boolean outcomes.
    """

    scenario_id: str
    scenario_version: str
    gate_family: GateFamily
    execution_profile: ExecutionProfile
    gate_results: tuple[CrossPlaneGateResult, ...]
    overall_gate: GateStatus
    gate_failures: tuple[str, ...] = ()
    schema_version: str = SCENARIO_RESULT_SCHEMA_VERSION
    pack_id: str = XP_PACK_ID
    pack_version: str = XP_PACK_VERSION

    def __post_init__(self) -> None:
        bounded_id(self.scenario_id, what="scenario_id")
        bounded_id(self.scenario_version, what="scenario_version")
        if self.schema_version != SCENARIO_RESULT_SCHEMA_VERSION:
            raise ScenarioSchemaError(
                f"unsupported scenario-result schema {self.schema_version!r}; this "
                f"reader only accepts {SCENARIO_RESULT_SCHEMA_VERSION}"
            )
        seen: set[str] = set()
        for result in self.gate_results:
            if result.gate_id in seen:
                raise CrossPlaneContractError(
                    f"{self.scenario_id}: duplicate gate result {result.gate_id!r}"
                )
            seen.add(result.gate_id)
        failed = tuple(
            result.gate_id
            for result in self.gate_results
            if result.status is GateStatus.FAIL
        )
        if failed != self.gate_failures:
            raise CrossPlaneContractError(
                f"{self.scenario_id}: gate_failures {self.gate_failures!r} do not match "
                f"the FAIL gate results {failed!r}"
            )
        expected_overall = GateStatus.FAIL if failed else GateStatus.PASS
        if self.overall_gate is not expected_overall:
            raise CrossPlaneContractError(
                f"{self.scenario_id}: overall_gate {self.overall_gate.value} is not the "
                f"non-compensating verdict of its gates ({expected_overall.value})"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "pack_version": self.pack_version,
            "scenario_id": self.scenario_id,
            "scenario_version": self.scenario_version,
            "gate_family": self.gate_family.value,
            "execution_profile": self.execution_profile.value,
            "gate_results": [result.to_payload() for result in self.gate_results],
            "overall_gate": self.overall_gate.value,
            "gate_failures": list(self.gate_failures),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Self:
        schema = payload.get("schema_version")
        if schema != SCENARIO_RESULT_SCHEMA_VERSION:
            raise ScenarioSchemaError(
                f"unsupported scenario-result schema {schema!r}; this reader only "
                f"accepts {SCENARIO_RESULT_SCHEMA_VERSION}"
            )
        raw_results = payload.get("gate_results") or ()
        if isinstance(raw_results, (str, bytes)) or not isinstance(raw_results, Sequence):
            raise BoundsViolation("gate_results must be a sequence")
        family = enum_member(
            GateFamily, payload.get("gate_family"), what="gate family"
        )
        profile = enum_member(
            ExecutionProfile,
            payload.get("execution_profile"),
            what="execution profile",
        )
        overall = enum_member(
            GateStatus, payload.get("overall_gate"), what="overall gate"
        )
        raw_failures = payload.get("gate_failures") or ()
        if isinstance(raw_failures, (str, bytes)) or not isinstance(raw_failures, Sequence):
            raise BoundsViolation("gate_failures must be a sequence")
        return cls(
            scenario_id=bounded_id(payload.get("scenario_id"), what="scenario_id"),
            scenario_version=bounded_id(
                payload.get("scenario_version"), what="scenario_version"
            ),
            gate_family=family,
            execution_profile=profile,
            gate_results=tuple(
                CrossPlaneGateResult.from_payload(item) for item in raw_results
            ),
            overall_gate=overall,
            gate_failures=tuple(str(item) for item in raw_failures),
            schema_version=SCENARIO_RESULT_SCHEMA_VERSION,
            pack_id=bounded_id(payload.get("pack_id"), what="pack_id"),
            pack_version=bounded_id(payload.get("pack_version"), what="pack_version"),
        )


def non_compensating_verdict(
    gate_results: Sequence[CrossPlaneGateResult],
) -> tuple[GateStatus, tuple[str, ...]]:
    """The ONLY way an XP-01 scenario verdict may be computed (06 §6.4, E1 §18).

    Every required gate PASS -> PASS. Any required gate FAIL -> FAIL. There is no
    parameter for a score, a weight, or an average, so a compensation bug cannot be
    expressed here.
    """
    failures = tuple(
        result.gate_id for result in gate_results if result.status is GateStatus.FAIL
    )
    return (GateStatus.FAIL if failures else GateStatus.PASS), failures
