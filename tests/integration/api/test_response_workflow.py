"""P2 response workflow integration over real Postgres (spec §2/§3/§5/§6).

End-to-end through the real HTTP boundary + real repository stack:
create proposal → human decision → durable SUBMIT → durable OBSERVE → (fake) HISIEM
SOAR → execution projection → workspace projection → audit timeline.

The HISIEM investigation side is faked (FakeHisiem + scripted model); the SOAR side
is a FakeSoar injected into the two response dispatchers. All Copilot persistence
(RESPONSE tables, outbox, domain_event) is real PostgreSQL. Skipped when the DB is
unreachable.

Determinism: the reconciliation cadence is configured to 0 seconds, so a durable
observation becomes claimable immediately. Nothing here sleeps on wall-clock time.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.infrastructure.durable.dispatcher import _MAX_ATTEMPTS
from tests.fixtures.fakes import FakeSoar
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel

_ALERT = "resp-alert-1"
_SECRET = "integration-service-secret-value"
_ENV_NAME = "HISIEM_COPILOT_SERVICE_TOKEN"
_PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"

_TRUNCATE = (
    "response_submission",
    "response_execution_ref",
    "approval_decision",
    "approval_request",
    "response_proposal_evidence",
    "response_proposal_target",
    "response_proposal",
    "tool_invocation",
    "outbox_message",
    "domain_event",
    "command_receipt",
    "orchestration_binding",
    "investigation_result_finding",
    "investigation_result",
    "finding_evidence",
    "finding",
    "evidence",
    "hypothesis_assessment_evidence",
    "hypothesis_assessment",
    "hypothesis",
    "plan_step",
    "plan_revision",
    "investigation",
)


def _settings() -> Settings:
    s = Settings()
    s.database.database_url = (
        "postgresql+psycopg://copilot:copilot@127.0.0.1:5433/copilot"
    )
    s.langgraph.database_url = s.database.database_url
    s.auth.trusted_context_provider = "hisiem_bearer"
    s.auth.hisiem_service_token_env = _ENV_NAME
    # Deterministic reconciliation: a durable observation is immediately claimable.
    s.app.response_observe_interval_seconds = 0.0
    return s


def _headers(
    *, tenant: str = "tenant-a", actor: str = "analyst", bearer: str | None = _SECRET
) -> dict[str, str]:
    headers = {"X-Tenant-ID": tenant, "X-Actor-Subject": actor}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


async def _db_reachable(settings: Settings) -> bool:
    try:
        import psycopg
        from sqlalchemy.engine import make_url

        url = make_url(settings.database.database_url)
        conn = psycopg.connect(
            host=url.host,
            port=url.port,
            user=url.username,
            password=url.password,
            dbname=url.database,
            connect_timeout=2,
        )
        conn.execute("SELECT 1")
        conn.close()
        return True
    except Exception:
        return False


def _script() -> dict[str, Any]:
    return {
        "decide": [
            {
                "tool_name": "hisiem.search_events",
                "arguments": {
                    "from": "2026-09-01T09:55:00Z",
                    "to": "2026-09-01T10:05:00Z",
                    "conditions": [
                        {
                            "field": "event.action",
                            "operator": "is",
                            "value": "authentication_success",
                        }
                    ],
                },
            },
        ],
        "findings": ["root login after brute force"],
        "verdict": {
            "disposition": "MALICIOUS",
            "summary": "SSH compromise confirmed",
            "confidence": 0.9,
        },
    }


class _Harness:
    def __init__(
        self,
        client: httpx.AsyncClient,
        container: Any,
        soar: FakeSoar,
        hisiem: FakeHisiem,
        session_factory: Any,
    ) -> None:
        self.client = client
        self.container = container
        self.soar = soar
        self.hisiem = hisiem
        self._session_factory = session_factory

    def arm_investigation_model(self) -> None:
        """Give the NEXT investigation its own scripted model.

        The script is consumed turn by turn, so a second investigation (e.g. a
        second proposal on the same alert) would otherwise finalize with no tool
        call and therefore no evidence to support a proposal.
        """
        self.container.dispatcher = self.container.outbox_dispatcher(
            hisiem=self.hisiem, model=GroundedSshModel(script=_script())
        )

    async def make_delivery_claimable(
        self, destination: str = "response.execution.submit"
    ) -> None:
        """Simulate the retry backoff elapsing — deterministically, no sleep.

        A transient failure leaves the outbox row with a FUTURE ``available_at``
        (exponential backoff), which is correct but unclaimable. Time is advanced by
        rewriting that timestamp into the past rather than by sleeping on the wall
        clock.
        """
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "UPDATE copilot.outbox_message "
                    "SET available_at = now() - interval '1 hour' "
                    "WHERE destination = :destination "
                    "AND status IN ('PENDING','FAILED')"
                ),
                {"destination": destination},
            )
            await session.commit()

    async def make_submit_delivery_claimable(self) -> None:
        await self.make_delivery_claimable("response.execution.submit")

    async def make_observe_delivery_claimable(self) -> None:
        await self.make_delivery_claimable("response.execution.observe")

    async def requeue_submit_delivery(self) -> None:
        """Undo a dead-letter: simulate a worker that died before settling the row.

        The business fact (e.g. FAILED_DEFINITIVE) is already committed; only the
        outbox settlement was lost, so the lease expires and the SAME delivery is
        reclaimed and re-delivered.
        """
        async with self._session_factory() as session:
            await session.execute(
                text(
                    "UPDATE copilot.outbox_message "
                    "SET status = 'PENDING', locked_at = NULL, locked_by = NULL, "
                    "lease_token = NULL, available_at = now() - interval '1 hour' "
                    "WHERE destination = 'response.execution.submit'"
                )
            )
            await session.commit()

    async def drain_submit(self) -> int:
        return await self.container.response_submit_dispatcher.drain_once()

    async def drain_observe(self) -> int:
        return await self.container.response_observe_dispatcher.drain_once()

    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self._session_factory() as session:
            result = await session.execute(text(sql), params)
            row = result.first()
            return None if row is None else row[0]

    async def rows(self, sql: str, **params: Any) -> list[tuple[Any, ...]]:
        async with self._session_factory() as session:
            result = await session.execute(text(sql), params)
            return [tuple(r) for r in result.all()]

    def restart_workers(self, *, status_sequence: list[str] | None = None) -> FakeSoar:
        """Simulate a PROCESS RESTART: brand-new runners over the same durable state."""
        soar = FakeSoar(
            submit_result=SoarExecutionResult(
                execution_id="hisiem-exec-restart", status="RUNNING"
            ),
            status_sequence=status_sequence,
        )
        self.container.response_submit_dispatcher = (
            self.container.response_submit_outbox_dispatcher(
                soar=soar, observe_delay_seconds=0.0
            )
        )
        self.container.response_observe_dispatcher = (
            self.container.response_observe_outbox_dispatcher(
                soar=soar, observe_delay_seconds=0.0
            )
        )
        return soar


@pytest_asyncio.fixture
async def harness(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_Harness]:
    settings = _settings()
    if not await _db_reachable(settings):
        pytest.skip("PostgreSQL not reachable — skipping response workflow test")

    monkeypatch.setenv(_ENV_NAME, _SECRET)

    async def _truncate(session_factory: Any) -> None:
        async with session_factory() as session:
            await session.execute(
                text(
                    f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} "
                    "RESTART IDENTITY CASCADE"
                )
            )
            await session.commit()

    engine = create_async_engine(settings.database.database_url)
    factory = async_sessionmaker(
        bind=engine, class_=AsyncSession, expire_on_commit=False
    )
    await _truncate(factory)

    app = create_app(settings)
    async with LifespanManager(app):
        container = app.state.container
        hisiem = FakeHisiem(alert_id=_ALERT)
        container.hisiem_adapter = hisiem  # type: ignore[assignment]
        soar = FakeSoar()
        container.response_submit_dispatcher = (
            container.response_submit_outbox_dispatcher(
                soar=soar, observe_delay_seconds=0.0
            )
        )
        container.response_observe_dispatcher = (
            container.response_observe_outbox_dispatcher(
                soar=soar, observe_delay_seconds=0.0
            )
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            harness = _Harness(c, container, soar, hisiem, factory)
            harness.arm_investigation_model()
            yield harness
        await _truncate(factory)
    await engine.dispose()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _completed_investigation(
    harness: _Harness, *, alert: str = _ALERT
) -> str:
    res = await harness.client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": alert,
            }
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    investigation_id = res.json()["investigation_id"]
    harness.arm_investigation_model()
    await harness.container.dispatcher.drain_once()
    return investigation_id


async def _workspace(harness: _Harness, investigation_id: str) -> dict[str, Any]:
    res = await harness.client.get(
        f"/api/v1/investigations/{investigation_id}/workspace", headers=_headers()
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _create_proposal(
    harness: _Harness,
    investigation_id: str,
    evidence_ids: list[str],
    *,
    reason: str = "contain",
) -> dict[str, Any]:
    """POST a proposal. NOTE: the body carries NO target — it is server-derived."""
    res = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "evidence_ids": evidence_ids,
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": reason,
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    return res.json()["proposal"]


async def _proposal_with_approval(
    harness: _Harness, *, alert: str = _ALERT
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    investigation_id = await _completed_investigation(harness, alert=alert)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]
    return investigation_id, proposal, approval, ws2


async def _approve(
    harness: _Harness, approval: dict[str, Any], proposal: dict[str, Any]
) -> httpx.Response:
    return await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(actor="operator"),
    )


# ---------------------------------------------------------------------------
# §1 — the trusted proposal-input boundary
# ---------------------------------------------------------------------------


async def test_proposal_body_cannot_carry_a_target(harness: _Harness) -> None:
    """A caller-supplied target FAILS LOUDLY instead of being silently ignored."""
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]

    res = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "target": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": "some-other-alert",
            },
            "evidence_ids": evidence_ids,
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": "contain",
        },
        headers=_headers(),
    )
    assert res.status_code == 422, res.text
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_proposal"
    ) == 0


async def test_proposal_target_is_the_persisted_source_alert(harness: _Harness) -> None:
    _, proposal, _, ws = await _proposal_with_approval(harness)

    persisted = await harness.rows(
        "SELECT provider, resource_type, address_id, business_id "
        "FROM copilot.response_proposal_target WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    )
    # Exactly ONE target, and it is the persisted source alert of the investigation
    # the proposal hangs off — nothing the browser could have named or forged.
    assert persisted == [("hisiem", "alert", _ALERT, None)]
    targets = ws["response"]["proposals"][0]["target_refs"]
    assert len(targets) == 1
    assert targets[0]["address_id"] == _ALERT


async def test_out_of_scope_evidence_is_rejected(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)

    res = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "evidence_ids": [str(uuid4())],
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": "contain",
        },
        headers=_headers(),
    )
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "RESPONSE_EVIDENCE_INVALID"
    # No existence oracle: the message states the RULE and never whether a row
    # exists, so the endpoint cannot be probed for other tenants' data.
    assert "does not exist" not in res.text.lower()
    assert res.json()["message"] == (
        "supporting evidence must reference evidence recorded on this investigation"
    )
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_proposal"
    ) == 0


# ---------------------------------------------------------------------------
# §2 / §5 — approval queues a SUBMISSION; only the worker creates the provider ref
# ---------------------------------------------------------------------------


async def test_proposal_requires_approval_and_projects_to_workspace(
    harness: _Harness,
) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]

    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    assert proposal["status"] == "WAITING_APPROVAL"
    assert proposal["policy_decision"] == "REQUIRE_APPROVAL"

    ws2 = await _workspace(harness, investigation_id)
    proposals = ws2["response"]["proposals"]
    assert len(proposals) == 1
    p = proposals[0]
    assert p["status"] == "WAITING_APPROVAL"
    assert p["approval"] is not None
    assert p["approval"]["decision"] is None
    assert p["approval"]["expected_revision"] == proposal["revision"]
    # No execution exists before a human decides.
    assert p["execution"] is None
    kinds = {t["kind"] for t in ws2["timeline"]}
    assert any(k.startswith("RESPONSE_") for k in kinds)


async def test_approval_queues_submission_and_creates_no_execution(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)

    approve = await _approve(harness, approval, proposal)
    assert approve.status_code == 200, approve.text
    body = approve.json()
    assert body["execution_queued"] is True
    assert body["status"] == "APPROVED"

    # No SOAR call happened inside the HTTP request (durable queue only).
    assert harness.soar.submitted == []
    # …and NO provider execution reference was fabricated.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["status"] == "APPROVED"
    # No fake external execution id may be shown while nothing was submitted.
    assert p["execution"] is None
    # The entry is a LOCAL SUBMISSION fact, not an execution fact: there is no
    # provider execution, so it must never be reported as one.
    queued = [
        t for t in ws["timeline"] if t["kind"] == "RESPONSE_SUBMISSION_QUEUED"
    ]
    assert len(queued) == 1
    assert queued[0]["status"] == "AWAITING_SUBMISSION"
    assert queued[0]["ref_id"] == proposal["proposal_id"]
    assert not [
        t for t in ws["timeline"] if t["kind"] == "RESPONSE_EXECUTION_QUEUED"
    ]

    # The investigation lifecycle is untouched by approval (§5).
    assert await harness.scalar(
        "SELECT status FROM copilot.investigation WHERE id = :iid",
        iid=investigation_id,
    ) == "COMPLETED"


async def test_submit_then_observe_settles_with_a_real_execution_id(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200

    assert await harness.drain_submit() == 1
    assert len(harness.soar.submitted) == 1
    expected_key = f"response:tenant-a:{proposal['proposal_id']}"
    assert harness.soar.submitted[0]["submission_key"] == expected_key

    execution_id = await harness.scalar(
        "SELECT execution_id FROM copilot.response_execution_ref "
        "WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    )
    assert execution_id  # non-empty, real provider identity
    assert execution_id == "hisiem-exec-1"

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["status"] == "SUBMITTED"  # a REAL lifecycle state (§5)
    assert p["execution"]["status"] == "SUCCEEDED"
    assert p["execution"]["external_execution_id"] == execution_id
    assert p["execution"]["finished_at"] is not None

    # The observe delivery settles; a further drain is a no-op.
    assert await harness.drain_submit() == 0
    assert len(harness.soar.submitted) == 1


async def test_two_approvals_before_any_drain_do_not_collide(harness: _Harness) -> None:
    """Approving A and B with nothing drained must not collide on provider identity."""
    harness.soar.submit_sequence = [
        SoarExecutionResult(execution_id="hisiem-exec-A", status="SUCCEEDED"),
        SoarExecutionResult(execution_id="hisiem-exec-B", status="SUCCEEDED"),
    ]

    # Two INDEPENDENT investigations (the first is already COMPLETED, so the
    # alert is free again) — nothing is drained between their approvals.
    inv_a, proposal_a, approval_a, _ = await _proposal_with_approval(harness)
    inv_b, proposal_b, approval_b, _ = await _proposal_with_approval(harness)
    assert inv_a != inv_b

    assert (await _approve(harness, approval_a, proposal_a)).status_code == 200
    # The OLD model wrote a provider reference with an EMPTY execution id at approval
    # time, so this second approval would have raised UNIQUE(provider, execution_id).
    # It must succeed.
    second = await _approve(harness, approval_b, proposal_b)
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "APPROVED"
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0

    # Drain both; each gets its OWN real provider execution.
    assert await harness.drain_submit() == 2
    assert len(harness.soar.submitted) == 2
    keys = {call["submission_key"] for call in harness.soar.submitted}
    assert keys == {
        f"response:tenant-a:{proposal_a['proposal_id']}",
        f"response:tenant-a:{proposal_b['proposal_id']}",
    }
    execution_ids = {
        await harness.scalar(
            "SELECT execution_id FROM copilot.response_execution_ref "
            "WHERE proposal_id = :pid",
            pid=proposal_a["proposal_id"],
        ),
        await harness.scalar(
            "SELECT execution_id FROM copilot.response_execution_ref "
            "WHERE proposal_id = :pid",
            pid=proposal_b["proposal_id"],
        ),
    }
    assert execution_ids == {"hisiem-exec-A", "hisiem-exec-B"}
    assert await harness.scalar(
        "SELECT count(DISTINCT execution_id) FROM copilot.response_execution_ref"
    ) == 2
    # At most one provider execution per proposal.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 2


async def test_duplicate_submit_delivery_never_double_executes(harness: _Harness) -> None:
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    # Re-arm the exact same durable delivery (as a crash/redelivery would).
    await harness.scalar(
        "UPDATE copilot.outbox_message SET status = 'PENDING', available_at = now() "
        "WHERE destination = 'response.execution.submit' RETURNING id"
    )
    await harness.drain_submit()

    assert len(harness.soar.submitted) == 1
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 1


# ---------------------------------------------------------------------------
# §3 — durable reconciliation
# ---------------------------------------------------------------------------


async def test_long_running_execution_is_reconciled_across_restarts(
    harness: _Harness,
) -> None:
    """A legitimately RUNNING playbook must NEVER dead-letter, and must survive restarts."""
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-1", status="RUNNING"),
        status_sequence=["RUNNING"] * (_MAX_ATTEMPTS + 2),
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    harness.container.response_observe_dispatcher = (
        harness.container.response_observe_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    # Many reconciliation cycles — far more than _MAX_ATTEMPTS.
    for _ in range(_MAX_ATTEMPTS + 2):
        await harness.drain_observe()

    assert await harness.scalar(
        "SELECT count(*) FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.observe' AND status = 'DEAD_LETTER'"
    ) == 0
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.outbox_message WHERE status = 'FAILED'"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_execution_ref WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == "RUNNING"

    # Restart: a brand-new worker with no in-memory state finishes the job.
    restart_soar = harness.restart_workers(status_sequence=["SUCCEEDED"])
    await harness.drain_observe()

    assert restart_soar.status_calls == ["hisiem-exec-1"]
    assert await harness.scalar(
        "SELECT status FROM copilot.response_execution_ref WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == "SUCCEEDED"


async def test_a_terminal_execution_is_not_re_observed(harness: _Harness) -> None:
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-1", status="RUNNING"),
        status_sequence=["SUCCEEDED"],
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    harness.container.response_observe_dispatcher = (
        harness.container.response_observe_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    await harness.drain_observe()

    # A terminal execution no longer schedules observation.
    assert await harness.drain_observe() == 0
    assert soar.status_calls == ["hisiem-exec-1"]


async def test_observation_never_re_posts_the_execution(harness: _Harness) -> None:
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    await harness.drain_observe()
    await harness.drain_observe()

    assert len(harness.soar.submitted) == 1


# ---------------------------------------------------------------------------
# §5 — the investigation lifecycle is independent and never reopened
# ---------------------------------------------------------------------------


async def test_response_proposal_id_is_linked_without_reopening(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)

    linked = await harness.scalar(
        "SELECT response_proposal_id FROM copilot.investigation WHERE id = :iid",
        iid=investigation_id,
    )
    assert str(linked) == proposal["proposal_id"]

    before = await harness.scalar(
        "SELECT status || '|' || termination_reason FROM copilot.investigation "
        "WHERE id = :iid",
        iid=investigation_id,
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    await harness.drain_observe()

    after = await harness.scalar(
        "SELECT status || '|' || termination_reason FROM copilot.investigation "
        "WHERE id = :iid",
        iid=investigation_id,
    )
    # Approval, submission and observation never change Investigation.status.
    assert after == before
    assert after.startswith("COMPLETED|")
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.investigation WHERE status IN "
        "('WAITING_APPROVAL','EXECUTING_RESPONSE')"
    ) == 0


async def test_reject_produces_zero_executions(harness: _Harness) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)

    reject = await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/reject",
        json={
            "decision": "REJECT",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
            "reason": "insufficient evidence",
        },
        headers=_headers(actor="operator"),
    )
    assert reject.status_code == 200, reject.text
    assert reject.json()["execution_queued"] is False

    assert await harness.drain_submit() == 0
    assert await harness.drain_observe() == 0
    assert harness.soar.submitted == []
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal["proposal_id"],
    ) == "REJECTED"

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["status"] == "REJECTED"
    assert p["execution"] is None


async def test_stale_contract_approval_is_rejected(harness: _Harness) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)

    res = await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": "0" * 64,  # wrong → stale contract
        },
        headers=_headers(actor="operator"),
    )
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "APPROVAL_CONTRACT_MISMATCH"
    assert await harness.drain_submit() == 0
    assert harness.soar.submitted == []
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == 0


async def test_decision_body_mismatch_with_route_is_rejected(harness: _Harness) -> None:
    _, proposal, approval, _ = await _proposal_with_approval(harness)

    # Route says approve, body says reject → refused (no smuggling).
    res = await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "REJECT",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(actor="operator"),
    )
    assert res.status_code == 400, res.text


async def test_response_workflow_is_tenant_scoped(harness: _Harness) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)

    # A valid service credential asserting a different tenant cannot see or act on it.
    foreign = await harness.client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(tenant="tenant-b"),
    )
    assert foreign.status_code == 404
    assert investigation_id not in foreign.text

    foreign_approve = await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(tenant="tenant-b", actor="operator"),
    )
    assert foreign_approve.status_code == 404

    assert await harness.drain_submit() == 0
    assert harness.soar.submitted == []


async def test_submission_projection_is_tenant_scoped(harness: _Harness) -> None:
    """The local submission lifecycle is reachable only through its own tenant."""
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200

    from hisiem_soc_copilot.infrastructure.persistence.repositories.response import (
        SqlAlchemyResponseSubmissionRepository,
    )

    async with harness.container.session_factory()() as session:
        repo = SqlAlchemyResponseSubmissionRepository(session)
        own = await repo.get_by_proposal(
            tenant_id="tenant-a", proposal_id=UUID(proposal["proposal_id"])
        )
        foreign_submission = await repo.get_by_proposal(
            tenant_id="tenant-b", proposal_id=UUID(proposal["proposal_id"])
        )
    assert own is not None
    assert own.status == "PENDING"
    assert foreign_submission is None


async def test_response_workflow_rejects_a_missing_service_credential(
    harness: _Harness,
) -> None:
    """§43/§44 — a bad/absent S2S credential is exactly one generic 401."""
    investigation_id = await _completed_investigation(harness)

    res = await harness.client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(bearer=None),
    )
    assert res.status_code == 401, res.text
    assert res.json()["code"] == "SERVICE_AUTHENTICATION_FAILED"
    assert res.json()["message"] == "service authentication failed"


async def test_response_timeline_is_deterministic(harness: _Harness) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    await harness.drain_observe()

    first = (await _workspace(harness, investigation_id))["timeline"]
    second = (await _workspace(harness, investigation_id))["timeline"]
    # Same request twice → byte-identical ordering (no nondeterministic tiebreak).
    assert [(t["kind"], t["occurred_at"], t["ref_id"]) for t in first] == [
        (t["kind"], t["occurred_at"], t["ref_id"]) for t in second
    ]
    occurred = [t["occurred_at"] for t in first]
    assert occurred == sorted(occurred)


# ---------------------------------------------------------------------------
# §2 — decisive failure paths
# ---------------------------------------------------------------------------


async def test_definitive_submit_failure_dead_letters_without_a_fake_ref(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=FakeSoar(
                raise_on_submit=ExternalServiceError(
                    "bad playbook", service="hisiem", code="HTTP_422"
                )
            ),
            observe_delay_seconds=0.0,
        )
    )

    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    # No provider execution exists, so NO provider identity may be persisted.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal["proposal_id"],
    ) == "APPROVED"
    assert await harness.scalar(
        "SELECT status FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == "DEAD_LETTER"

    # The DEFINITIVE refusal is a fact about the SUBMISSION, and it must be
    # persisted as one: without it the workspace would keep claiming "awaiting
    # submission" forever.
    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == "FAILED_DEFINITIVE"
    assert await harness.scalar(
        "SELECT last_error_code FROM copilot.response_submission "
        "WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == "HTTP_422"
    assert await harness.scalar(
        "SELECT safe_error_message IS NOT NULL FROM copilot.response_submission "
        "WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) is True

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["execution"] is None  # no fabricated execution identity
    assert p["submission"]["status"] == "FAILED_DEFINITIVE"
    # ...and the workspace must stop claiming that submission is still pending.
    kinds = [t["kind"] for t in ws["timeline"]]
    assert "RESPONSE_SUBMISSION_FAILED" in kinds
    assert "RESPONSE_SUBMISSION_QUEUED" not in kinds
    assert "RESPONSE_SUBMISSION_RETRYING" not in kinds
    assert not [
        t for t in ws["timeline"] if t["kind"].startswith("RESPONSE_EXECUTION_")
    ]


async def test_transient_submit_failure_stays_queued(harness: _Harness) -> None:
    _, proposal, approval, _ = await _proposal_with_approval(harness)
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=FakeSoar(
                raise_on_submit=ExternalServiceError(
                    "unavailable", service="hisiem", code="HTTP_503"
                )
            ),
            observe_delay_seconds=0.0,
        )
    )

    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    assert await harness.scalar(
        "SELECT status FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == "FAILED"
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    # Still retryable, and recorded as such - never as a provider failure.
    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "RETRYING"
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.response_submission"
    ) >= 1
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.domain_event "
        "WHERE event_type = 'response_submission_failed'"
    ) == 0


async def test_observed_provider_failure_settles_the_execution(harness: _Harness) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="hisiem-exec-1", status="RUNNING"),
        status_sequence=["FAILED"],
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    harness.container.response_observe_dispatcher = (
        harness.container.response_observe_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    await harness.drain_observe()

    ws = await _workspace(harness, investigation_id)
    execution = ws["response"]["proposals"][0]["execution"]
    assert execution is not None
    assert execution["status"] == "FAILED"
    assert execution["finished_at"] is not None
    assert execution["external_execution_id"] == "hisiem-exec-1"


# ---------------------------------------------------------------------------
# §1 provenance — who proposed this response?
# ---------------------------------------------------------------------------


async def test_proposal_persists_the_authenticated_proposer(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]

    res = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "evidence_ids": evidence_ids,
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": "contain",
        },
        headers=_headers(actor="analyst-proposer"),
    )
    assert res.status_code == 201, res.text
    proposal_id = res.json()["proposal"]["proposal_id"]

    # Round-trip through the workspace read model (a fresh read, i.e. a refresh).
    first = (await _workspace(harness, investigation_id))["response"]["proposals"][0]
    assert first["created_by_subject"] == "analyst-proposer"
    second = (await _workspace(harness, investigation_id))["response"]["proposals"][0]
    assert second["created_by_subject"] == "analyst-proposer"

    # Persisted, immutable, and NOT borrowed from the investigation initiator.
    assert await harness.scalar(
        "SELECT created_by_subject FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal_id,
    ) == "analyst-proposer"
    assert await harness.scalar(
        "SELECT initiated_by_subject FROM copilot.investigation WHERE id = :iid",
        iid=investigation_id,
    ) == "analyst"

    # The audit event names the proposer as its actor, so the ledger alone answers
    # "who proposed this response".
    assert await harness.scalar(
        "SELECT actor_subject_id FROM copilot.domain_event "
        "WHERE event_type = 'response_proposal_created' AND aggregate_id = :pid",
        pid=proposal_id,
    ) == "analyst-proposer"

    # The browser cannot supply a creator: an out-of-bounds field is rejected loudly.
    forged = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "evidence_ids": evidence_ids,
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": "contain",
            "created_by_subject": "someone-else",
        },
        headers=_headers(actor="analyst-proposer"),
    )
    assert forged.status_code == 422, forged.text


# ---------------------------------------------------------------------------
# §2 the local submission lifecycle
# ---------------------------------------------------------------------------


async def test_approval_persists_a_pending_submission_intent(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    assert (await _approve(harness, approval, proposal)).status_code == 200

    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == "PENDING"
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.response_submission "
        "WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    ) == 0
    key = await harness.scalar(
        "SELECT submission_key FROM copilot.response_submission "
        "WHERE proposal_id = :pid",
        pid=proposal["proposal_id"],
    )
    assert key == f"response:tenant-a:{proposal['proposal_id']}"

    # Still local-only: the approval transaction created NO provider execution.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0

    ws = await _workspace(harness, investigation_id)
    assert ws["response"]["proposals"][0]["submission"]["status"] == "PENDING"
    assert ws["response"]["proposals"][0]["execution"] is None


@pytest.mark.parametrize("status", [400, 404, 409, 422])
async def test_definitive_submission_rejection_is_not_an_execution_failure(
    harness: _Harness, status: int
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=FakeSoar(
                raise_on_submit=ExternalServiceError(
                    "rejected", service="hisiem", code=f"HTTP_{status}"
                )
            ),
            observe_delay_seconds=0.0,
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "FAILED_DEFINITIVE"
    assert await harness.scalar(
        "SELECT last_error_code FROM copilot.response_submission"
    ) == f"HTTP_{status}"
    # No provider execution was created, so NOTHING may claim one failed.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.domain_event "
        "WHERE event_type IN ('response_execution_failed', 'response_execution_started')"
    ) == 0
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.domain_event "
        "WHERE event_type = 'response_submission_failed'"
    ) == 1

    ws = await _workspace(harness, investigation_id)
    refreshed = await _workspace(harness, investigation_id)
    assert ws["response"]["proposals"][0] == refreshed["response"]["proposals"][0]
    assert ws["timeline"] == refreshed["timeline"]


async def test_transient_submission_failure_projects_as_retrying(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=FakeSoar(
                raise_on_submit=ExternalServiceError(
                    "throttled", service="hisiem", code="HTTP_429"
                )
            ),
            observe_delay_seconds=0.0,
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["submission"]["status"] == "RETRYING"
    assert p["submission"]["attempt_count"] >= 1
    assert p["execution"] is None
    kinds = [t["kind"] for t in ws["timeline"]]
    assert "RESPONSE_SUBMISSION_RETRYING" in kinds
    assert "RESPONSE_SUBMISSION_FAILED" not in kinds
    # The proposal itself is untouched: nothing was accepted and nothing failed.
    assert p["status"] == "APPROVED"


async def test_a_successful_retry_settles_submission_and_creates_the_ref(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    # First delivery throttled, second accepted: the SAME submission key is reused.
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=FakeSoar(
                raise_on_submit=ExternalServiceError(
                    "throttled", service="hisiem", code="HTTP_429"
                )
            ),
            observe_delay_seconds=0.0,
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "RETRYING"

    # Provider recovers; the retry must converge on the SAME logical submission.
    soar = FakeSoar(
        submit_result=SoarExecutionResult(execution_id="exec-retry-1", status="RUNNING")
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    await harness.make_submit_delivery_claimable()
    await harness.drain_submit()

    assert [call["submission_key"] for call in soar.submitted] == [
        f"response:tenant-a:{proposal['proposal_id']}"
    ]
    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "SUBMITTED"
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.response_submission"
    ) >= 1
    assert await harness.scalar(
        "SELECT execution_id FROM copilot.response_execution_ref"
    ) == "exec-retry-1"
    assert await harness.scalar(
        "SELECT status FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal["proposal_id"],
    ) == "SUBMITTED"

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["submission"]["status"] == "SUBMITTED"
    assert p["execution"]["external_execution_id"] == "exec-retry-1"
    kinds = [t["kind"] for t in ws["timeline"]]
    assert "RESPONSE_EXECUTION_QUEUED" in kinds
    assert "RESPONSE_SUBMISSION_FAILED" not in kinds


# ---------------------------------------------------------------------------
# §4 concurrent first-creates converge (never two proposals, never a 500)
# ---------------------------------------------------------------------------


async def test_concurrent_first_creates_converge_on_one_proposal(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import contextlib

    from hisiem_soc_copilot.infrastructure.persistence.repositories.response import (
        SqlAlchemyResponseProposalRepository,
    )

    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]

    # Force the exact interleaving under test: both requests must pass the
    # "does a proposal already exist?" pre-check before either may INSERT. Without
    # this the race is real but not deterministic; nothing about the production
    # path is weakened - the loser still hits the real UNIQUE constraint.
    original = SqlAlchemyResponseProposalRepository.get_by_investigation
    barrier = asyncio.Event()
    arrived = 0
    guard = asyncio.Lock()

    async def gated(
        self: Any, *, tenant_id: str, investigation_id: Any
    ) -> Any:
        nonlocal arrived
        async with guard:
            arrived += 1
            if arrived >= 2:
                barrier.set()
        # Safety valve only: never expected to fire, and never a wall-clock wait
        # for correctness (both requests are in flight before the barrier opens).
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return await original(
            self, tenant_id=tenant_id, investigation_id=investigation_id
        )

    monkeypatch.setattr(
        SqlAlchemyResponseProposalRepository, "get_by_investigation", gated
    )

    async def create() -> httpx.Response:
        return await harness.client.post(
            f"/api/v1/investigations/{investigation_id}/response-proposals",
            json={
                "action_key": "START_SOAR_PLAYBOOK",
                "evidence_ids": evidence_ids,
                "parameters": {"playbook_id": _PLAYBOOK_ID},
                "reason": "contain",
            },
            headers=_headers(),
        )

    responses = await asyncio.gather(create(), create())

    # Never a raw integrity error (500): the loser converges or gets a 409.
    assert [r.status_code for r in responses] != []
    for res in responses:
        assert res.status_code in (201, 409), res.text
    accepted = [r for r in responses if r.status_code == 201]
    assert accepted, [r.text for r in responses]

    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_proposal WHERE investigation_id = :iid",
        iid=investigation_id,
    ) == 1
    winners = {r.json()["proposal"]["proposal_id"] for r in accepted}
    assert len(winners) == 1

    # The investigation is linked to that ONE proposal, and stays COMPLETED.
    assert await harness.scalar(
        "SELECT response_proposal_id FROM copilot.investigation WHERE id = :iid",
        iid=investigation_id,
    ) == UUID(winners.pop())
    assert await harness.scalar(
        "SELECT status FROM copilot.investigation WHERE id = :iid",
        iid=investigation_id,
    ) == "COMPLETED"


# ---------------------------------------------------------------------------
# §1 crash/redelivery safety, and §2 an exhausted submit budget
# ---------------------------------------------------------------------------


async def test_a_redelivered_submit_after_a_definitive_refusal_converges(
    harness: _Harness,
) -> None:
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "rejected", service="hisiem", code="HTTP_422"
        )
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()

    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "FAILED_DEFINITIVE"
    assert len(soar.submitted) == 1

    # Simulate the crash window: the refusal is committed but the outbox row was
    # never dead-lettered, so it is reclaimed and the SAME delivery is re-delivered.
    await harness.requeue_submit_delivery()
    assert await harness.drain_submit() == 1

    # The provider is not asked again — we already know, durably, that it refused.
    assert len(soar.submitted) == 1
    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "FAILED_DEFINITIVE"
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.response_submission"
    ) == 1
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal["proposal_id"],
    ) == "APPROVED"
    # The stale delivery settles normally rather than being dead-lettered.
    assert await harness.scalar(
        "SELECT status FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == "PUBLISHED"

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    assert p["submission"]["status"] == "FAILED_DEFINITIVE"
    assert p["execution"] is None


async def test_an_exhausted_submit_budget_becomes_attention_required(
    harness: _Harness,
) -> None:
    """Exhaustion is a BUSINESS fact persisted before the outbox goes terminal."""
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    soar = FakeSoar(
        raise_on_submit=ExternalServiceError(
            "unavailable", service="hisiem", code="HTTP_503"
        )
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200

    for _ in range(_MAX_ATTEMPTS + 2):
        await harness.make_submit_delivery_claimable()
        await harness.drain_submit()

    assert await harness.scalar(
        "SELECT status FROM copilot.response_submission"
    ) == "ATTENTION_REQUIRED"
    assert await harness.scalar(
        "SELECT attention_required_at IS NOT NULL FROM copilot.response_submission"
    ) is True
    # Every claim was a real provider attempt, so the transport's own counter is
    # the exact number of submissions tried — and the local submission agrees.
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == len(soar.submitted)
    assert len(soar.submitted) >= 1
    assert await harness.scalar(
        "SELECT attempt_count FROM copilot.response_submission"
    ) == len(soar.submitted)
    assert await harness.scalar(
        "SELECT last_error_code FROM copilot.response_submission"
    ) == "HTTP_503"
    assert await harness.scalar(
        "SELECT status FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.submit'"
    ) == "DEAD_LETTER"

    # No provider execution exists, none is claimed to have failed, and the
    # proposal is untouched: nobody knows whether the provider accepted this.
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.response_execution_ref"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_proposal WHERE id = :pid",
        pid=proposal["proposal_id"],
    ) == "APPROVED"
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.domain_event "
        "WHERE event_type IN ('response_execution_failed','response_submission_failed')"
    ) == 0
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.domain_event "
        "WHERE event_type = 'response_submission_attention_required'"
    ) == 1

    ws = await _workspace(harness, investigation_id)
    p = ws["response"]["proposals"][0]
    # The workspace must stop claiming a retry that is no longer happening.
    assert p["submission"]["status"] == "ATTENTION_REQUIRED"
    assert p["execution"] is None
    kinds = [t["kind"] for t in ws["timeline"]]
    assert "RESPONSE_SUBMISSION_ATTENTION_REQUIRED" in kinds
    assert "RESPONSE_SUBMISSION_RETRYING" not in kinds
    assert not [
        t for t in ws["timeline"] if t["kind"].startswith("RESPONSE_EXECUTION_")
    ]

    # Refreshing reconstructs the same truth.
    refreshed = await _workspace(harness, investigation_id)
    assert refreshed["response"]["proposals"][0] == p


async def test_a_prolonged_observe_outage_never_loses_reconciliation(
    harness: _Harness,
) -> None:
    """A real execution exists; a long provider outage must not end reconciliation."""
    investigation_id, proposal, approval, _ = await _proposal_with_approval(harness)
    # A genuinely long-running execution: it must still be RUNNING while the
    # provider is unreadable, so the outage cannot be confused with a fast success.
    soar = FakeSoar(
        submit_result=SoarExecutionResult(
            execution_id="exec-outage-1", status="RUNNING"
        )
    )
    harness.container.response_submit_dispatcher = (
        harness.container.response_submit_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    harness.container.response_observe_dispatcher = (
        harness.container.response_observe_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    assert (await _approve(harness, approval, proposal)).status_code == 200
    await harness.drain_submit()
    assert await harness.scalar(
        "SELECT status FROM copilot.response_execution_ref"
    ) == "RUNNING"

    # The provider becomes unreadable for far longer than the generic budget.
    soar.raise_on_status = ExternalServiceError(
        "unavailable", service="hisiem", code="HTTP_503"
    )
    for _ in range(_MAX_ATTEMPTS * 2):
        await harness.make_observe_delivery_claimable()
        await harness.drain_observe()

    assert await harness.scalar(
        "SELECT count(*) FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.observe' AND status = 'DEAD_LETTER'"
    ) == 0
    assert await harness.scalar(
        "SELECT status FROM copilot.response_execution_ref"
    ) == "RUNNING"  # an unreadable provider never settles the execution

    # PROCESS RESTART: brand-new workers over the same durable state.
    harness.restart_workers()
    harness.container.response_observe_dispatcher = (
        harness.container.response_observe_outbox_dispatcher(
            soar=soar, observe_delay_seconds=0.0
        )
    )
    soar.raise_on_status = None
    soar.status_sequence = ["SUCCEEDED"]
    for _ in range(5):
        await harness.make_observe_delivery_claimable()
        await harness.drain_observe()
        if await harness.scalar(
            "SELECT status FROM copilot.response_execution_ref"
        ) == "SUCCEEDED":
            break

    assert await harness.scalar(
        "SELECT status FROM copilot.response_execution_ref"
    ) == "SUCCEEDED"
    assert await harness.scalar(
        "SELECT count(*) FROM copilot.outbox_message "
        "WHERE destination = 'response.execution.observe' AND status = 'DEAD_LETTER'"
    ) == 0
    ws = await _workspace(harness, investigation_id)
    assert ws["response"]["proposals"][0]["execution"]["status"] == "SUCCEEDED"
