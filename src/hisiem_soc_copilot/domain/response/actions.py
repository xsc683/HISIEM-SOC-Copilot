"""Executable response-action registry (V1 bounded allowlist).

The domain allowlist (:class:`ResponseActionKey`) names the response intents the
system understands. This module narrows that to the actions HISIEM can ACTUALLY
execute in V1, and validates each action's parameters so a proposal can only ever
carry a bounded, schema-checked contract.

V1 supports exactly one executable action:

    START_SOAR_PLAYBOOK — start a published SOAR playbook against the target
    alert (HISIEM's real, durable, idempotent execution capability).

The other :class:`ResponseActionKey` members (BLOCK_SOURCE_IP / DISABLE_ACCOUNT /
ISOLATE_HOST) map to NO supported HISIEM capability today, so a proposal using
them can never be executed — such a recommendation stays informational (spec §5,
§21). We deliberately do NOT invent fake integrations for them.

The registry is pure domain: no framework/HTTP imports.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..shared.errors import DomainError
from .enums import ResponseActionKey


class ResponseActionUnsupportedError(DomainError):
    """Raised when an action key is not executable against HISIEM in V1."""

    code = "RESPONSE_ACTION_UNSUPPORTED"


@dataclass(frozen=True)
class ActionSpec:
    """Bounded contract for one executable action.

    ``allowed_parameters`` is an exhaustive key set; ``required_parameters`` a
    subset. ``max_parameter_length`` bounds each string parameter so a proposal
    can never carry an unbounded payload.
    """

    key: ResponseActionKey
    required_parameters: frozenset[str]
    allowed_parameters: frozenset[str]
    max_parameter_length: int = 200


_SPECS: dict[ResponseActionKey, ActionSpec] = {
    ResponseActionKey.START_SOAR_PLAYBOOK: ActionSpec(
        key=ResponseActionKey.START_SOAR_PLAYBOOK,
        required_parameters=frozenset({"playbook_id"}),
        allowed_parameters=frozenset({"playbook_id"}),
    ),
}

#: The V1 executable allowlist — the only action keys HISIEM can run today.
EXECUTABLE_ACTION_KEYS: frozenset[str] = frozenset(k.value for k in _SPECS)


def is_executable(action_key: str) -> bool:
    """True iff ``action_key`` has a real HISIEM execution mapping in V1."""
    return action_key in EXECUTABLE_ACTION_KEYS


def validate_action_parameters(action_key: str, parameters: dict[str, object]) -> None:
    """Validate an executable action's bounded parameters, or raise.

    Raises :class:`ResponseActionUnsupportedError` for an action outside the V1
    executable allowlist; :class:`DomainError` for a missing/extra/oversized
    parameter. Deterministic and side-effect free.
    """
    spec = _SPECS.get(ResponseActionKey(action_key)) if _is_known(action_key) else None
    if spec is None:
        raise ResponseActionUnsupportedError(
            f"response action {action_key!r} is not executable in V1"
        )
    provided = set(parameters)
    missing = spec.required_parameters - provided
    if missing:
        raise DomainError(
            f"{action_key} is missing required parameters: {sorted(missing)}",
            details={"missing_parameters": sorted(missing)},
        )
    extra = provided - spec.allowed_parameters
    if extra:
        raise DomainError(
            f"{action_key} has unsupported parameters: {sorted(extra)}",
            details={"unsupported_parameters": sorted(extra)},
        )
    for name, value in parameters.items():
        if not isinstance(value, str) or not value.strip():
            raise DomainError(f"{action_key} parameter {name!r} must be a non-empty string")
        if len(value) > spec.max_parameter_length:
            raise DomainError(
                f"{action_key} parameter {name!r} exceeds the bounded length"
            )


def _is_known(action_key: str) -> bool:
    return any(action_key == k.value for k in ResponseActionKey)
