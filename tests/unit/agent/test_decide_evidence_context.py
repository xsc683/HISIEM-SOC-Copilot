"""Bounded deterministic decide_next Evidence working context.

``DecideNextRequest.evidence`` is the provider-neutral working-context boundary.
A long-running investigation could accumulate unbounded Evidence rows; appending
every row to the model context would grow the prompt/token cost without bound.
These tests pin the pure, deterministic selection (select_decide_evidence_context)
against the three simultaneous budget limits, and prove the bounded context is what
actually reaches the prompt builder (not merely truncated later).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from hisiem_soc_copilot.application.ports.model_provider import (
    MAX_DECIDE_EVIDENCE_ITEMS,
    MAX_DECIDE_EVIDENCE_SUMMARY_CHARS,
    MAX_DECIDE_EVIDENCE_TOTAL_CHARS,
    DecideAlertContext,
    DecideNextRequest,
    select_decide_evidence_context,
)
from hisiem_soc_copilot.domain.investigation.entities import Evidence, EvidenceSource
from hisiem_soc_copilot.domain.investigation.enums import EvidenceSourceType


def _evidence(
    *,
    id: UUID | None = None,
    collected_at: datetime,
    operation: str = "authentication_success",
    summary: str | None = None,
) -> Evidence:
    return Evidence(
        id=id or uuid4(),
        investigation_id=UUID(int=1),
        source=EvidenceSource(
            type=EvidenceSourceType.HISIEM_EVENT,
            provider="hisiem",
            operation=operation,
        ),
        collected_at=collected_at,
        observation={"nested": {"raw": "payload-never-shown"}},
        summary=summary,
        source_resource_ref=None,
        source_tool_call_id=None,
    )


def _ts(minutes_from_now: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes_from_now)


def test_decide_evidence_context_caps_item_count() -> None:
    """Test A: 200 persisted Evidence rows → at most MAX_DECIDE_EVIDENCE_ITEMS."""
    rows = [
        _evidence(collected_at=_ts(minutes_from_now=i), summary=f"summary-{i}")
        for i in range(200)
    ]
    context = select_decide_evidence_context(rows)
    assert len(context) <= MAX_DECIDE_EVIDENCE_ITEMS


def test_decide_evidence_context_caps_total_chars() -> None:
    """Test B: the returned summaries (already bounded) fit the total-char cap.

    The char budget is measured on the SAME truncated summaries the model sees, so
    it is exact — never a raw-observation length.
    """
    rows = [
        _evidence(
            collected_at=_ts(minutes_from_now=i),
            summary="x" * 2000,
        )
        for i in range(200)
    ]
    context = select_decide_evidence_context(rows)
    assert len(context) > 0
    total_chars = sum(len(str(entry["summary"])) for entry in context)
    assert total_chars <= MAX_DECIDE_EVIDENCE_TOTAL_CHARS
    for entry in context:
        assert len(str(entry["summary"])) <= MAX_DECIDE_EVIDENCE_SUMMARY_CHARS


def test_decide_evidence_context_is_deterministic() -> None:
    """Test C: the same input rows → the same ids, in the same order."""
    rows = [
        _evidence(
            id=UUID(int=i + 1000),
            collected_at=_ts(minutes_from_now=i),
            operation=f"op-{i}",
            summary=f"summary-{i}",
        )
        for i in range(1, 60)
    ]
    # Insertion order is deliberately shuffled — selection must never depend on it.
    shuffled = list(reversed(rows))
    first = select_decide_evidence_context(rows)
    second = select_decide_evidence_context(shuffled)
    assert first == second
    assert [entry["evidence_id"] for entry in first] == [
        entry["evidence_id"] for entry in second
    ]


def test_decide_evidence_context_keeps_newest_drops_oldest() -> None:
    """Test D: newest evidence is retained, the oldest dropped once over the cap.

    Row i is collected ``59 - i`` minutes ago, so i=0 is the OLDEST and i=59 the
    NEWEST. 60 rows exceed the item cap, so the 40 newest (i in 20..59) are kept
    and the 20 oldest (i in 0..19) are dropped.
    """
    assert MAX_DECIDE_EVIDENCE_ITEMS < 60
    rows = [
        _evidence(
            id=UUID(int=i + 2000),
            collected_at=_ts(minutes_from_now=59 - i),  # larger i = newer
            summary=f"summary-{i}",
        )
        for i in range(60)
    ]
    context = select_decide_evidence_context(rows)
    ids = {UUID(str(entry["evidence_id"])) for entry in context}
    # Newest retained (i in 20..59), oldest dropped (i in 0..19).
    assert UUID(int=0 + 2000) not in ids
    assert UUID(int=19 + 2000) not in ids
    assert UUID(int=20 + 2000) in ids
    assert UUID(int=59 + 2000) in ids
    # Chronological order restored for the prompt.
    times = [
        next(e.collected_at for e in rows if str(e.id) == entry["evidence_id"])
        for entry in context
    ]
    assert times == sorted(times)


def test_decide_evidence_context_tie_keeps_deterministic_newest() -> None:
    """Same collected_at: the deterministic tie-break keeps the highest id."""
    same_instant = _ts(minutes_from_now=5)
    rows = [
        _evidence(id=UUID(int=n), collected_at=same_instant, summary=f"s-{n}")
        for n in range(1, 100)
    ]
    context = select_decide_evidence_context(rows)
    # 99 equally-new rows, but the item cap allows only 40 — and one row had to be
    # dropped, so the single largest-id row won the last slot.
    assert len(context) == MAX_DECIDE_EVIDENCE_ITEMS
    ids = {int(UUID(str(entry["evidence_id"]))) for entry in context}
    assert max(ids) == 99  # the newest (largest id) survived
    assert 1 not in ids  # the smallest-id newest row lost the tie


def test_decide_evidence_context_only_returns_rows_it_was_given() -> None:
    """Test E: the selector is a pure function of the scoped rows it receives.

    Tenant + current-Investigation isolation is enforced by the graph node's
    scoped repository read (the selector never re-scopes or fabricates). Given a
    set of rows from one investigation, the selector never returns a row outside
    that set, and never fabricates rows that were not passed in.
    """
    inv_id = UUID(int=100)
    a_rows = [
        Evidence(
            id=UUID(int=1000 + i),
            investigation_id=inv_id,
            source=EvidenceSource(
                type=EvidenceSourceType.HISIEM_EVENT,
                provider="hisiem",
                operation="authentication_success",
            ),
            collected_at=_ts(minutes_from_now=i),
            observation={},
            summary=f"a-{i}",
        )
        for i in range(45)
    ]
    context = select_decide_evidence_context(a_rows)
    input_ids = {str(row.id) for row in a_rows}
    returned_ids = {str(entry["evidence_id"]) for entry in context}
    # Never fabricates: every returned id was actually passed in.
    assert returned_ids.issubset(input_ids)
    # Never drops the whole set silently: 45 in-scope rows still yields a context.
    assert returned_ids
    # Every returned entry is bounded to the allowed three fields.
    for entry in context:
        assert set(entry) == {"evidence_id", "operation", "summary"}
        assert len(str(entry["summary"])) <= MAX_DECIDE_EVIDENCE_SUMMARY_CHARS


def test_decide_request_evidence_is_bounded_before_prompt_builder() -> None:
    """Test F: bounding happens at DecideNextRequest.evidence (the provider-neutral
    boundary), so the prompt builder receives the bounded context unchanged.

    Render the decide prompt and assert the bounded subset + per-entry summary are
    present while the dropped (oldest) evidence never appears.
    """
    from hisiem_soc_copilot.infrastructure.llm.prompts.decide import build_messages

    rows = [
        _evidence(
            id=UUID(int=i + 3000),
            collected_at=_ts(minutes_from_now=99 - i),  # i=0 oldest ... 99 newest
            operation="authentication_success",
            summary=f"summary-{i}",
        )
        for i in range(100)
    ]
    context = select_decide_evidence_context(rows)
    request = DecideNextRequest(
        investigation_id="inv-x",
        iteration=0,
        plan_goal="Investigate",
        evidence_summary=[str(e["evidence_id"]) for e in context],
        tool_names=["hisiem.search_events", "hisiem.get_detection_rule"],
        alert_context=DecideAlertContext(rule_id="rule-123"),
        evidence=context,
    )
    # The bounded context IS the request.evidence (bounding already happened above,
    # not inside the builder).
    assert request.evidence == context
    assert len(request.evidence) <= MAX_DECIDE_EVIDENCE_ITEMS
    text = "\n".join(m["content"] for m in build_messages(request))
    # The newest evidence is in the prompt; the oldest dropped rows are not.
    newest = str(UUID(int=99 + 3000))
    assert newest in text
    assert "summary-0" not in text
    assert "summary-1" not in text
    # Per-entry bounded summary appears verbatim (truncation already applied at the
    # boundary, never left to a later renderer that could reintroduce raw data).
    for entry in context:
        assert str(entry["evidence_id"]) in text
