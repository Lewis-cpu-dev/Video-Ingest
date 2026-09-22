"""Verify the real stdio and authenticated HTTP MCP deployments without platform downloads."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import socket
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"resolve_video", "prepare_video", "get_job", "read_transcript", "inspect_segment",
            "get_media", "cancel_job", "delete_asset"}


async def check_session(read, write):
    async with ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        if {tool.name for tool in tools.tools} != EXPECTED:
            raise RuntimeError("The server did not expose the expected eight tools")
        response = await session.call_tool("resolve_video", {"url": "https://unsupported.example/video"})
        if not response.isError or response.structuredContent["error"]["code"] != "UNSUPPORTED_PLATFORM":
            raise RuntimeError("Structured tool error handling failed")


async def verify(directory: Path):
    config_path = ROOT / "plugin/.mcp.json"
    if not config_path.is_file():
        raise RuntimeError("Run scripts/configure_plugin.py before this check")
    config = json.loads(config_path.read_text())["mcpServers"]["video-ingest"]
    env = {**os.environ, **config.get("env", {}), "VIDEO_INGEST_DATA_DIR": str(directory / "stdio"),
           "PYTHONDONTWRITEBYTECODE": "1"}
    parameters = StdioServerParameters(command=config["command"], args=config["args"],
                                      cwd=str(directory), env=env)
    async with stdio_client(parameters) as (read, write):
        await check_session(read, write)

    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env.update(VIDEO_INGEST_DATA_DIR=str(directory / "http"), VIDEO_INGEST_TOKEN=token)
    url = f"http://127.0.0.1:{port}/mcp"
    with (directory / "http.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            config["command"], *config["args"], "--transport", "http", "--port", str(port),
            cwd=directory, env=env, stdout=log, stderr=log)
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                for _ in range(60):
                    if process.returncode is not None:
                        raise RuntimeError("HTTP server exited before becoming ready")
                    try:
                        response = await client.get(url)
                        if response.status_code == 401:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("HTTP server did not become ready")
                denied = await client.get(f"http://127.0.0.1:{port}/artifacts/art_unknown")
                if denied.status_code != 401:
                    raise RuntimeError("Artifact authentication check failed")
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
                await check_session(read, write)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)


def main():
    for executable in ("ffmpeg", "ffprobe"):
        if not shutil.which(executable):
            raise SystemExit(f"Required executable missing: {executable}")
    temporary = ROOT / ".tmp"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="deployment-", dir=temporary) as directory:
        asyncio.run(verify(Path(directory)))
    record = {"recorded_at": datetime.now(UTC).isoformat(), "status": "passed",
              "checks": ["stdio MCP from another working directory", "HTTP MCP initialization and tools",
                         "eight expected tools", "structured errors", "HTTP bearer authentication",
                         "artifact endpoint authentication", "ffmpeg and ffprobe available"],
              "scope": "Local deployment; target ChatGPT account and remote gateway not tested"}
    destination = ROOT / "docs/runs/deployment-verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(record, indent=2) + "\n")
    print("PASS: stdio and authenticated HTTP deployments expose all eight tools.")


if __name__ == "__main__":
    main()
