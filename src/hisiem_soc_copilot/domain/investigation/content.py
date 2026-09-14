"""Deterministic content-hash / dedup helpers for Evidence.

Evidence is immutable and deduplicated. ``content_hash`` pins the exact observed
content; ``dedup_key`` identifies "the same underlying fact" so a retried tool call
or a resumed/retried graph step never records a duplicate Evidence row.

Both are pure functions over bounded provenance/observation values — the model
never supplies them (investigation-tool-contract.md §24).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _canonical_json(value: Any) -> str:
    """Stable stringification of nested JSON-ish values.

    Uses ``sort_keys`` with default separators so semantically equal content maps
    to the same string regardless of insertion order.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_content_hash(observation: dict[str, Any]) -> str:
    """sha256 over the canonical observation JSON (64 hex chars)."""
    return hashlib.sha256(_canonical_json(observation).encode("utf-8")).hexdigest()


def compute_dedup_key(
    *,
    source_provider: str,
    source_operation: str,
    raw_reference: dict[str, Any] | None,
    resource_address: str | None = None,
) -> str:
    """sha256 identity for "the same underlying provider fact".

    A retried read of the same provider document/row (same raw reference or source
    resource address) must produce the same dedup key so RecordEvidenceBatch can
    skip a duplicate insert. Collection time is intentionally NOT part of the key.
    """
    identity_reference = raw_reference
    if (
        source_provider == "knowledge"
        and source_operation == "retrieve_security_guidance"
        and isinstance(raw_reference, dict)
    ):
        citation_identity = raw_reference.get("citation_identity")
        if isinstance(citation_identity, dict):
            identity_reference = citation_identity
        else:
            # Tolerate pre-closure reference shapes while excluding fields
            # that describe a retrieval execution rather than cited content.
            identity_reference = {
                key: value
                for key, value in raw_reference.items()
                if key not in {"retrieval_mode", "retrieval_profile_id", "retrieved_at"}
            }
    payload = {
        "provider": source_provider,
        "operation": source_operation,
        "raw_reference": identity_reference,
        "resource_address": resource_address,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
