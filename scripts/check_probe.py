"""Record local MCP text/image delivery; this does not test ChatGPT perception."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import secrets
import socket
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str):
    if not condition:
        raise RuntimeError(message)


async def check_session(read, write, directory: Path) -> dict:
    async with ClientSession(read, write) as session:
        await session.initialize()
        text = await session.call_tool("probe_text", {})
        require(not text.isError, "Text probe returned an error")
        payload = text.structuredContent
        require(isinstance(payload, dict), "Text probe has no structured content")
        token = payload.get("random_text")
        require(isinstance(token, str) and bool(token), "Text probe returned empty text")
        require(any(block.type == "text" and json.loads(block.text) == payload
                    for block in text.content), "Text content and structured content disagree")

        result = await session.call_tool("probe_images", {})
        require(not result.isError, "Image probe returned an error")
        payload = result.structuredContent
        require(isinstance(payload, dict), "Image probe has no structured content")
        require(payload["host_delivery"]["host_consumption_proven"] is False,
                "Local result must not claim verified host perception")
        frames = payload["frames"]
        require(len(frames) == 3 and sum(block.type == "image" for block in result.content) == 3,
                "Expected three actual MCP ImageContent blocks")
        require([frame["image_content_index"] for frame in frames] == [1, 2, 3],
                "Frame bindings are incorrect")
        require([frame["timestamp_ms"] for frame in frames] == [1250, 7600, 18250],
                "Frame timestamps are incorrect")
        source = directory / "source" / payload["probe_id"]
        truth = json.loads((source / "private_ground_truth.json").read_text())["truth"]
        public_text = json.dumps(payload) + "".join(
            block.text for block in result.content if block.type == "text")
        require(all(number not in public_text for number in truth["digits"]),
                "Image answers leaked into text metadata")
        samples = directory / "received"
        samples.mkdir(mode=0o700)
        (samples / "text.txt").write_text(token + "\n")
        images = []
        for index, frame in enumerate(frames):
            block = result.content[frame["image_content_index"]]
            require(block.type == "image" and block.mimeType == "image/png", "Expected PNG image")
            raw = base64.b64decode(block.data, validate=True)
            require(raw == (source / f"frame_{index}.png").read_bytes(),
                    "Received bytes differ from the generated source image")
            with Image.open(io.BytesIO(raw)) as picture:
                picture.load()
                require(picture.format == "PNG" and picture.size == (640, 200),
                        "Invalid image format or dimensions")
                require(not picture.info, "Unexpected image metadata")
                require(picture.getextrema() == ((0, 255), (0, 255), (0, 255)),
                        "Image is blank or missing expected pixel contrast")
            destination = samples / f"frame_{index}.png"
            destination.write_bytes(raw)
            images.append({"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                           "width": 640, "height": 200, "timestamp_ms": frame["timestamp_ms"],
                           "received_file": str(destination.relative_to(ROOT))})
        return {"status": "passed", "text_characters": len(token),
                "text_sha256": hashlib.sha256(token.encode()).hexdigest(), "images": images,
                "image_answers_absent_from_text": True, "host_consumption_proven": False}


async def verify(directory: Path, results: dict):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    stdio_dir = directory / "stdio"
    parameters = StdioServerParameters(
        command=sys.executable, args=["-m", "server.probe", "--data-dir", str(stdio_dir / "source")],
        cwd=str(ROOT), env=env)
    async with stdio_client(parameters) as (read, write):
        results["stdio"] = await check_session(read, write, stdio_dir)

    http_dir = directory / "http"
    http_dir.mkdir(mode=0o700)
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    with (http_dir / "server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "server.probe", "--transport", "streamable-http", "--port", str(port),
            "--data-dir", str(http_dir / "source"), cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            async with httpx.AsyncClient(timeout=1, trust_env=False) as client:
                for _ in range(100):
                    if process.returncode is not None:
                        raise RuntimeError("HTTP probe exited before becoming ready")
                    try:
                        response = await client.get(url)
                        if response.status_code in {400, 406}:
                            break
                    except httpx.TransportError:
                        pass
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("HTTP probe did not become ready")
            async with streamablehttp_client(url) as (read, write, _):
                results["http_loopback"] = await check_session(read, write, http_dir)
        finally:
            if process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), timeout=5)


def main() -> int:
    os.umask(0o077)
    directory = ROOT / ".runtime/prototype-verification" / secrets.token_hex(8)
    directory.mkdir(parents=True, mode=0o700)
    report = {"recorded_at": datetime.now(UTC).isoformat(), "status": "failed", "transports": {},
              "scope": "Local MCP client receives real text and PNG pixels; no OCR or model perception test",
              "video_download": "not_tested_by_this_check",
              "chatgpt_web": {"status": "not_tested", "host_consumption_proven": False}}
    try:
        asyncio.run(asyncio.wait_for(verify(directory, report["transports"]), timeout=90))
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001 — save a failed run instead of leaving a stale passing report
        report["error_type"] = type(exc).__name__
        (directory / "failure.log").write_text(traceback.format_exc())
        print(f"FAIL: local probe check ({type(exc).__name__}); inspect {directory}", file=sys.stderr)
    destination = ROOT / "docs/runs/prototype-local-verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2) + "\n")
    if report["status"] == "passed":
        print("PASS: stdio and loopback HTTP each returned real text and three PNG images.")
        print("ChatGPT web model reading remains NOT TESTED.")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
