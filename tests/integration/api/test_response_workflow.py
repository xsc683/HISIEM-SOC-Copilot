"""P2 response workflow integration over real Postgres.

End-to-end through the real HTTP boundary + real repository stack:
create proposal → approval → durable queue → response worker → (fake) HISIEM SOAR
→ execution status → workspace projection → audit timeline.

The HISIEM investigation side is faked (FakeHisiem + scripted model); the SOAR
side is a FakeSoar injected into the response dispatcher. All Copilot persistence
(RESPONSE tables, outbox, domain_event) is real PostgreSQL. Skipped when the DB is
unreachable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.application.errors import ExternalServiceError
from hisiem_soc_copilot.config import Settings
from tests.fixtures.fakes import FakeSoar
from tests.fixtures.hisiem_fake import FakeHisiem
from tests.fixtures.ssh_models import GroundedSshModel

_ALERT = "resp-alert-1"
_SECRET = "integration-service-secret-value"
_ENV_NAME = "HISIEM_COPILOT_SERVICE_TOKEN"
_PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"

_TRUNCATE = (
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
    def __init__(self, client: httpx.AsyncClient, container: Any, soar: FakeSoar) -> None:
        self.client = client
        self.container = container
        self.soar = soar


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
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)

    app = create_app(settings)
    async with LifespanManager(app):
        container = app.state.container
        hisiem = FakeHisiem(alert_id=_ALERT)
        container.hisiem_adapter = hisiem  # type: ignore[assignment]
        container.dispatcher = container.outbox_dispatcher(
            hisiem=hisiem, model=GroundedSshModel(script=_script())
        )
        soar = FakeSoar()
        container.response_dispatcher = container.response_outbox_dispatcher(soar=soar)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            yield _Harness(c, container, soar)
        await _truncate(factory)
    await engine.dispose()


async def _completed_investigation(harness: _Harness) -> str:
    res = await harness.client.post(
        "/api/v1/investigations",
        json={
            "source_alert_ref": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": _ALERT,
            }
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    investigation_id = res.json()["investigation_id"]
    await harness.container.dispatcher.drain_once()
    return investigation_id


async def _workspace(harness: _Harness, investigation_id: str) -> dict[str, Any]:
    res = await harness.client.get(
        f"/api/v1/investigations/{investigation_id}/workspace", headers=_headers()
    )
    assert res.status_code == 200, res.text
    return res.json()


async def _create_proposal(
    harness: _Harness, investigation_id: str, evidence_ids: list[str], *, reason: str = "contain"
) -> dict[str, Any]:
    res = await harness.client.post(
        f"/api/v1/investigations/{investigation_id}/response-proposals",
        json={
            "action_key": "START_SOAR_PLAYBOOK",
            "target": {
                "provider": "hisiem",
                "resource_type": "alert",
                "address_id": _ALERT,
            },
            "evidence_ids": evidence_ids,
            "parameters": {"playbook_id": _PLAYBOOK_ID},
            "reason": reason,
        },
        headers=_headers(),
    )
    assert res.status_code == 201, res.text
    return res.json()["proposal"]


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
    # Audit timeline carries the response events.
    kinds = {t["kind"] for t in ws2["timeline"]}
    assert any(k.startswith("RESPONSE_") for k in kinds)


async def test_approval_queues_execution_worker_settles_and_projects(
    harness: _Harness,
) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]

    approve = await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(actor="operator"),
    )
    assert approve.status_code == 200, approve.text
    body = approve.json()
    assert body["execution_queued"] is True
    assert body["status"] == "APPROVED"

    # No SOAR call happened inside the HTTP request (durable queue only).
    assert harness.soar.submitted == []

    # The durable worker performs the one approved side effect.
    drained = await harness.container.response_dispatcher.drain_once()
    assert drained == 1
    assert len(harness.soar.submitted) == 1
    expected_key = f"response:tenant-a:{proposal['proposal_id']}"
    assert harness.soar.submitted[0]["submission_key"] == expected_key

    ws3 = await _workspace(harness, investigation_id)
    p = ws3["response"]["proposals"][0]
    assert p["status"] == "APPROVED"
    assert p["approval"]["decision"]["decision"] == "APPROVE"
    assert p["approval"]["decision"]["actor_subject_id"] == "operator"
    assert p["execution"] is not None
    assert p["execution"]["status"] == "SUCCEEDED"
    assert p["execution"]["finished_at"] is not None

    # A second drain is a no-op (published) → still exactly one SOAR submission.
    assert await harness.container.response_dispatcher.drain_once() == 0
    assert len(harness.soar.submitted) == 1


async def test_reject_produces_zero_executions(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]

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

    # Nothing was queued → draining the worker finds no work and SOAR is untouched.
    assert await harness.container.response_dispatcher.drain_once() == 0
    assert harness.soar.submitted == []

    ws3 = await _workspace(harness, investigation_id)
    p = ws3["response"]["proposals"][0]
    assert p["status"] == "REJECTED"
    assert p["approval"]["decision"]["decision"] == "REJECT"
    assert p["execution"] is None


async def test_stale_contract_approval_is_rejected(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]

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
    # No execution queued by a mismatched contract.
    assert await harness.container.response_dispatcher.drain_once() == 0
    assert harness.soar.submitted == []


async def test_decision_body_mismatch_with_route_is_rejected(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]

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
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    await _create_proposal(harness, investigation_id, evidence_ids)

    # A valid service credential asserting a different tenant cannot see or act on it.
    foreign = await harness.client.get(
        f"/api/v1/investigations/{investigation_id}/workspace",
        headers=_headers(tenant="tenant-b"),
    )
    assert foreign.status_code == 404
    assert investigation_id not in foreign.text


async def test_response_timeline_is_deterministic(harness: _Harness) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]
    await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(actor="operator"),
    )
    await harness.container.response_dispatcher.drain_once()

    first = (await _workspace(harness, investigation_id))["timeline"]
    second = (await _workspace(harness, investigation_id))["timeline"]
    # Same request twice → byte-identical ordering (no nondeterministic tiebreak).
    assert [(t["kind"], t["occurred_at"], t["ref_id"]) for t in first] == [
        (t["kind"], t["occurred_at"], t["ref_id"]) for t in second
    ]
    occurred = [t["occurred_at"] for t in first]
    assert occurred == sorted(occurred)


async def test_soar_definitive_failure_settles_execution_failed(
    harness: _Harness,
) -> None:
    investigation_id = await _completed_investigation(harness)
    ws = await _workspace(harness, investigation_id)
    evidence_ids = [e["evidence_id"] for e in ws["evidence"]][:1]
    proposal = await _create_proposal(harness, investigation_id, evidence_ids)
    ws2 = await _workspace(harness, investigation_id)
    approval = ws2["response"]["proposals"][0]["approval"]
    # Swap in a SOAR that definitively rejects the submission.
    harness.container.response_dispatcher = harness.container.response_outbox_dispatcher(
        soar=FakeSoar(
            raise_on_submit=ExternalServiceError(
                "bad playbook", service="hisiem", code="HTTP_422"
            )
        )
    )

    await harness.client.post(
        f"/api/v1/investigations/response-approvals/{approval['request_id']}/approve",
        json={
            "decision": "APPROVE",
            "expected_revision": proposal["revision"],
            "expected_content_hash": proposal["content_hash"],
        },
        headers=_headers(actor="operator"),
    )
    await harness.container.response_dispatcher.drain_once()

    ws3 = await _workspace(harness, investigation_id)
    execution = ws3["response"]["proposals"][0]["execution"]
    assert execution is not None
    assert execution["status"] == "FAILED"
    assert execution["safe_error_code"] == "HTTP_422"
