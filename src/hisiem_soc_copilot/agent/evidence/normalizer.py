"""Evidence Normalizer — ToolResult data → Evidence observation candidates.

Chain (investigation-tool-contract.md §24): Typed ToolResult → Evidence Normalizer
→ Deduplication → RecordEvidenceBatch → Immutable Evidence.

The normalizer selects bounded observations from a tool result, attaches
provenance/raw-reference/collection metadata and computes the dedup/content
hashes. It NEVER accepts model-supplied provenance, source ids, or authority.
The graph calls RecordEvidenceBatch (application command) to persist.

Event provenance uses the log-search raw reference (investigation-tool-contract.md
§17): {index, document_id, query_fingerprint} — never a fabricated
ExternalResourceRef (no stable event address API in HISIEM).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from ...application.commands.investigation import EvidenceObservation
from ...contracts.tools.types import ToolResult


class EvidenceNormalizer:
    """Normalizes typed ToolResults into EvidenceObservation candidates."""

    def __init__(self) -> None:
        self._now: datetime | None = None

    def normalize_alert_related(self, tool_result: ToolResult) -> list[EvidenceObservation]:
        return []

    def normalize_search_events(
        self, tool_result: ToolResult, *, tool_call_id: str
    ) -> list[EvidenceObservation]:
        """Turn one hisiem.search_events result into per-event observations."""
        observations: list[EvidenceObservation] = []
        data = tool_result.data
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return observations
        for item in items:
            document_id = item.get("document_id") or item.get("_id")
            index = item.get("index") or item.get("_index")
            if not document_id:
                continue
            observation = {
                k: v
                for k, v in item.items()
                if v is not None and k not in {"document_id", "index", "_id", "_index"}
            }
            observations.append(
                EvidenceObservation(
                    source_type="HISIEM_LOG_SEARCH",
                    source_provider="hisiem",
                    source_operation="search_events",
                    observation=observation,
                    source_tool_invocation_id=_uuid(tool_call_id),
                    observed_at=_parse_ts(observation.get("timestamp")),
                    raw_reference={
                        "index": index,
                        "document_id": document_id,
                        "query_fingerprint": _fingerprint(data),
                    },
                )
            )
        return observations

    def normalize_detection_rule(
        self, tool_result: ToolResult, *, tool_call_id: str
    ) -> list[EvidenceObservation]:
        data = tool_result.data if isinstance(tool_result.data, dict) else {}
        rule_id = data.get("rule_id")
        if not rule_id:
            return []
        observation = {
            "rule_id": rule_id,
            "name": data.get("name"),
            "category": data.get("category"),
            "rule_type": data.get("rule_type"),
            "severity": data.get("severity"),
            "enabled": data.get("enabled"),
            "status": data.get("status"),
            "tags": data.get("tags") or [],
            "description": data.get("description"),
            "logic_summary": data.get("logic_summary"),
        }
        return [
            EvidenceObservation(
                source_type="SYSTEM",
                source_provider="hisiem",
                source_operation="get_detection_rule",
                observation={k: v for k, v in observation.items() if v is not None},
                source_tool_invocation_id=_uuid(tool_call_id),
                raw_reference={"rule_id": str(rule_id)},
            )
        ]


    def normalize_knowledge_guidance(
        self, tool_result: ToolResult, *, tool_call_id: str
    ) -> list[EvidenceObservation]:
        """Turn one knowledge.retrieve_security_guidance result into per-hit observations.

        One observation per hit: the excerpt is DATA (never instruction), and the
        citation handle plus document/version/chunk ids ride in ``raw_reference``
        so a Finding can cite the evidence and the resolver can re-validate the
        handle later (spec sections 54/57).
        """
        observations: list[EvidenceObservation] = []
        data = tool_result.data if isinstance(tool_result.data, dict) else None
        if data is None:
            return observations
        hits = data.get("hits")
        if not isinstance(hits, list):
            return observations
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            citation_id = hit.get("citation_id")
            document_id = hit.get("document_id")
            content_hash = hit.get("verified_content_hash")
            provenance = hit.get("retrieval_provenance")
            if (
                not citation_id
                or not document_id
                or not isinstance(content_hash, str)
                or len(content_hash) != 64
                or any(char not in "0123456789abcdef" for char in content_hash)
                or not isinstance(provenance, dict)
            ):
                continue
            observations.append(
                EvidenceObservation(
                    source_type="KNOWLEDGE",
                    source_provider="knowledge",
                    source_operation="retrieve_security_guidance",
                    observation={
                        k: v
                        for k, v in hit.items()
                        if v is not None
                        and k
                        in {
                            "title",
                            "excerpt",
                            "source_kind",
                            "source_version",
                        }
                    },
                    source_tool_invocation_id=_uuid(tool_call_id),
                    raw_reference={
                        "citation_identity": {
                            "citation_id": str(citation_id),
                            "document_id": str(document_id),
                            "document_version_id": str(
                                hit.get("document_version_id") or ""
                            ),
                            "chunk_id": str(hit.get("chunk_id") or ""),
                            "content_hash": content_hash,
                        },
                        "retrieval_provenance": {
                            "mode": str(provenance.get("mode") or "UNKNOWN"),
                            "profile_id": provenance.get("profile_id"),
                            "retrieved_at": provenance.get("retrieved_at"),
                        },
                    },
                )
            )
        return observations

    def normalize_attack_technique(
        self, tool_result: ToolResult, *, tool_call_id: str
    ) -> list[EvidenceObservation]:
        """Turn one knowledge.resolve_attack_technique result into one observation.

        The canonical record (name, description, tactics, platforms) is DATA;
        the authoritative release travels in the observation so the workbench
        can display it without re-reading authority (spec section 67).
        """
        data = tool_result.data if isinstance(tool_result.data, dict) else None
        if data is None:
            return []
        technique_id = data.get("technique_id")
        if not technique_id:
            return []
        return [
            EvidenceObservation(
                source_type="KNOWLEDGE",
                source_provider="knowledge",
                source_operation="resolve_attack_technique",
                observation={
                    k: v
                    for k, v in data.items()
                    if v is not None
                    and k
                    in {
                        "technique_id",
                        "framework",
                        "name",
                        "description",
                        "tactics",
                        "platforms",
                        "authoritative_release",
                        "content_hash",
                    }
                },
                source_tool_invocation_id=_uuid(tool_call_id),
                raw_reference={
                    "technique_id": str(technique_id),
                    "framework": str(data.get("framework") or ""),
                    "authoritative_release": str(
                        data.get("authoritative_release") or ""
                    ),
                },
            )
        ]


def _uuid(value: str) -> Any:
    from uuid import UUID

    try:
        return UUID(value)
    except ValueError:
        return None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except ValueError:
        return None


def _fingerprint(data: dict[str, Any]) -> str:
    """A stable query fingerprint over the search bounds/params (not the events)."""
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
