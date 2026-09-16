from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
import uvicorn
from mcp.server.mcpserver import MCPServer

from hisiem_soc_copilot.agent.tools.providers import (
    AdmissionEntry,
    ProviderInvocationContext,
    ResultBounds,
    external_schema_fingerprint,
)
from hisiem_soc_copilot.config import MCPServerSettings
from hisiem_soc_copilot.contracts.tools.types import ToolCandidate
from hisiem_soc_copilot.infrastructure.mcp.provider import MCPToolProvider


class AuthorizationCapture:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.authorization_headers: list[str | None] = []

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http":
            headers = dict(scope.get("headers", []))
            raw = headers.get(b"authorization")
            self.authorization_headers.append(raw.decode("utf-8") if raw else None)
        await self.app(scope, receive, send)


@asynccontextmanager
async def _serve(app: Any) -> AsyncIterator[str]:
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_level="critical",
        lifespan="on",
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


@pytest.mark.asyncio
async def test_provider_uses_official_streamable_http_client_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp_server = MCPServer(name="stage-c-local", version="test")

    @mcp_server.tool(name="read_record", structured_output=True)
    async def read_record(query: str) -> dict[str, str]:
        return {"answer": query}

    tools = await mcp_server.list_tools()
    assert len(tools) == 1
    remote_tool = tools[0]
    external_input_schema = dict(remote_tool.input_schema)
    external_output_schema = (
        dict(remote_tool.output_schema) if remote_tool.output_schema is not None else None
    )
    app = AuthorizationCapture(
        mcp_server.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
        )
    )

    async with _serve(app) as endpoint:
        monkeypatch.setenv("STAGE_C_MCP_TOKEN", "local-secret")
        server_settings = MCPServerSettings(
            server_id="local",
            endpoint=endpoint,
            allowed_hosts=["127.0.0.1"],
            trusted_internal_transport=True,
            auth_token_env="STAGE_C_MCP_TOKEN",
            server_category="internal",
        )
        admission = AdmissionEntry(
            internal_name="mcp.read_record",
            description="Read a local test record",
            server_id="local",
            external_name="read_record",
            input_schema={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
            result_schema=external_output_schema,
            expected_external_schema_fingerprint=external_schema_fingerprint(
                "read_record",
                external_input_schema,
                external_output_schema,
            ),
        )
        provider = MCPToolProvider(
            server=server_settings,
            admissions=[admission],
            global_bounds=ResultBounds(),
        )

        await provider.start()
        assert provider.discovery_failure is None
        assert provider.unavailable == {}
        result = await provider.invoke(
            admission=admission,
            candidate=ToolCandidate(
                tool_name="mcp.read_record",
                arguments={"query": "local-value"},
            ),
            context=ProviderInvocationContext(
                tenant_id="trusted-tenant",
                investigation_id="inv-local",
                tool_call_id="call-local",
                source_alert_ref={"provider": "hisiem", "address_id": "alert-local"},
            ),
        )

        assert result.status == "SUCCESS"
        assert result.data == {"answer": "local-value"}
        assert "Bearer local-secret" in app.authorization_headers
