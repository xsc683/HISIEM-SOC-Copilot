"""RuntimeBudget — the single deterministic authority over the run's counters.

Every node-level budget decision (may the model be consulted? may a tool run? may
another step happen? has the wall-clock deadline passed?) and every decrement goes
through this one value object. No node hand-decrements ``budget_remaining_*``
directly.

The counters and the wall-clock deadline are checkpointed in the graph state, so a
crash/restart/resume naturally continues from the CONSUMED budget — never reset to
full (load_investigation seeds a FRESH run from the aggregate's BudgetLimits and
preserves checkpointed values on resume). The model can never raise the budget and
no node may write it upward.

``max_llm_calls`` invariant: the total number of model consults across the run
(plan + decide + assess + verdict) is bounded by the runtime's original
``max_llm_calls`` for ANY ``max_llm_calls >= 1``. decide reserves the final two
LLM-call slots for the convergence path (assess + verdict); when fewer than two
remain it stops consulting. assess and verdict themselves only consult when a slot
remains (``can_call_llm``), otherwise the graph applies the deterministic
low-budget fallback (hypotheses UNRESOLVED; verdict INCONCLUSIVE "model-call
budget exhausted") — never an over-budget model call.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

# Two LLM-call slots reserved for the convergence path (assess + verdict). decide
# stops consulting once only this many (or fewer) slots remain, so the final assess
# and verdict consults are guaranteed their slots and the total never exceeds
# max_llm_calls regardless of how many investigate iterations the model requests.
CONVERGENCE_LLM_RESERVE = 2


@dataclass(frozen=True)
class RuntimeBudget:
    """A frozen snapshot of the runtime budget + a wall-clock deadline.

    Built per node from the checkpointed graph state. ``consume_*`` returns a NEW
    budget with one unit consumed (never negative); the node merges the resulting
    counters into its state update via ``to_updates()``.
    """

    steps: int
    tool_calls: int
    llm_calls: int
    deadline_at: float | None = None

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> RuntimeBudget:
        deadline = state.get("budget_deadline_at")
        deadline_at = float(deadline) if isinstance(deadline, (int, float)) else None
        return cls(
            steps=int(state.get("budget_remaining_steps") or 0),
            tool_calls=int(state.get("budget_remaining_tool_calls") or 0),
            llm_calls=int(state.get("budget_remaining_llm_calls") or 0),
            deadline_at=deadline_at,
        )

    # ------------------------------------------------------------------
    # gates (reads)
    # ------------------------------------------------------------------
    @property
    def deadline_exceeded(self) -> bool:
        """True when the wall-clock deadline (epoch-seconds) has passed."""
        return self.deadline_at is not None and time.time() >= self.deadline_at

    def can_call_llm(self) -> bool:
        """A model consult may start only when a slot remains AND the deadline has
        not passed."""
        return self.llm_calls > 0 and not self.deadline_exceeded

    def can_consult_decide(self) -> bool:
        """decide may consult only while MORE than the convergence reserve remains.

        This is the LLM-call ceiling for the investigate loop: once only the two
        convergence slots are left, decide stops asking the model and routes to the
        bounded finalize path.
        """
        return self.llm_calls > CONVERGENCE_LLM_RESERVE and not self.deadline_exceeded

    def can_execute_tool(self) -> bool:
        """A tool may be scheduled only when a tool-call slot remains (the step is
        already gated by ``can_take_step``). The deadline is not re-checked at
        scheduling time; execute_and_ingest enforces it as the backstop before the
        tool actually runs."""
        return self.tool_calls > 0

    def can_take_step(self) -> bool:
        """A further investigate iteration may run only while a step slot remains."""
        return self.steps > 0

    # ------------------------------------------------------------------
    # consumptions (return new budgets; never negative)
    # ------------------------------------------------------------------
    def consume_llm_call(self) -> RuntimeBudget:
        return self._consume(llm_calls=max(self.llm_calls - 1, 0))

    def consume_tool_call(self) -> RuntimeBudget:
        return self._consume(tool_calls=max(self.tool_calls - 1, 0))

    def consume_step(self) -> RuntimeBudget:
        return self._consume(steps=max(self.steps - 1, 0))

    def to_updates(self) -> dict[str, Any]:
        """State-update keys for the consumed counters (merged by the node)."""
        return {
            "budget_remaining_steps": self.steps,
            "budget_remaining_tool_calls": self.tool_calls,
            "budget_remaining_llm_calls": self.llm_calls,
        }

    def _consume(self, **fields: int) -> RuntimeBudget:
        return replace(self, **fields)
