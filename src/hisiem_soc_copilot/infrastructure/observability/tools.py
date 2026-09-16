"""Infrastructure decorator for safe semantic tool and provider telemetry."""

from __future__ import annotations

from time import perf_counter

from ...agent.knowledge.catalog import KnowledgeRetrievalCatalogAdapter
from ...agent.tools.executor import ToolExecution, ToolExecutor
from ...agent.tools.provider_router import ProviderRouter
from ...agent.tools.registry import ToolRegistry
from ...application.ports.hisiem import HisiemPort
from ...contracts.tools.types import ToolCandidate, ToolResultStatus
from .context import bind_log_context, set_span_attribute, start_span
from .metrics import record_counter, record_histogram

_TOOL_NAMES = frozenset(
    {
        "hisiem.search_events",
        "hisiem.get_detection_rule",
        "knowledge.retrieve_security_guidance",
        "knowledge.resolve_attack_technique",
    }
)
_RETRIEVAL_TOOL = "knowledge.retrieve_security_guidance"


class ObservedToolExecutor(ToolExecutor):
    """Add telemetry at the composition edge without coupling the Agent layer."""

    def __init__(
        self,
        *,
        hisiem: HisiemPort,
        knowledge: KnowledgeRetrievalCatalogAdapter | None = None,
        provider_router: ProviderRouter | None = None,
        registry: ToolRegistry | None = None,
    ) -> None:
        super().__init__(
            hisiem=hisiem,
            knowledge=knowledge,
            provider_router=provider_router,
            registry=registry,
        )
        self._observed_router = provider_router

    async def execute(
        self,
        *,
        candidate: ToolCandidate,
        tenant_id: str,
        source_alert_ref: dict[str, str],
        tool_call_id: str | None = None,
        investigation_id: str | None = None,
        budget_remaining: int | None = None,
        budget_already_reserved: bool = False,
    ) -> ToolExecution:
        started = perf_counter()
        is_mcp = self._is_mcp(candidate.tool_name)
        tool_name = candidate.tool_name if candidate.tool_name in _TOOL_NAMES else None
        provider = "mcp" if is_mcp else "native"
        metric_attributes: dict[str, str] = {"tool_provider": provider}
        span_attributes: dict[str, str] = {"tool_provider": provider}
        if tool_name is not None:
            metric_attributes["tool_name"] = tool_name
            span_attributes["tool_name"] = tool_name
        with bind_log_context(
            tool_invocation_id=tool_call_id,
            investigation_id=investigation_id,
        ), start_span("tool.execute", attributes=span_attributes) as span:
            try:
                if is_mcp:
                    execution = await self._execute_mcp(
                        candidate=candidate,
                        tenant_id=tenant_id,
                        source_alert_ref=source_alert_ref,
                        tool_call_id=tool_call_id,
                        investigation_id=investigation_id,
                        budget_remaining=budget_remaining,
                        budget_already_reserved=budget_already_reserved,
                    )
                elif candidate.tool_name == _RETRIEVAL_TOOL:
                    retrieval_started = perf_counter()
                    with start_span(
                        "knowledge.retrieve",
                        attributes={"tool_name": _RETRIEVAL_TOOL},
                    ):
                        execution = await super().execute(
                            candidate=candidate,
                            tenant_id=tenant_id,
                            source_alert_ref=source_alert_ref,
                            tool_call_id=tool_call_id,
                            investigation_id=investigation_id,
                            budget_remaining=budget_remaining,
                            budget_already_reserved=budget_already_reserved,
                        )
                    self._record_retrieval(
                        execution, perf_counter() - retrieval_started
                    )
                else:
                    execution = await super().execute(
                        candidate=candidate,
                        tenant_id=tenant_id,
                        source_alert_ref=source_alert_ref,
                        tool_call_id=tool_call_id,
                        investigation_id=investigation_id,
                        budget_remaining=budget_remaining,
                        budget_already_reserved=budget_already_reserved,
                    )
            except Exception:
                record_counter("tool.errors", attributes=metric_attributes)
                set_span_attribute(span, "result", "error")
                raise

            result = _result_category(execution.status)
            set_span_attribute(span, "result", result)
            record_counter(
                "tool.calls",
                attributes={**metric_attributes, "result": result},
            )
            if result in {"rejected", "unavailable", "error"}:
                record_counter("tool.errors", attributes=metric_attributes)
            record_histogram("tool.duration", perf_counter() - started, metric_attributes)
            return execution

    def _is_mcp(self, tool_name: str) -> bool:
        if self._observed_router is None:
            return False
        provider = self._observed_router.provider(tool_name)
        return provider is not None and provider.identity.provider_type == "mcp"

    async def _execute_mcp(
        self,
        *,
        candidate: ToolCandidate,
        tenant_id: str,
        source_alert_ref: dict[str, str],
        tool_call_id: str | None,
        investigation_id: str | None,
        budget_remaining: int | None,
        budget_already_reserved: bool,
    ) -> ToolExecution:
        router = self._observed_router
        if router is None:
            raise RuntimeError("MCP execution requires a provider router")
        admission = router.admission(candidate.tool_name)
        server_category = "external"
        if admission is not None:
            provider = router.provider(candidate.tool_name)
            if provider is not None:
                server_category = provider.identity.server_category
        attrs: dict[str, str] = {
            "tool_provider": "mcp",
            "server_category": server_category,
        }
        started = perf_counter()
        with start_span(
            "mcp.call",
            attributes={"tool_name": candidate.tool_name, **attrs},
        ):
            execution = await super().execute(
                candidate=candidate,
                tenant_id=tenant_id,
                source_alert_ref=source_alert_ref,
                tool_call_id=tool_call_id,
                investigation_id=investigation_id,
                budget_remaining=budget_remaining,
                budget_already_reserved=budget_already_reserved,
            )
        result = _result_category(execution.status)
        metric_attrs = {"tool_provider": "mcp", "server_category": server_category}
        record_counter(
            "mcp.calls",
            attributes={**metric_attrs, "result": result},
        )
        record_histogram("mcp.duration", perf_counter() - started, metric_attrs)
        if execution.result.error_code:
            record_counter(
                "mcp.errors",
                attributes={
                    **metric_attrs,
                    "error_category": execution.result.error_code,
                },
            )
        return execution

    @staticmethod
    def _record_retrieval(execution: ToolExecution, duration: float) -> None:
        result = execution.result
        data = result.data if isinstance(result.data, dict) else {}
        mode = data.get("retrieval_mode")
        mode_value = mode if isinstance(mode, str) else "UNKNOWN"
        attrs = {"retrieval_mode": mode_value}
        record_counter("retrieval.calls", attributes=attrs)
        record_histogram("retrieval.duration", duration, attrs)
        if result.status == "UNAVAILABLE":
            record_counter(
                "retrieval.errors",
                attributes={"retrieval_mode": mode_value, "error_category": "UNAVAILABLE"},
            )
        count = data.get("returned")
        if isinstance(count, int) and count >= 0:
            record_histogram("retrieval.hit_count", float(count), attrs)


def _result_category(status: ToolResultStatus) -> str:
    categories = {
        "SUCCESS": "success",
        "NO_DATA": "no_data",
        "PARTIAL": "partial",
        "REJECTED": "rejected",
        "UNAVAILABLE": "unavailable",
    }
    return categories.get(status, "error")
