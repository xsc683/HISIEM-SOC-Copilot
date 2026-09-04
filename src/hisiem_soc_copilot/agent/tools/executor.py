"""ToolExecutor — the deterministic executor over allowlisted read tools.

Chain (investigation-tool-contract.md §2): candidate → schema validation →
authenticated scope binding → policy/budget → provider adapter → typed ToolResult.

The executor NEVER reads tenant/actor/authorization from model arguments; those
come from the ToolExecutionContext the graph builds from trusted state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from ...application.ports.hisiem import HisiemPort
from ...contracts.tools.types import (
    LogSearchCondition,
    ToolCandidate,
    ToolResult,
    ToolResultStatus,
)
from .args import (
    DetectionRuleArgs,
    SearchEventsArgs,
    parse_detection_rule,
    parse_search_events,
)
from .policy import validate_search_span
from .registry import UnknownToolError


@dataclass
class ToolExecution:
    """Outcome of one tool call (safe metadata + typed result)."""

    tool_name: str
    tool_call_id: str
    status: ToolResultStatus
    result: ToolResult


class ToolExecutor:
    """Executes a schema-validated, policy-checked read tool via the adapter.

    ``hisiem`` must satisfy the read side of HisiemPort (get_alert / search_events
    / get_detection_rule). A single tool failure is returned as a typed result —
    it never raises out of the graph unless the failure is a policy/argument
    rejection that must stop the step.
    """

    def __init__(self, *, hisiem: HisiemPort) -> None:
        self._hisiem = hisiem

    async def execute(
        self,
        *,
        candidate: ToolCandidate,
        tenant_id: str,
        source_alert_ref: dict[str, str],
    ) -> ToolExecution:
        """Validate + execute one candidate into a typed ToolExecution.

        Raises ToolPolicyError/UnknownToolError (no budget check here — budget is
        consumed by the caller after a successful read, per the graph step).
        """
        tool_call_id = str(uuid4())
        fetched_at = datetime.now(UTC).isoformat()
        try:
            if candidate.tool_name == "hisiem.search_events":
                search_args = parse_search_events(candidate.arguments)
                validate_search_span(search_args)
                result = await self._search_events(
                    search_args, tenant_id=tenant_id, tool_call_id=tool_call_id
                )
            elif candidate.tool_name == "hisiem.get_detection_rule":
                rule_args = parse_detection_rule(candidate.arguments)
                result = await self._detection_rule(
                    rule_args, tenant_id=tenant_id, tool_call_id=tool_call_id
                )
            else:
                raise UnknownToolError(
                    f"tool '{candidate.tool_name}' is not supported by this executor"
                )
            return ToolExecution(
                tool_name=candidate.tool_name,
                tool_call_id=tool_call_id,
                status=result.status,
                result=result,
            )
        except (UnknownToolError, ValueError) as exc:
            # Argument/policy rejections are deterministic, typed, non-retryable.
            return ToolExecution(
                tool_name=candidate.tool_name,
                tool_call_id=tool_call_id,
                status="REJECTED",
                result=ToolResult(
                    tool_call_id=tool_call_id,
                    tool_name=candidate.tool_name,
                    status="REJECTED",
                    fetched_at=fetched_at,
                    error=str(exc),
                    error_code="POLICY_REJECTED",
                ),
            )

    async def _search_events(
        self, args: SearchEventsArgs, *, tenant_id: str, tool_call_id: str
    ) -> ToolResult:
        from ...application.errors import ExternalServiceError

        try:
            outcome = await self._hisiem.search_events(
                tenant_id=tenant_id,
                from_=args.from_,
                to=args.to,
                conditions=[_condition_payload(c) for c in args.conditions],
                limit=args.limit,
                sort=args.sort,
            )
        except ExternalServiceError:
            return _error_result(
                tool_call_id, "hisiem.search_events", "UPSTREAM_UNAVAILABLE", retryable=True
            )
        items = [
            {
                "document_id": hit.document_id,
                "index": hit.index,
                "timestamp": hit.timestamp,
                "event_category": hit.event_category,
                "event_action": hit.event_action,
                "event_outcome": hit.event_outcome,
                "source_ip": hit.source_ip,
                "destination_ip": hit.destination_ip,
                "user_name": hit.user_name,
                "host_name": hit.host_name,
                "log_source_id": hit.log_source_id,
                "message": hit.message,
            }
            for hit in outcome.items
        ]
        status: ToolResultStatus = "SUCCESS" if items else "NO_DATA"
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name="hisiem.search_events",
            status=status,
            fetched_at=datetime.now(UTC).isoformat(),
            data={"items": items, "total": outcome.total, "returned": outcome.returned},
            truncated=outcome.truncated,
        )

    async def _detection_rule(
        self, args: DetectionRuleArgs, *, tenant_id: str, tool_call_id: str
    ) -> ToolResult:
        from ...application.errors import ExternalServiceError

        try:
            rule = await self._hisiem.get_detection_rule(
                tenant_id=tenant_id, rule_id=args.rule_id
            )
        except ExternalServiceError:
            return _error_result(
                tool_call_id, "hisiem.get_detection_rule", "UPSTREAM_UNAVAILABLE", retryable=True
            )
        if rule is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name="hisiem.get_detection_rule",
                status="NO_DATA",
                fetched_at=datetime.now(UTC).isoformat(),
                data={},
            )
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name="hisiem.get_detection_rule",
            status="SUCCESS",
            fetched_at=datetime.now(UTC).isoformat(),
            data={
                "rule_id": rule.rule_id,
                "name": rule.name,
                "category": rule.category,
                "rule_type": rule.rule_type,
                "severity": rule.severity,
                "enabled": rule.enabled,
                "status": rule.status,
                "tags": rule.tags,
                "description": rule.description,
                "logic_summary": rule.logic_summary,
            },
        )


def _error_result(
    tool_call_id: str, tool_name: str, code: str, *, retryable: bool
) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_call_id,
        tool_name=tool_name,
        status="UNAVAILABLE",
        fetched_at=datetime.now(UTC).isoformat(),
        error_code=code,
        error="upstream unavailable",
        continuation="retryable" if retryable else None,
    )


def _condition_payload(condition: LogSearchCondition) -> dict[str, object]:
    return {
        "field": condition.field,
        "operator": condition.operator,
        "value": condition.value,
    }
