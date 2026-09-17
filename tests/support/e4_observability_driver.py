"""E4 XP-OBS-001 runtime driver: one REAL representative cross-plane lifecycle.

Run as a module in its OWN process:

```text
python -m tests.support.e4_observability_driver <database_url> <otlp_endpoint> <out.json>
```

It boots the real Copilot runtime (real container, real durable dispatcher, real
repositories, real observability bootstrap exporting to ``otlp_endpoint``), drives a
representative cross-plane lifecycle, and writes ONE bounded JSON document holding:

* the captured spans projected to BOUNDED facts (operation name, status category,
  parent/link presence, attribute KEYS only — E4 §21),
* the W3C traceparents the durable store persisted,
* the logical durable command identities,
* the PERSISTED business outcome (statuses and counts only — never telemetry).

The HISIEM **SOAR** plane is real (control-api + SOAR worker). The investigation's
upstream HISIEM read port is an in-process fake and the LLM is scripted, because
Elasticsearch is deliberately not part of this slice — the trace still crosses the
durable boundary, the persistence boundary, the agent runtime, and real HISIEM HTTP.

Test-support only: nothing here is production code, and nothing in ``src`` imports it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from uuid import UUID

import httpx
from asgi_lifespan import LifespanManager
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.domain.response.enums import ApprovalDecisionKind
from tests.support.e4_observability_fixture import project_spans

TENANT = "default"
ALERT = "e4-observability-alert-1"
PLAYBOOK_ID = "pb-a3b539a1-fa97-478e-8344-7a36d8e87816"
_TOKEN_ENV = "HISIEM_COPILOT_SERVICE_TOKEN"

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


def _script() -> dict[str, Any]:
    return {
        "decide": [
            {
                "tool_name": "hisiem.get_detection_rule",
                "arguments": {"rule_id": "ssh_brute_force"},
            },
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


def _attach_capture() -> Any:
    """Attach one in-process span processor to the installed tracer provider."""
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = trace_api.get_tracer_provider()
    assert isinstance(provider, TracerProvider), "the runtime installed no tracer provider"
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


def _settings(database_url: str, otlp_endpoint: str) -> Any:
    import os

    from hisiem_soc_copilot.config import Settings

    settings = Settings()
    settings.database.database_url = database_url
    settings.langgraph.database_url = database_url
    # The API boundary uses the development header provider (the same choice the
    # existing durable-chain integration test makes); the Copilot -> HISIEM SOAR
    # boundary keeps its own real credential below.
    settings.auth.trusted_context_provider = "header"
    settings.observability.otlp_endpoint = otlp_endpoint
    settings.observability.tracing_enabled = True
    settings.app.response_observe_interval_seconds = 0.0
    settings.app.enable_dispatcher = True
    settings.soar.base_url = "http://127.0.0.1:8080"
    settings.soar.bearer_token = os.environ.get(_TOKEN_ENV, "").strip() or "unset"
    return settings


async def _truncate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()


async def _persisted_traceparents(
    factory: async_sessionmaker[AsyncSession],
) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT traceparent FROM copilot.outbox_message "
                "WHERE traceparent IS NOT NULL ORDER BY created_at"
            )
        )
        return [str(row.traceparent) for row in rows]


async def _run(database_url: str, otlp_endpoint: str, out_path: str) -> dict[str, Any]:
    from tests.fixtures.hisiem_fake import FakeHisiem
    from tests.fixtures.ssh_models import GroundedSshModel

    engine = create_async_engine(database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)

    settings = _settings(database_url, otlp_endpoint)
    app = create_app(settings)
    captured: Any = None
    async with LifespanManager(app):
            # The application lifespan installed the REAL telemetry pipeline
            # (production bootstrap, real OTLP export to the configured
            # collector). E4 attaches one extra in-process processor so the
            # emitted spans can be projected into bounded measurement facts;
            # nothing about the production pipeline is replaced.
            exporter = _attach_capture()
            container = app.state.container
            # The investigation's upstream HISIEM READ port is scripted (the same
            # substitution the repository's own durable-chain integration test
            # makes: real DB rows, real dispatcher, real graph, scripted reads).
            # The Copilot -> HISIEM SOAR boundary below stays REAL.
            hisiem = FakeHisiem(alert_id=ALERT)
            container.hisiem_adapter = hisiem  # type: ignore[assignment]
            model = GroundedSshModel(script=_script())
            container.dispatcher = container.outbox_dispatcher(hisiem=hisiem, model=model)

            # --- Agent plane: API -> durable dispatch -> graph -> tools -----
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                created = await client.post(
                    "/api/v1/investigations",
                    json={
                        "source_alert_ref": {
                            "provider": "hisiem",
                            "resource_type": "alert",
                            "address_id": ALERT,
                        }
                    },
                    headers={"X-Tenant-ID": TENANT, "X-Actor-Subject": "analyst"},
                )
                assert created.status_code == 201, created.text
                investigation_id = created.json()["investigation_id"]
                assert await container.dispatcher.drain_once() >= 1

                # --- Response plane: policy -> approval -> durable SOAR ------
                proposal = await container.response_command_handler().create_response_proposal(
                    CreateResponseProposal(
                        tenant_id=TENANT,
                        investigation_id=UUID(investigation_id),
                        action_key="START_SOAR_PLAYBOOK",
                        evidence_ids=(
                            str(
                                await _first_evidence(container, investigation_id)
                            ),
                        ),
                        parameters={"playbook_id": PLAYBOOK_ID},
                        reason="E4 representative lifecycle",
                        initiated_by_subject="analyst",
                    )
                )
                request = await _approval_request(container, proposal.id)
                assert request is not None
                await container.response_command_handler().decide_response_approval(
                    DecideResponseApproval(
                        tenant_id=TENANT,
                        approval_request_id=request.id,
                        decision=ApprovalDecisionKind.APPROVE,
                        expected_revision=proposal.content_revision,
                        expected_content_hash=proposal.content_hash,
                        initiated_by_subject="operator",
                    )
                )
                soar = container.soar()
                submit = container.response_submit_outbox_dispatcher(soar=soar)
                assert await submit.drain_once() >= 1
                observe = container.response_observe_outbox_dispatcher(soar=soar)
                # Drive reconciliation to a TERMINAL provider state before measuring, so the
                # business outcome this run records does not depend on how many reconcile
                # rounds happened to fit in the budget (that would make the outage comparison
                # depend on timing rather than on telemetry availability).
                for _ in range(40):
                    await _make_claimable(factory, "response.execution.observe")
                    await observe.drain_once()
                    execution = await _execution(container, proposal.id)
                    if execution is not None and execution.status in {"SUCCEEDED", "FAILED"}:
                        break
                    await asyncio.sleep(0.25)
                await submit.stop()
                await observe.stop()
                await soar.close()

            captured = project_spans(exporter.get_finished_spans())
            outcome = await _business_outcome(container, investigation_id, proposal.id)
            traceparents = await _persisted_traceparents(factory)
            command_ids = await _logical_command_ids(factory, proposal.id)

    await engine.dispose()
    document = {
        "spans": [
            {
                "operation": span.operation,
                "status_category": span.status_category,
                "parent_present": span.parent_present,
                "link_count": span.link_count,
                "trace_id": span.trace_id,
                "attribute_keys": list(span.attribute_keys),
            }
            for span in (captured or ())
        ],
        "traceparents": traceparents,
        "logical_command_ids": command_ids,
        "business_outcome": outcome,
    }
    # Blocking on purpose: this runs once at the end of a standalone driver process.
    with open(out_path, "w", encoding="utf-8") as handle:  # noqa: ASYNC230
        json.dump(document, handle)
    return document


async def _first_evidence(container: Any, investigation_id: str) -> Any:
    uow = container.unit_of_work()
    try:
        rows = await uow.evidence.list_by_investigation(
            tenant_id=TENANT, investigation_id=UUID(investigation_id)
        )
    finally:
        await uow.close()
    assert rows, "the representative lifecycle produced no Evidence"
    return rows[0].id


async def _approval_request(container: Any, proposal_id: Any) -> Any:
    uow = container.unit_of_work()
    try:
        return await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
    finally:
        await uow.close()


async def _execution(container: Any, proposal_id: Any) -> Any:
    uow = container.unit_of_work()
    try:
        return await uow.response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
    finally:
        await uow.close()


async def _make_claimable(
    factory: async_sessionmaker[AsyncSession], destination: str
) -> None:
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE copilot.outbox_message SET "
                "available_at = now() - interval '1 hour', status = 'PENDING', "
                "locked_at = NULL, locked_by = NULL, lease_token = NULL "
                "WHERE destination = :d AND status <> 'PUBLISHED'"
            ),
            {"d": destination},
        )
        await session.commit()


async def _logical_command_ids(
    factory: async_sessionmaker[AsyncSession], proposal_id: Any
) -> list[str]:
    async with factory() as session:
        rows = await session.execute(
            text(
                "SELECT payload FROM copilot.domain_event "
                "WHERE aggregate_id = :aid AND event_type = 'response_execution_queued'"
            ),
            {"aid": str(proposal_id)},
        )
        return [
            str((row.payload or {}).get("submission_key"))
            for row in rows
            if (row.payload or {}).get("submission_key")
        ]


async def _business_outcome(
    container: Any, investigation_id: str, proposal_id: Any
) -> dict[str, Any]:
    """The PERSISTED business outcome — statuses and counts only, never telemetry."""
    uow = container.unit_of_work()
    try:
        investigation = await uow.investigations.get(
            tenant_id=TENANT, investigation_id=UUID(investigation_id)
        )
        result = await uow.results.get_by_investigation(
            tenant_id=TENANT, investigation_id=UUID(investigation_id)
        )
        evidence = await uow.evidence.list_by_investigation(
            tenant_id=TENANT, investigation_id=UUID(investigation_id)
        )
        findings = await uow.findings.list_by_investigation(
            tenant_id=TENANT, investigation_id=UUID(investigation_id)
        )
        proposal = await uow.response_proposals.get(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        submission = await uow.response_submissions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        execution = await uow.response_executions.get_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        request = await uow.response_approvals.get_request_by_proposal(
            tenant_id=TENANT, proposal_id=proposal_id
        )
        decision = (
            await uow.response_approvals.get_decision(
                tenant_id=TENANT, approval_request_id=request.id
            )
            if request is not None
            else None
        )
    finally:
        await uow.close()
    return {
        "investigation_status": investigation.status.value if investigation else None,
        "verdict_disposition": result.verdict.disposition.value if result else None,
        "evidence_count": len(evidence),
        "finding_count": len(findings),
        "proposal_status": proposal.status.value if proposal else None,
        "policy_decision": (
            proposal.policy_decision.value
            if proposal is not None and proposal.policy_decision is not None
            else None
        ),
        "approval_decision": decision.decision if decision else None,
        "submission_status": submission.status if submission else None,
        "execution_provider": execution.provider if execution else None,
        "execution_status": execution.status if execution else None,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: -m tests.support.e4_observability_driver "
            "<db_url> <otlp_endpoint> <out.json>",
            file=sys.stderr,
        )
        return 2
    if sys.platform == "win32":
        import selectors

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
    document = asyncio.run(_run(argv[1], argv[2], argv[3]))
    print(
        json.dumps(
            {
                "span_count": len(document["spans"]),
                "traceparent_count": len(document["traceparents"]),
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
