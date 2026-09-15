"""Infrastructure decorator for safe semantic tool and Knowledge spans."""

from __future__ import annotations

from time import perf_counter

from ...agent.knowledge.catalog import KnowledgeRetrievalCatalogAdapter
from ...agent.tools.executor import ToolExecution, ToolExecutor
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
    ) -> None:
        super().__init__(hisiem=hisiem, knowledge=knowledge)

    async def execute(
        self,
        *,
        candidate: ToolCandidate,
        tenant_id: str,
        source_alert_ref: dict[str, str],
        tool_call_id: str | None = None,
    ) -> ToolExecution:
        started = perf_counter()
        tool_name = candidate.tool_name if candidate.tool_name in _TOOL_NAMES else None
        metric_attributes: dict[str, str] = {"tool_provider": "native"}
        span_attributes: dict[str, str] = {"tool_provider": "native"}
        if tool_name is not None:
            metric_attributes["tool_name"] = tool_name
            span_attributes["tool_name"] = tool_name
        with bind_log_context(tool_invocation_id=tool_call_id), start_span(
            "tool.execute", attributes=span_attributes
        ) as span:
                try:
                    if candidate.tool_name == _RETRIEVAL_TOOL:
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
