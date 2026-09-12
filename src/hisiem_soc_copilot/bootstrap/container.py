"""Composition Root container.

Only this layer knows every concrete implementation at once (python-package-
boundary.md §21). It wires domain ports to infrastructure adapters and exposes the
application services the API routers depend on. Async resources (engines,
sessions, HTTP clients) are owned by the lifespan via open()/close().
"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.requests import Request

from ..application.handlers.attack_import import AttackImportHandler
from ..application.handlers.investigation import InvestigationCommandHandler
from ..application.handlers.knowledge import (
    KnowledgeIngestionHandler,
    KnowledgeIngestionLimits,
)
from ..application.handlers.response import ResponseCommandHandler
from ..application.handlers.workflow import InvestigationWorkflowHandler
from ..application.ports.attack import AttackBundleParser
from ..application.ports.clock import SystemClock
from ..application.ports.durable import OutboxStore
from ..application.ports.embedding import EmbeddingProvider
from ..application.ports.hisiem import HisiemPort
from ..application.ports.model_provider import ModelProvider
from ..application.ports.soar import SoarPort
from ..application.ports.trust import (
    ServiceAuthenticationError,
    TrustedContextProvider,
)
from ..application.ports.unit_of_work import UnitOfWork
from ..application.services.investigation_service import InvestigationReadService
from ..application.services.knowledge_retrieval import (
    HybridRetrievalConfig,
    KnowledgeCitationResolver,
    KnowledgeRetrievalService,
)
from ..application.services.workspace_service import InvestigationWorkspaceService
from ..config import Settings
from ..domain.investigation.value_objects import BudgetLimits
from ..infrastructure.auth.header_provider import HeaderTrustedContextProvider
from ..infrastructure.auth.hisiem_service_provider import (
    HisiemServiceTrustedContextProvider,
)
from ..infrastructure.durable.dispatcher import AsyncOutboxDispatcher
from ..infrastructure.durable.investigation_runner import (
    AsyncInvestigationGraphRunner,
)
from ..infrastructure.durable.response_runner import (
    ResponseObserveRunner,
    ResponseSubmitExhaustionHandler,
    ResponseSubmitRunner,
)
from ..infrastructure.embedding.openai_compatible import OpenAICompatibleEmbeddingAdapter
from ..infrastructure.hisiem.adapter import HisiemHttpAdapter
from ..infrastructure.knowledge.attack_source import MitreStixAttackSource
from ..infrastructure.knowledge.chunker_port import StructureAwareChunker
from ..infrastructure.knowledge.diagnostics import DiagnosticsReport, collect_diagnostics
from ..infrastructure.persistence.database import build_engine, build_session_factory
from ..infrastructure.persistence.repositories.durable import SqlAlchemyOutboxStore
from ..infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork
from ..infrastructure.soar.adapter import HisiemSoarAdapter


class Container:
    """Holds configured service instances for the FastAPI process."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.copilot_engine: AsyncEngine | None = None
        self.copilot_sessions: async_sessionmaker[AsyncSession] | None = None
        self.hisiem_adapter: HisiemHttpAdapter | None = None
        self.soar_adapter: HisiemSoarAdapter | None = None
        self.embedding_adapter: EmbeddingProvider | None = None
        self.dispatcher: AsyncOutboxDispatcher | None = None
        self.response_submit_dispatcher: AsyncOutboxDispatcher | None = None
        self.response_observe_dispatcher: AsyncOutboxDispatcher | None = None
        # Read-only factories below hand out a UnitOfWork that no command will
        # ever close (there is no request scope to close it). The connection they
        # check out is returned here instead, so the pool is not left holding an
        # in-transaction connection that is only released when the garbage
        # collector drops it -- which is exactly the case SQLAlchemy warns about
        # and cannot terminate safely.
        self._read_scoped_unit_of_works: list[UnitOfWork] = []

    # --- async resource lifecycle (called from lifespan) ---
    async def open(self) -> None:
        self.copilot_engine = build_engine(self.settings.database)
        self.copilot_sessions = build_session_factory(self.copilot_engine)
        self.hisiem_adapter = HisiemHttpAdapter(settings=self.settings.hisiem)
        # Fail closed at startup: if the HISIEM service boundary is selected but
        # its credential is not configured, refuse to start rather than silently
        # accepting (or rejecting) every request.
        if self.settings.auth.trusted_context_provider == "hisiem_bearer":
            self._resolve_hisiem_service_token()
        if self.settings.app.enable_dispatcher:
            self.dispatcher = self.outbox_dispatcher()
            await self.dispatcher.start()
        if self.settings.app.enable_response_worker:
            # The SOAR adapter fails closed (blank bearer → ExternalServiceError) so
            # the response worker can never run without a service credential.
            self.soar_adapter = self.soar()
            self.response_submit_dispatcher = self.response_submit_outbox_dispatcher()
            await self.response_submit_dispatcher.start()
            self.response_observe_dispatcher = self.response_observe_outbox_dispatcher()
            await self.response_observe_dispatcher.start()

    async def close(self) -> None:
        if self.response_observe_dispatcher is not None:
            await self.response_observe_dispatcher.stop()
        if self.response_submit_dispatcher is not None:
            await self.response_submit_dispatcher.stop()
        if self.dispatcher is not None:
            await self.dispatcher.stop()
        if self.soar_adapter is not None:
            await self.soar_adapter.close()
        if isinstance(self.embedding_adapter, OpenAICompatibleEmbeddingAdapter):
            await self.embedding_adapter.aclose()
        if self.hisiem_adapter is not None:
            await self.hisiem_adapter.close()
        # Return every read-scoped connection to the pool BEFORE the engine is
        # disposed; disposing first would leave these sessions pointing at a dead
        # pool.
        for uow in self._read_scoped_unit_of_works:
            await uow.close()
        self._read_scoped_unit_of_works.clear()
        if self.copilot_engine is not None:
            await self.copilot_engine.dispose()

    # --- service graph (valid only after open()) ---
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        return self.copilot_sessions

    def unit_of_work_factory(self) -> Callable[[], UnitOfWork]:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        sessions = self.copilot_sessions

        def _factory() -> UnitOfWork:
            return SqlAlchemyUnitOfWork(sessions)

        return _factory

    def unit_of_work(self) -> UnitOfWork:
        return self.unit_of_work_factory()()

    def outbox_store(self) -> OutboxStore:
        store: OutboxStore = SqlAlchemyOutboxStore(self.session_factory())
        return store

    def model_provider(self) -> ModelProvider:
        """The SINGLE configuration-based provider construction path (E1-C2 §3).

        Provider selection lives ONLY here (config/bootstrap/container): the graph
        never branches on the provider name. ``scripted`` → the deterministic fake;
        ``openai_compatible`` → the real Command Code adapter. The adapter's
        constructor reads the API key from ``llm.api_key_env`` and raises
        :class:`ModelConfigurationError` when it is absent — so a real provider can
        never be silently built without credentials. Callers that need ONE provider
        instance shared by the graph AND telemetry obtain it here and inject it.
        """
        from ..infrastructure.llm.openai_compatible import OpenAICompatibleModelProvider
        from ..infrastructure.llm.scripted import ScriptedModelProvider

        llm = self.settings.llm
        if llm.provider == "openai_compatible":
            return OpenAICompatibleModelProvider(
                base_url=llm.base_url,
                model=llm.model,
                api_key_env=llm.api_key_env,
                timeout_seconds=llm.timeout_seconds,
                max_retries=llm.max_retries,
                zdr=llm.zdr,
                structured_output_mode=llm.structured_output_mode,
            )
        return ScriptedModelProvider()

    def investigation_runner(
        self,
        *,
        hisiem: HisiemPort | None = None,
        model: ModelProvider | None = None,
    ) -> AsyncInvestigationGraphRunner:
        """Build the durable runner that executes one investigation's graph.

        ``hisiem`` defaults to the real HISIEM HTTP adapter; ``model`` defaults to
        :meth:`model_provider` (the single configuration-based provider selection).
        Tests inject fakes to run the graph without a live HISIEM or model API.
        """
        from ..agent.evidence.normalizer import EvidenceNormalizer
        from ..agent.graph.builder import build_investigation_graph
        from ..agent.graph.runtime import GraphRuntime
        from ..agent.tools.executor import ToolExecutor
        from ..agent.tools.registry import ToolRegistry

        uow_factory = self.unit_of_work_factory()
        workflow_handler = self.investigation_workflow_handler()
        hisiem_adapter = hisiem if hisiem is not None else self.hisiem()
        model_provider = model if model is not None else self.model_provider()

        def _runtime(tenant_id: str) -> GraphRuntime:
            return GraphRuntime(
                uow_factory=uow_factory,
                workflow_handler=workflow_handler,
                model=model_provider,
                executor=ToolExecutor(hisiem=hisiem_adapter),
                normalizer=EvidenceNormalizer(),
                registry=ToolRegistry(),
                hisiem=hisiem_adapter,
                tenant_id=tenant_id,
            )

        return AsyncInvestigationGraphRunner(
            unit_of_work_factory=uow_factory,
            workflow_handler=workflow_handler,
            runtime_factory=_runtime,
            compile_graph=build_investigation_graph,
            checkpoint_settings=self.settings.langgraph,
        )

    def outbox_dispatcher(
        self,
        *,
        hisiem: HisiemPort | None = None,
        model: ModelProvider | None = None,
    ) -> AsyncOutboxDispatcher:
        """Build the durable outbox dispatcher for the investigation runner."""
        from ..infrastructure.durable.dispatcher import SqlAlchemyOutboxResolver

        resolver = SqlAlchemyOutboxResolver(self.session_factory())
        runner = self.investigation_runner(hisiem=hisiem, model=model)
        return AsyncOutboxDispatcher(
            outbox_store=self.outbox_store(),
            resolver=resolver,
            runner=runner,
            worker_name="copilot-dispatcher",
        )

    def soar(self) -> HisiemSoarAdapter:
        """Build the HISIEM SOAR adapter (fails closed on a blank credential)."""
        return HisiemSoarAdapter(settings=self.settings.soar)

    def response_command_handler(self) -> ResponseCommandHandler:
        if self.copilot_sessions is None:
            raise RuntimeError("container must be opened before use")
        return ResponseCommandHandler(
            unit_of_work_factory=self.unit_of_work_factory(),
        )

    def response_submit_runner(
        self,
        *,
        soar: SoarPort | None = None,
        observe_delay_seconds: float | None = None,
    ) -> ResponseSubmitRunner:
        return ResponseSubmitRunner(
            unit_of_work_factory=self.unit_of_work_factory(),
            soar=self._resolve_soar(soar),
            observe_delay_seconds=self._observe_delay(observe_delay_seconds),
        )

    def response_observe_runner(
        self,
        *,
        soar: SoarPort | None = None,
        observe_delay_seconds: float | None = None,
    ) -> ResponseObserveRunner:
        return ResponseObserveRunner(
            unit_of_work_factory=self.unit_of_work_factory(),
            soar=self._resolve_soar(soar),
            observe_delay_seconds=self._observe_delay(observe_delay_seconds),
        )

    def _observe_delay(self, override: float | None) -> float:
        if override is not None:
            return override
        return self.settings.app.response_observe_interval_seconds

    def _resolve_soar(self, soar: SoarPort | None) -> SoarPort:
        return soar if soar is not None else (self.soar_adapter or self.soar())

    def response_submit_outbox_dispatcher(
        self,
        *,
        soar: SoarPort | None = None,
        observe_delay_seconds: float | None = None,
    ) -> AsyncOutboxDispatcher:
        """Build the durable dispatcher for the response SUBMIT destination."""
        from ..infrastructure.durable.dispatcher import (
            RESPONSE_SUBMIT_DESTINATION,
            SqlAlchemyOutboxResolver,
        )

        return AsyncOutboxDispatcher(
            outbox_store=self.outbox_store(),
            resolver=SqlAlchemyOutboxResolver(self.session_factory()),
            runner=self.response_submit_runner(
                soar=soar, observe_delay_seconds=observe_delay_seconds
            ),
            worker_name="copilot-response-submit-dispatcher",
            destination=RESPONSE_SUBMIT_DESTINATION,
            # Destination-specific: the generic dispatcher must not have to know
            # what an exhausted submit budget MEANS, but the business fact has to
            # be persisted before the delivery is dead-lettered.
            exhaustion=ResponseSubmitExhaustionHandler(
                unit_of_work_factory=self.unit_of_work_factory()
            ),
        )

    def response_observe_outbox_dispatcher(
        self,
        *,
        soar: SoarPort | None = None,
        observe_delay_seconds: float | None = None,
    ) -> AsyncOutboxDispatcher:
        """Build the durable dispatcher for the response OBSERVE destination."""
        from ..infrastructure.durable.dispatcher import (
            RESPONSE_OBSERVE_DESTINATION,
            SqlAlchemyOutboxResolver,
        )

        return AsyncOutboxDispatcher(
            outbox_store=self.outbox_store(),
            resolver=SqlAlchemyOutboxResolver(self.session_factory()),
            runner=self.response_observe_runner(
                soar=soar, observe_delay_seconds=observe_delay_seconds
            ),
            worker_name="copilot-response-observe-dispatcher",
            destination=RESPONSE_OBSERVE_DESTINATION,
        )

    def _resolve_hisiem_service_token(self) -> str:
        """Resolve the HISIEM→Copilot service credential from the environment.

        The secret is never a config default: only the NAME of the environment
        variable is configured. A missing/blank value is a configuration defect
        and fails closed (raises) — it never degrades to ``header``/``none`` or an
        anonymous/system identity.
        """
        env_name = self.settings.auth.hisiem_service_token_env
        token = os.environ.get(env_name) or ""
        if not token.strip():
            raise ServiceAuthenticationError(
                f"service credential environment variable {env_name} is not set; "
                "refusing to serve the HISIEM service boundary"
            )
        return token

    def trusted_context_provider(self, request: Request) -> TrustedContextProvider | None:
        """Build a TrustedContextProvider for the request from configuration.

        Returns None when no provider is configured (``none`` default) so the API
        fails closed. ``hisiem_bearer`` is the integrated/production boundary: it
        authenticates HISIEM first, then trusts the tenant/actor it asserts.
        ``header`` is a development/test adapter only.
        """
        mode = self.settings.auth.trusted_context_provider
        if mode == "header":
            return HeaderTrustedContextProvider(request)
        if mode == "hisiem_bearer":
            return HisiemServiceTrustedContextProvider(
                request, expected_token=self._resolve_hisiem_service_token()
            )
        if mode == "none":
            return None
        raise RuntimeError(f"unknown trusted-context provider: {mode}")

    def investigation_command_handler(self) -> InvestigationCommandHandler:
        if self.hisiem_adapter is None:
            raise RuntimeError("container must be opened before use")
        return InvestigationCommandHandler(
            unit_of_work_factory=self.unit_of_work_factory(),
            hisiem=self.hisiem_adapter,
            budget_limits=_budget_limits(self.settings),
        )

    def investigation_workflow_handler(self) -> InvestigationWorkflowHandler:
        return InvestigationWorkflowHandler(
            unit_of_work_factory=self.unit_of_work_factory()
        )

    def hisiem(self) -> HisiemHttpAdapter:
        if self.hisiem_adapter is None:
            raise RuntimeError("container must be opened before use")
        return self.hisiem_adapter

    # --- knowledge subsystem (P3-A; NOT reachable from the Agent) -----------
    def embedding_provider(self) -> EmbeddingProvider | None:
        """The configured embedding provider, or ``None`` when unconfigured.

        ``None`` is a first-class outcome, not an error: with no embedding
        provider configured, lexical retrieval still works and vector/hybrid
        retrieval reports itself unavailable instead of inventing vectors
        (brief section 87). The secret is read from the environment variable
        NAMED by config and never stored on the container.
        """
        if self.embedding_adapter is not None:
            return self.embedding_adapter
        settings = self.settings.embedding
        if not settings.is_configured:
            return None
        api_key = os.environ.get(settings.api_key_env, "")
        self.embedding_adapter = OpenAICompatibleEmbeddingAdapter(
            base_url=settings.base_url,
            api_key=api_key,
            model_id=settings.model,
            dimension=settings.dimension,
            normalization=settings.normalization,
            distance_metric=settings.distance_metric,
            profile_version=settings.profile_version,
            timeout_seconds=settings.timeout_seconds,
            max_retries=settings.max_retries,
        )
        return self.embedding_adapter

    def knowledge_chunker(self) -> StructureAwareChunker:
        """The FROZEN structure-aware chunker, bound to the configured bounds."""
        k = self.settings.knowledge
        return StructureAwareChunker(
            profile=k.chunker_profile(),
            max_chunks=k.max_chunks_per_document,
            max_chunk_chars=k.max_chunk_chars,
        )

    def knowledge_ingestion_handler(
        self, *, embedding_provider: EmbeddingProvider | None = None
    ) -> KnowledgeIngestionHandler:
        """Build the knowledge ingestion/retirement handler.

        There is NO silent fallback to a test provider: with no embedding
        configuration the handler is built without one, and ``ingest`` refuses.
        That refusal lives in the use case rather than here because retirement
        does not embed anything -- a misconfigured embedding channel must not be
        able to stop an operator from retiring a document (section 87).
        """
        provider = embedding_provider or self.embedding_provider()
        k = self.settings.knowledge
        return KnowledgeIngestionHandler(
            unit_of_work_factory=self.unit_of_work_factory(),
            embedding_provider=provider,
            chunker=self.knowledge_chunker(),
            clock=SystemClock(),
            limits=KnowledgeIngestionLimits(
                max_document_bytes=k.max_document_bytes,
                max_normalized_chars=k.max_normalized_chars,
                max_chunks_per_document=k.max_chunks_per_document,
                max_chunk_chars=k.max_chunk_chars,
            ),
        )

    def knowledge_retrieval_config(self) -> HybridRetrievalConfig:
        k = self.settings.knowledge
        return HybridRetrievalConfig(
            lexical_candidate_limit=k.lexical_candidate_limit,
            vector_candidate_limit=k.vector_candidate_limit,
            rrf_k=k.rrf_k,
            max_chunks_per_document=k.max_hits_per_document,
            chunker_version=self.knowledge_chunker().chunker_version,
        )

    def knowledge_retrieval_service(
        self, *, embedding_provider: EmbeddingProvider | None = None
    ) -> KnowledgeRetrievalService:
        """Build the retrieval service on its OWN short-lived UnitOfWork.

        Retrieval is a read path: it never shares the request-scoped transaction
        of a write, so a slow search can never hold a write transaction open.

        ``embedding_provider`` overrides the configured provider. Only the CLI's
        explicitly-labelled dev fixture uses it; the default is the deployment's
        real configuration, so a caller cannot drift onto a test double by
        forgetting an argument.
        """
        uow = self._read_scoped_unit_of_work()
        return KnowledgeRetrievalService(
            chunks=uow.knowledge_chunks,
            embedding_profiles=uow.embedding_profiles,
            embedding_provider=(
                embedding_provider if embedding_provider is not None else self.embedding_provider()
            ),
            clock=SystemClock(),
            config=self.knowledge_retrieval_config(),
        )

    def knowledge_citation_resolver(self) -> KnowledgeCitationResolver:
        return KnowledgeCitationResolver(
            chunks=self._read_scoped_unit_of_work().knowledge_chunks
        )

    def _read_scoped_unit_of_work(self) -> UnitOfWork:
        """A UnitOfWork for a read path, closed by :meth:`close` rather than by us.

        Resolution is a read: it must never join a command's transaction, and the
        caller (the CLI, the evaluation driver) has no scope of its own in which
        to close it -- so the container owns the lifetime instead of leaving the
        checked-out connection to be dropped by the garbage collector.
        """
        uow = self.unit_of_work()
        self._read_scoped_unit_of_works.append(uow)
        return uow

    def attack_bundle_parser(self) -> AttackBundleParser:
        """The local STIX reader behind the application's ATT&CK port."""
        return MitreStixAttackSource()

    def attack_import_handler(
        self, *, embedding_provider: EmbeddingProvider | None = None
    ) -> AttackImportHandler:
        return AttackImportHandler(
            unit_of_work_factory=self.unit_of_work_factory(),
            parser=self.attack_bundle_parser(),
            ingestion=self.knowledge_ingestion_handler(
                embedding_provider=embedding_provider
            ),
            clock=SystemClock(),
        )

    def embedding_status(self) -> tuple[bool, str]:
        """Whether the embedding channel is usable, plus a SAFE explanation.

        Reports presence, never a value: the API key is checked for being
        non-empty and is never echoed, logged, or included in the detail string
        (sections 18/31/69).
        """
        settings = self.settings.embedding
        if settings.provider == "unconfigured":
            return False, (
                "no embedding provider configured (EMBEDDING_PROVIDER=unconfigured): "
                "lexical retrieval only"
            )
        missing = [
            name
            for name, value in (
                ("EMBEDDING_BASE_URL", settings.base_url),
                ("EMBEDDING_MODEL", settings.model),
            )
            if not value.strip()
        ]
        if settings.dimension <= 0:
            missing.append("EMBEDDING_DIMENSION")
        if not os.environ.get(settings.api_key_env, "").strip():
            missing.append(settings.api_key_env)
        if missing:
            return False, "embedding configuration is incomplete: missing " + ", ".join(
                missing
            )
        return True, (
            f"{settings.provider} model={settings.model} dim={settings.dimension} "
            f"{settings.distance_metric} (key read from {settings.api_key_env})"
        )

    async def knowledge_diagnostics(self) -> DiagnosticsReport:
        """Run the read-only knowledge deployment checks (brief section 31)."""
        if self.copilot_engine is None:
            raise RuntimeError("container must be opened before use")
        configured, detail = self.embedding_status()
        return await collect_diagnostics(
            engine=self.copilot_engine,
            unit_of_work_factory=self.unit_of_work_factory(),
            database_url=self.settings.database.database_url,
            embedding_configured=configured,
            embedding_detail=detail,
        )

    def investigation_read_service(self) -> InvestigationReadService:
        return InvestigationReadService(unit_of_work=self.unit_of_work())

    def investigation_workspace_service(self) -> InvestigationWorkspaceService:
        return InvestigationWorkspaceService(unit_of_work=self.unit_of_work())


def _budget_limits(settings: Settings) -> BudgetLimits:
    b = settings.agent_budget
    return BudgetLimits(
        max_steps=b.max_steps,
        max_tool_calls=b.max_tool_calls,
        max_llm_calls=b.max_llm_calls,
        max_llm_tokens=b.max_llm_tokens,
        max_duration_seconds=b.max_duration_seconds,
    )


@lru_cache(maxsize=1)
def build_container() -> Container:
    """Return the process-wide container (used by the app lifespan)."""
    from ..config import get_settings

    return Container(get_settings())
