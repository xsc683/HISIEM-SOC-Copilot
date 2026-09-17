"""Reusable in-process, deterministic Streamable HTTP MCP server fixture (E2).

Stage C proved the official MCP SDK client path with a real local Streamable HTTP
server, but built that server inline inside a single test function. E2 needs the
same thing for several XP-01 scenarios (admitted invocation, schema drift, result
bounds, prompt injection), so this module factors the proven pattern into one
reusable fixture instead of copying it.

It uses the SAME production objects the composition root uses — ``MCPServerSettings``,
``AdmissionEntry`` with a fingerprint computed by the Copilot's own
``external_schema_fingerprint``, and the real ``MCPToolProvider`` — so a scenario
that runs against it is exercising the real provider, not a stand-in.

Test-support only: nothing here is production code, and nothing here is imported by
``src``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp.server.mcpserver import MCPServer

from hisiem_soc_copilot.agent.tools.providers import (
    AdmissionEntry,
    ResultBounds,
    external_schema_fingerprint,
)
from hisiem_soc_copilot.config import MCPServerSettings
from hisiem_soc_copilot.infrastructure.mcp.provider import MCPToolProvider

ToolFn = Callable[..., Awaitable[dict[str, Any]]]


@asynccontextmanager
async def _serve(app: Any) -> AsyncIterator[str]:
    """Serve ``app`` on an ephemeral localhost port; yield the /mcp endpoint."""
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="critical", lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("local MCP server exited before startup")
            await asyncio.sleep(0.01)
        if not server.started:
            raise TimeoutError("local MCP server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)


@dataclass
class DeterministicMCPServer:
    """An in-process MCP server plus helpers to admit and invoke its tools.

    Usage::

        async with DeterministicMCPServer() as server:
            server.add_tool("read_record", read_record)
            admission = await server.admit("mcp.read_record", "read_record")
            provider = server.provider([admission])
            await provider.start()
            result = await provider.invoke(admission=admission, candidate=..., context=...)
    """

    name: str = "e2-deterministic"
    _server: MCPServer = field(init=False)
    _endpoint: str = field(default="", init=False)
    _names: list[str] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._server = MCPServer(name=self.name, version="1.0.0")

    # -- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> DeterministicMCPServer:
        app = self._server.streamable_http_app(
            streamable_http_path="/mcp", stateless_http=True
        )
        self._ctx = _serve(app)
        self._endpoint = await self._ctx.__aenter__()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._ctx.__aexit__(*exc)

    @property
    def endpoint(self) -> str:
        if not self._endpoint:
            raise RuntimeError("server is not running; use `async with`")
        return self._endpoint

    # -- tools -------------------------------------------------------------

    def add_tool(self, name: str, fn: ToolFn) -> None:
        self._server.add_tool(fn, name=name, structured_output=True)
        self._names.append(name)

    async def _schemas(self, external_name: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        for tool in await self._server.list_tools():
            if tool.name == external_name:
                out = dict(tool.output_schema) if tool.output_schema is not None else None
                return dict(tool.input_schema), out
        raise KeyError(f"no such tool on the fixture server: {external_name!r}")

    async def admit(
        self,
        internal_name: str,
        external_name: str,
        *,
        risk: str = "READ_ONLY",
        bounds: ResultBounds | None = None,
        drift_fingerprint: bool = False,
        tenant_scope: str = "TENANT_SCOPED",
        model_selectable: bool = True,
    ) -> AdmissionEntry:
        """Build a real ``AdmissionEntry`` bound to the server's OBSERVED schema.

        ``drift_fingerprint=True`` deliberately stores a fingerprint that does not
        match the observed schema, which is how XP-MCP-004 exercises fail-closed
        drift using the real fingerprint contract rather than a fake.
        """
        input_schema, output_schema = await self._schemas(external_name)
        fingerprint = external_schema_fingerprint(external_name, input_schema, output_schema)
        if drift_fingerprint:
            fingerprint = "0" * 64
        return AdmissionEntry(
            internal_name=internal_name,
            description=f"deterministic fixture capability {external_name}",
            server_id="e2-local",
            external_name=external_name,
            input_schema=input_schema,
            result_schema=output_schema,
            expected_external_schema_fingerprint=fingerprint,
            risk=risk,  # type: ignore[arg-type]
            tenant_scope=tenant_scope,  # type: ignore[arg-type]
            model_selectable=model_selectable,
            result_bounds=bounds or ResultBounds(),
        )

    def settings(self, *, token_env: str = "E2_MCP_FIXTURE_TOKEN") -> MCPServerSettings:
        return MCPServerSettings(
            server_id="e2-local",
            endpoint=self.endpoint,
            allowed_hosts=["127.0.0.1"],
            trusted_internal_transport=True,
            auth_token_env=token_env,
            server_category="internal",
        )

    def provider(
        self,
        admissions: list[AdmissionEntry],
        *,
        bounds: ResultBounds | None = None,
        token_env: str = "E2_MCP_FIXTURE_TOKEN",
    ) -> MCPToolProvider:
        return MCPToolProvider(
            server=self.settings(token_env=token_env),
            admissions=admissions,
            global_bounds=bounds or ResultBounds(),
        )


@asynccontextmanager
async def deterministic_mcp_server(
    tools: dict[str, ToolFn] | None = None,
    *,
    name: str = "e2-deterministic",
    token: str = "local-fixture-token",
) -> AsyncIterator[DeterministicMCPServer]:
    """Start a fixture MCP server with ``tools`` already registered."""
    os.environ.setdefault("E2_MCP_FIXTURE_TOKEN", token)
    async with DeterministicMCPServer(name=name) as server:
        for tool_name, fn in (tools or {}).items():
            server.add_tool(tool_name, fn)
        yield server
