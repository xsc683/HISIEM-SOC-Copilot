"""The in-memory fakes must not drift from the real durable routing.

``FakeEventLedger`` enqueues outbox deliveries using its own
``FAKE_EVENT_DESTINATIONS`` map. If that map ever diverged from the production
``_EVENT_DESTINATIONS``, the unit suite would keep passing while the real
dispatcher routed an event to the wrong (or no) destination — the unit tests would
be proving a fiction. This is the guard for that.

It also pins the two-destination response split (spec §3): submission and
reconciliation are separate durable responsibilities, so a
``response_execution_queued`` event must NEVER be delivered to the observer, and an
observation must never be delivered to the submitter.
"""

from __future__ import annotations

from hisiem_soc_copilot.infrastructure.durable.dispatcher import (
    RESPONSE_OBSERVE_DESTINATION,
    RESPONSE_SUBMIT_DESTINATION,
)
from hisiem_soc_copilot.infrastructure.persistence.repositories.durable import (
    _EVENT_DESTINATIONS,
)
from tests.fixtures.fakes import FAKE_EVENT_DESTINATIONS


def test_fake_destinations_match_the_real_routing() -> None:
    assert FAKE_EVENT_DESTINATIONS == _EVENT_DESTINATIONS


def test_submission_and_reconciliation_are_distinct_destinations() -> None:
    assert RESPONSE_SUBMIT_DESTINATION != RESPONSE_OBSERVE_DESTINATION


def test_the_response_split_is_routed_as_specified() -> None:
    assert _EVENT_DESTINATIONS["response_execution_queued"] == (
        RESPONSE_SUBMIT_DESTINATION
    )
    assert _EVENT_DESTINATIONS["response_execution_submitted"] == (
        RESPONSE_OBSERVE_DESTINATION
    )
    assert _EVENT_DESTINATIONS["response_execution_observed"] == (
        RESPONSE_OBSERVE_DESTINATION
    )
    # Reconciliation is never the submitter's job, and vice versa.
    assert _EVENT_DESTINATIONS["response_execution_queued"] != RESPONSE_OBSERVE_DESTINATION


def test_every_investigation_event_is_routed_to_the_graph_runner() -> None:
    assert _EVENT_DESTINATIONS["investigation_created"] == "investigation.graph.run"
