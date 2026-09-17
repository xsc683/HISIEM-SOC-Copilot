"""E3 XP-REL-005 runtime driver: one REAL durable lifecycle, one business outcome.

Run as a module in its OWN process:

```text
python -m tests.support.e3_runtime_telemetry_driver <database_url> <otlp_endpoint>
```

It configures the real observability SDK against ``otlp_endpoint``, runs the real
durable response lifecycle (real repositories, real dispatcher, real SUBMIT runner)
against ``database_url``, and prints ONE JSON line holding the persisted business
outcome of that run.

XP-REL-005 needs two of these runs — one with the telemetry backend reachable and
one with it down — in separate processes, because the OpenTelemetry SDK's tracer
provider is process-global and is built once. Comparing their business outcomes is
what the ``TELEMETRY_CHANGED_BUSINESS_STATE`` gate consumes.

Test-support only: nothing here is production code, and nothing in ``src`` imports it.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from hisiem_soc_copilot.api.app import create_app
from hisiem_soc_copilot.application.commands.response import (
    CreateResponseProposal,
    DecideResponseApproval,
)
from hisiem_soc_copilot.application.ports.soar import SoarExecutionResult
from hisiem_soc_copilot.config import Settings
from hisiem_soc_copilot.domain.investigation.aggregate import Investigation
from hisiem_soc_copilot.domain.investigation.entities import (
    Evidence,
    EvidenceSource,
    InvestigationResult,
    Verdict,
)
from hisiem_soc_copilot.domain.investigation.enums import (
    EvidenceSourceType,
    VerdictDisposition,
)
from hisiem_soc_copilot.domain.investigation.value_objects import (
    ActorRef,
    BudgetLimits,
    ExternalResourceRef,
)
from hisiem_soc_copilot.domain.response.enums import ApprovalDecisionKind
from hisiem_soc_copilot.evaluation_harness.cross_plane_authority import business_outcome
from tests.fixtures.fakes import FakeSoar

TENANT = "tenant-a"
ALERT = "e3-telemetry-alert-1"
PLAYBOOK_ID = "11111111-2222-3333-4444-555555555555"
_TOKEN_ENV = "HISIEM_COPILOT_SERVICE_TOKEN"

_TRUNCATE = (
    "response_submission",
    "response_execution_ref",
    "approval_decision",
    "approval_request",
    "response_proposal_evidence",
    "response_proposal_target",
    "response_proposal",
    "outbox_message",
    "domain_event",
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


def _settings(database_url: str, otlp_endpoint: str) -> Settings:
    settings = Settings()
    settings.database.database_url = database_url
    settings.langgraph.database_url = database_url
    settings.auth.trusted_context_provider = "hisiem_bearer"
    settings.auth.hisiem_service_token_env = _TOKEN_ENV
    settings.observability.otlp_endpoint = otlp_endpoint
    settings.observability.tracing_enabled = True
    settings.app.response_observe_interval_seconds = 0.0
    return settings


async def _truncate(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory() as session:
        await session.execute(
            text(f"TRUNCATE copilot.{', copilot.'.join(_TRUNCATE)} RESTART IDENTITY CASCADE")
        )
        await session.commit()


async def _run(database_url: str, otlp_endpoint: str) -> dict[str, Any]:
    from hisiem_soc_copilot.domain.shared.identifiers import utc_now

    settings = _settings(database_url, otlp_endpoint)
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await _truncate(factory)

    now = utc_now()
    app = create_app(settings)
    # ``create_app`` installs the real telemetry pipeline (setup_telemetry) when the
    # lifespan starts, against settings.observability.otlp_endpoint.
    from asgi_lifespan import LifespanManager

    async with LifespanManager(app):
        container = app.state.container
        investigation_id = uuid4()
        uow = container.unit_of_work()
        try:
            investigation = Investigation.create(
                id=investigation_id,
                tenant_id=TENANT,
                source_alert_ref=ExternalResourceRef(
                    provider="hisiem",
                    resource_type="alert",
                    address_id=ALERT,
                    business_id="AL-1",
                ),
                initiated_by=ActorRef(subject_id="analyst", tenant_id=TENANT),
                budget_limits=BudgetLimits(),
                now=now,
            )
            investigation.start(
                actor=ActorRef(subject_id="analyst", tenant_id=TENANT), now=now
            )
            investigation.complete_without_response()
            await uow.investigations.add(investigation)
            await uow.commit()

            evidence = Evidence(
                id=uuid4(),
                investigation_id=investigation_id,
                source=EvidenceSource(
                    type=EvidenceSourceType.HISIEM_LOG_SEARCH,
                    provider="hisiem",
                    operation="log_search",
                ),
                collected_at=now,
                observation={"summary": "sudo auth failure from 203.0.113.9"},
            )
            await uow.evidence.add(evidence)
            result = InvestigationResult(
                id=uuid4(),
                investigation_id=investigation_id,
                verdict=Verdict(
                    disposition=VerdictDisposition.MALICIOUS,
                    summary="confirmed",
                    confidence=0.9,
                ),
                finding_ids=[],
                created_at=now,
            )
            await uow.results.add(result)
            await uow.commit()
        finally:
            await uow.close()

        handler = container.response_command_handler()
        proposal = await handler.create_response_proposal(
            CreateResponseProposal(
                tenant_id=TENANT,
                investigation_id=investigation_id,
                action_key="START_SOAR_PLAYBOOK",
                evidence_ids=(str(evidence.id),),
                parameters={"playbook_id": PLAYBOOK_ID},
                reason="contain the intrusion",
                initiated_by_subject="analyst",
            )
        )
        uow = container.unit_of_work()
        try:
            request = await uow.response_approvals.get_request_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            )
        finally:
            await uow.close()
        assert request is not None
        await handler.decide_response_approval(
            DecideResponseApproval(
                tenant_id=TENANT,
                approval_request_id=request.id,
                decision=ApprovalDecisionKind.APPROVE,
                expected_revision=proposal.content_revision,
                expected_content_hash=proposal.content_hash,
                initiated_by_subject="operator",
            )
        )

        soar = FakeSoar(
            submit_result=SoarExecutionResult(
                execution_id="hisiem-exec-telemetry", status="RUNNING"
            ),
            status_sequence=["RUNNING", "SUCCEEDED"],
        )
        submit = container.response_submit_outbox_dispatcher(soar=soar)
        await submit.drain_once()
        observe = container.response_observe_outbox_dispatcher(soar=soar)
        for _ in range(4):
            async with factory() as session:
                await session.execute(
                    text(
                        "UPDATE copilot.outbox_message SET "
                        "available_at = now() - interval '1 hour', status = 'PENDING', "
                        "locked_at = NULL, locked_by = NULL, lease_token = NULL "
                        "WHERE destination = 'response.execution.observe' "
                        "AND status <> 'PUBLISHED'"
                    )
                )
                await session.commit()
            if await observe.drain_once() == 0:
                break
            state_uow = container.unit_of_work()
            try:
                execution = await state_uow.response_executions.get_by_proposal(
                    tenant_id=TENANT, proposal_id=proposal.id
                )
            finally:
                await state_uow.close()
            if execution is not None and execution.status == "SUCCEEDED":
                break
        await submit.stop()
        await observe.stop()

        uow = container.unit_of_work()
        try:
            final_proposal = await uow.response_proposals.get(
                tenant_id=TENANT, proposal_id=proposal.id
            )
            submission = await uow.response_submissions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            )
            execution = await uow.response_executions.get_by_proposal(
                tenant_id=TENANT, proposal_id=proposal.id
            )
            decision = await uow.response_approvals.get_decision(
                tenant_id=TENANT, approval_request_id=request.id
            )
        finally:
            await uow.close()

    await engine.dispose()
    return business_outcome(
        proposal=final_proposal,
        decision=decision,
        submission=submission,
        execution=execution,
        events=(),
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(
            "usage: -m tests.support.e3_runtime_telemetry_driver <db_url> <otlp>",
            file=sys.stderr,
        )
        return 2
    if sys.platform == "win32":
        # psycopg async cannot run on the Windows default ProactorEventLoop; the
        # pytest conftest sets the same policy for in-process tests.
        import selectors

        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())  # type: ignore[attr-defined]
        asyncio.set_event_loop(asyncio.SelectorEventLoop(selectors.SelectSelector()))
    outcome = asyncio.run(_run(argv[1], argv[2]))
    print(json.dumps({"business_outcome": outcome}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
