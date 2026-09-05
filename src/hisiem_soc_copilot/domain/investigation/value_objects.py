"""Investigation value objects.

Value objects are immutable and structural. They model the ExternalResourceRef /
ActorRef / budget boundaries without importing framework types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalResourceRef:
    """Stable reference to an external (HISIEM-owned) resource.

    ``address_id`` is the stable identifier used by the HISIEM API to address the
    resource. ``business_id`` is an optional display identifier and must not be
    used to infer the addressing id downstream.
    """

    provider: str
    resource_type: str
    address_id: str
    business_id: str | None = None

    @property
    def is_alert(self) -> bool:
        return self.provider == "hisiem" and self.resource_type == "alert"


@dataclass(frozen=True)
class ActorRef:
    """An authenticated caller/actor snapshot.

    Only the trusted authentication context may populate ``subject_id``/``tenant_id``.
    These must never be declared by request body, LLM, or tool result.
    """

    subject_id: str
    tenant_id: str
    display_name: str | None = None
    role_snapshot: str | None = None


@dataclass(frozen=True)
class BudgetLimits:
    """Autonomy budget bounds applied to an investigation run.

    These are the RUNTIME authority bounds the graph deterministically enforces.
    ``max_llm_tokens`` is reserved for a real provider's token accounting (no fake
    accounting is done today); ``max_llm_calls`` is the deterministically enforced
    LLM-call ceiling in this round.
    """

    max_steps: int = 20
    max_tool_calls: int = 30
    max_llm_calls: int = 20
    max_llm_tokens: int = 20_000
    max_duration_seconds: int = 600

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_steps": self.max_steps,
            "max_tool_calls": self.max_tool_calls,
            "max_llm_calls": self.max_llm_calls,
            "max_llm_tokens": self.max_llm_tokens,
            "max_duration_seconds": self.max_duration_seconds,
        }
