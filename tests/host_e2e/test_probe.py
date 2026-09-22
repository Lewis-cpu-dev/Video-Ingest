from __future__ import annotations

import base64
import io
import json
import os

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolResult
from PIL import Image

from server.config import PROJECT_ROOT
from server.probe import ProbeStore, create_probe_server


@pytest.mark.asyncio
async def test_probe_images_hide_ground_truth_from_nonpixel_content(tmp_path):
    server = create_probe_server(tmp_path)
    result = await server.call_tool("probe_images", {})
    assert isinstance(result, CallToolResult)
    probe_id = result.structuredContent["probe_id"]
    record = json.loads((tmp_path / probe_id / "private_ground_truth.json").read_text())
    public = json.dumps(result.structuredContent) + result.content[0].text
    for number in record["truth"]["digits"]:
        assert number not in public
    for metadata, timestamp in zip(result.structuredContent["frames"], record["truth"]["timestamps_ms"]):
        assert metadata["timestamp_ms"] == timestamp
        block = result.content[metadata["image_content_index"]]
        im = Image.open(io.BytesIO(base64.b64decode(block.data)))
        assert not im.info
        assert im.getextrema() == ((0, 255), (0, 255), (0, 255))


@pytest.mark.asyncio
async def test_file_probe_secret_requires_resource_read(tmp_path):
    server = create_probe_server(tmp_path)
    result = await server.call_tool("probe_file", {})
    probe_id = result.structuredContent["probe_id"]
    truth = json.loads((tmp_path / probe_id / "private_ground_truth.json").read_text())["truth"]["file_token"]
    assert truth not in result.model_dump_json()
    resources = await server.read_resource(str(result.content[1].uri))
    assert any(resource.content == truth for resource in resources)


@pytest.mark.asyncio
async def test_probe_job_persists_across_server_recreation(tmp_path):
    server = create_probe_server(tmp_path)
    result = await server.call_tool("probe_start_job", {})
    job_id = result.structuredContent["job_id"]
    recreated = create_probe_server(tmp_path)
    status = await recreated.call_tool("probe_get_job", {"job_id": job_id})
    assert status.structuredContent["job_id"] == job_id
    assert status.structuredContent["status"] == "queued"


def test_probe_store_rejects_path_traversal(tmp_path):
    store = ProbeStore(tmp_path)
    with pytest.raises(ValueError):
        store.record("../../private")


@pytest.mark.asyncio
async def test_real_stdio_mcp_roundtrip():
    # Client and server transport test only; no target-host/model consumption is implied.
    parameters = StdioServerParameters(
        command=str(PROJECT_ROOT / ".venv/bin/python"), args=["-m", "server.probe"],
        cwd=str(PROJECT_ROOT), env={**os.environ, "PYTHONPYCACHEPREFIX": str(PROJECT_ROOT / ".cache/pycache")},
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert {"probe_text", "probe_images", "probe_audio", "probe_video", "probe_file"} <= {
            tool.name for tool in tools.tools}
        text = await session.call_tool("probe_text", {})
        assert text.structuredContent["random_text"]
        pictures = await session.call_tool("probe_images", {})
        assert len([c for c in pictures.content if c.type == "image"]) == 3
        assert pictures.structuredContent["host_test_status"] == "awaiting_operator_verification"


@pytest.mark.asyncio
async def test_audio_probe_has_real_pcm_and_private_beep_count(tmp_path):
    import wave

    server = create_probe_server(tmp_path)
    result = await server.call_tool("probe_audio", {})
    assert result.content[1].type == "audio"
    assert "beep_count" not in result.model_dump_json()
    truth = json.loads((tmp_path / result.structuredContent["probe_id"] /
                        "private_ground_truth.json").read_text())["truth"]
    with wave.open(io.BytesIO(base64.b64decode(result.content[1].data)), "rb") as audio:
        assert audio.getframerate() == 16000
        assert audio.getnframes() == truth["beep_count"] * 6400
        assert set(audio.readframes(audio.getnframes())) != {0}


@pytest.mark.asyncio
async def test_video_probe_has_real_resource_and_private_sequence(tmp_path):
    server = create_probe_server(tmp_path)
    result = await server.call_tool("probe_video", {})
    if result.structuredContent.get("status") == "not_run":
        pytest.skip("FFmpeg unavailable for optional video probe encoding")
    probe_id = result.structuredContent["probe_id"]
    truth = json.loads((tmp_path / probe_id / "private_ground_truth.json").read_text())["truth"]
    assert all(number not in result.model_dump_json() for number in truth["digits_in_order"])
    resources = await server.read_resource(str(result.content[1].uri))
    assert resources[0].content[4:8] == b"ftyp"
    assert resources[0].mime_type == "video/mp4"
