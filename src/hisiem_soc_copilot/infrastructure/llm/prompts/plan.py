"""Plan prompt: PlanRequest → Chat messages.

The plan expresses ONLY the investigation goal + bounded steps; the model never
emits Shell/SQL/HTTP/Elasticsearch DSL/write/SOAR/approval/new-tool content.
"""

from __future__ import annotations

from ....application.ports.model_provider import PlanRequest
from ..schemas import PLAN_JSON_SCHEMA
from .common import bounded, system_message, tool_names_block, user_message


def build_messages(request: PlanRequest, *, json_only: bool = False) -> list[dict[str, str]]:
    task = f"""Your task is to propose an investigation PLAN for one alert.

Investigation id: {request.investigation_id}

Alert summary:
{bounded(request.alert_summary, limit=800) or "(no alert summary supplied)"}

{tool_names_block(request.tool_names)}

Return JSON EXACTLY in this shape (no extra keys, no renames):

{{"goal": "<single concise investigation objective>",
  "steps": [{{"step_id": "<stable short id>", "objective": "<one bounded step>"}}]}}

Each step is one concrete read or reasoning action referencing ONLY the selectable
tools. You may propose no write, side-effect, shell, SQL, HTTP, or SOAR action, and
no new tool definition. Every step object MUST use the keys "step_id" and
"objective" — never "step", "action", or any other name."""
    return [system_message(), user_message(task, json_only=json_only)]


def strict_schema() -> dict[str, object]:
    """The strict-json_schema for the plan candidate (provider wire shape)."""
    return PLAN_JSON_SCHEMA
