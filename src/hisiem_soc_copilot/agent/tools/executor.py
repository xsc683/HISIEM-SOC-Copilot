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
from ...application.ports.knowledge import KnowledgeQuery
from ...contracts.tools.types import (
    LogSearchCondition,
    ToolCandidate,
    ToolResult,
    ToolResultStatus,
)
from ..knowledge.catalog import (
    KnowledgeRetrievalCatalogAdapter,
    guidance_tool_result,
)
from .args import (
    DetectionRuleArgs,
    ResolveTechniqueArgs,
    RetrieveGuidanceArgs,
    SearchEventsArgs,
    parse_detection_rule,
    parse_resolve_technique,
    parse_retrieve_guidance,
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

    def __init__(
        self,
        *,
        hisiem: HisiemPort,
        knowledge: KnowledgeRetrievalCatalogAdapter | None = None,
    ) -> None:
        self._hisiem = hisiem
        self._knowledge = knowledge

    async def execute(
        self,
        *,
        candidate: ToolCandidate,
        tenant_id: str,
        source_alert_ref: dict[str, str],
        tool_call_id: str | None = None,
    ) -> ToolExecution:
        """Validate + execute one candidate into a typed ToolExecution.

        ``tool_call_id`` is the STABLE invocation identity: it becomes the audit
        row id and Evidence.source_tool_invocation_id. When the caller does not
        supply one (direct executor use) a fresh id is generated, but the graph
        always passes its deterministic id so audit ↔ evidence provenance matches.
        Raises ToolPolicyError/UnknownToolError (no budget check here — budget is
        consumed by the caller after a successful read, per the graph step).
        """
        tool_call_id = tool_call_id or str(uuid4())
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
            elif candidate.tool_name == "knowledge.retrieve_security_guidance":
                guidance_args = parse_retrieve_guidance(candidate.arguments)
                result = await self._retrieve_guidance(
                    guidance_args, tenant_id=tenant_id, tool_call_id=tool_call_id
                )
            elif candidate.tool_name == "knowledge.resolve_attack_technique":
                technique_args = parse_resolve_technique(candidate.arguments)
                result = await self._resolve_technique(
                    technique_args, tenant_id=tenant_id, tool_call_id=tool_call_id
                )
            elif candidate.tool_name.startswith("knowledge."):
                # A knowledge tool with no wired adapter is an unavailable
                # channel, not a policy rejection: the name is legitimate, the
                # wiring is absent.
                result = self._knowledge_unavailable(
                    tool_name=candidate.tool_name,
                    tool_call_id=tool_call_id,
                    reason="no knowledge catalog is wired into this executor",
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
        except Exception:
            # Retrieval, database, provider, and citation-integrity failures are
            # data-channel outages, never exceptions escaping the investigation graph.
            if not candidate.tool_name.startswith("knowledge."):
                raise
            result = _error_result(
                tool_call_id,
                candidate.tool_name,
                "KNOWLEDGE_UNAVAILABLE",
                retryable=True,
            )
            return ToolExecution(
                tool_name=candidate.tool_name,
                tool_call_id=tool_call_id,
                status=result.status,
                result=result,
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

    async def _require_knowledge(
        self, *, tool_name: str, tool_call_id: str
    ) -> KnowledgeRetrievalCatalogAdapter | None:
        """Return the catalog adapter, or None after recording unavailability.

        The executor is constructed without a knowledge adapter in unit tests
        and in any wiring that forgot it. That must read as "channel
        unavailable", never as AttributeError out of the graph.
        """
        if self._knowledge is None:
            return None
        return self._knowledge

    def _knowledge_unavailable(
        self, *, tool_name: str, tool_call_id: str, reason: str
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="UNAVAILABLE",
            fetched_at=datetime.now(UTC).isoformat(),
            data={},
            error=reason,
            error_code="KNOWLEDGE_UNAVAILABLE",
        )

    async def _retrieve_guidance(
        self, args: RetrieveGuidanceArgs, *, tenant_id: str, tool_call_id: str
    ) -> ToolResult:
        """Run one knowledge search; the mode label is honest by construction.

        HYBRID is attempted first; when no ACTIVE embedding profile or no
        provider exists the service raises ``KnowledgeRetrievalUnavailableError``
        for the vector channel, and this falls back to LEXICAL_ONLY with the
        fallback labeled in the result (spec section 53). A lexical failure is
        a genuine defect and propagates.
        """
        from ...application.errors import KnowledgeRetrievalUnavailableError

        adapter = await self._require_knowledge(
            tool_name="knowledge.retrieve_security_guidance",
            tool_call_id=tool_call_id,
        )
        if adapter is None:
            return self._knowledge_unavailable(
                tool_name="knowledge.retrieve_security_guidance",
                tool_call_id=tool_call_id,
                reason="no knowledge catalog is wired into this executor",
            )
        query = KnowledgeQuery(
            topic=args.topic,
            context_terms=args.context_terms,
            limit=args.limit,
        )
        try:
            validated = await adapter.retrieve_security_guidance_validated(
                tenant_id=tenant_id, query=query
            )
        except KnowledgeRetrievalUnavailableError:
            validated = await adapter.retrieve_lexical_guidance_validated(
                tenant_id=tenant_id, query=query
            )
        return guidance_tool_result(
            tool_call_id=tool_call_id,
            result=validated.result,
            verified_content_hashes=validated.verified_content_hashes,
        )

    async def _resolve_technique(
        self, args: ResolveTechniqueArgs, *, tenant_id: str, tool_call_id: str
    ) -> ToolResult:
        """Resolve one technique id against the authoritative release.

        Returns NO_DATA (not an error) when no release is authoritative or the
        id is unknown to it: absence of authority is a fact about the corpus,
        not a failure of the call.
        """
        adapter = await self._require_knowledge(
            tool_name="knowledge.resolve_attack_technique",
            tool_call_id=tool_call_id,
        )
        if adapter is None:
            return self._knowledge_unavailable(
                tool_name="knowledge.resolve_attack_technique",
                tool_call_id=tool_call_id,
                reason="no knowledge catalog is wired into this executor",
            )
        technique_id = args.technique_id
        framework = args.framework
        record = await adapter.resolve_attack_technique(
            tenant_id=tenant_id, technique_id=technique_id, framework=framework
        )
        if record is None:
            return ToolResult(
                tool_call_id=tool_call_id,
                tool_name="knowledge.resolve_attack_technique",
                status="NO_DATA",
                fetched_at=datetime.now(UTC).isoformat(),
                data={"technique_id": technique_id, "framework": framework},
            )
        return ToolResult(
            tool_call_id=tool_call_id,
            tool_name="knowledge.resolve_attack_technique",
            status="SUCCESS",
            fetched_at=datetime.now(UTC).isoformat(),
            data={
                "technique_id": record.technique_id,
                "framework": record.framework,
                "name": record.name,
                "description": record.description,
                "tactics": list(record.tactics),
                "platforms": list(record.platforms),
                "authoritative_release": record.source_release,
                "content_hash": record.content_hash,
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
