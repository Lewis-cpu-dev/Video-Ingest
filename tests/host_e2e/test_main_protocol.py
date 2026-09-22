from __future__ import annotations

import json
import os
import subprocess

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from starlette.responses import JSONResponse

from server.config import PROJECT_ROOT
from server.main import BearerAuth


@pytest.mark.asyncio
async def test_main_real_stdio_tool_schema_and_structured_error(tmp_path):
    configuration = json.loads((PROJECT_ROOT / "plugin/.mcp.json").read_text())["mcpServers"]["video-ingest"]
    parameters = StdioServerParameters(
        command=configuration["command"], args=configuration["args"], cwd=configuration["cwd"],
        env={**os.environ, "VIDEO_INGEST_DATA_DIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listed = await session.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "resolve_video", "prepare_video", "get_job", "read_transcript",
            "inspect_segment", "get_media", "cancel_job", "delete_asset",
        }
        tools = {tool.name: tool for tool in listed.tools}
        assert tools["prepare_video"].annotations.readOnlyHint is False
        assert tools["get_job"].annotations.readOnlyHint is True
        assert tools["delete_asset"].annotations.destructiveHint is True
        result = await session.call_tool("resolve_video", {"url": "https://unsupported.example/video"})
        assert result.isError
        assert result.structuredContent["error"]["code"] in {"UNSUPPORTED_URL", "UNSUPPORTED_PLATFORM"}
        assert "trace_id" in result.structuredContent
        assert str(tmp_path) not in result.model_dump_json()


@pytest.mark.parametrize("arguments,expected", [
    (["--host", "0.0.0.0"], "invalid choice"),
    (["--transport", "http"], "VIDEO_INGEST_TOKEN"),
])
def test_main_cli_refuses_public_bind_or_missing_auth(tmp_path, arguments, expected):
    env = {**os.environ, "VIDEO_INGEST_TOKEN": "", "VIDEO_INGEST_DATA_DIR": str(tmp_path)}
    process = subprocess.run([str(PROJECT_ROOT / ".venv/bin/python"), "-m", "server.main", *arguments],
                             capture_output=True, text=True, cwd=PROJECT_ROOT, env=env, timeout=15, check=False)
    assert process.returncode == 2
    assert expected in process.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_bearer_auth_covers_mcp_and_artifact_routes():
    async def downstream(scope, receive, send):
        await JSONResponse({"accepted": True})(scope, receive, send)

    token = "t" * 32
    transport = httpx.ASGITransport(app=BearerAuth(downstream, token))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        for path in ["/mcp", "/artifacts/art_unknown"]:
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
            duplicate = [("Authorization", f"Bearer {token}"), ("Authorization", f"Bearer {token}")]
            assert (await client.get(path, headers=duplicate)).status_code == 401
            result = await client.get(path, headers={"Authorization": f"Bearer {token}"})
            assert result.status_code == 200
