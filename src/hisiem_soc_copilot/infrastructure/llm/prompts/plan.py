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

Produce a structured plan for this investigation:
- goal: a single concise statement of the investigation objective.
- steps: a short ordered list of bounded investigative steps. Each step is one
  concrete read or reasoning action. Steps may reference ONLY the selectable tools.
  You may propose no write, side-effect, shell, SQL, HTTP, or SOAR action, and no
  new tool definition."""
    return [system_message(), user_message(task, json_only=json_only)]


def strict_schema() -> dict[str, object]:
    """The strict-json_schema for the plan candidate (provider wire shape)."""
    return PLAN_JSON_SCHEMA
