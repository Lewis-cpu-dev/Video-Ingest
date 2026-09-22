"""Independent Gate 0 probes. Passing transport is not passing host perception."""
from __future__ import annotations

import argparse
import base64
import io
import json
import math
import os
import secrets
import struct
import subprocess
import time
import wave
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import AudioContent, CallToolResult, ResourceLink, TextContent, ToolAnnotations
from PIL import Image, ImageDraw, ImageFont

from server.adapters.delivery import HostDeliveryAdapter
from server.config import PROJECT_ROOT
from server.contracts.models import ClientProfile

PROBE_TTL_SECONDS = 1800


def pixel_image(digits: str) -> bytes:
    """Render using Pillow's bundled font; numbers are absent from image metadata."""
    picture = Image.new("RGB", (640, 200), "white")
    draw = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=82)
    draw.text((320, 100), digits, font=font, fill="black", anchor="mm")
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return output.getvalue()


class ProbeStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_relative_to(PROJECT_ROOT) or self.root == PROJECT_ROOT:
            raise ValueError("Probe data must remain inside the workspace")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.prune()

    def prune(self):
        # All retained items have opaque service-created directory names.
        for directory in self.root.glob("probe_*"):
            record = directory / "private_ground_truth.json"
            if directory.is_symlink() or not record.is_file() or record.is_symlink():
                continue
            if json.loads(record.read_text())["expires_at"] < time.time():
                for item in directory.iterdir():
                    if item.is_file() and not item.is_symlink():
                        item.unlink()
                directory.rmdir()

    def create(self, kind: str, truth: dict) -> tuple[str, Path]:
        self.prune()
        probe_id = "probe_" + secrets.token_hex(16)
        directory = self.root / probe_id
        directory.mkdir(mode=0o700)
        (directory / "private_ground_truth.json").write_text(json.dumps({
            "probe_id": probe_id, "kind": kind, "created_at": time.time(),
            "expires_at": time.time() + PROBE_TTL_SECONDS, "truth": truth,
        }))
        return probe_id, directory

    def record(self, probe_id: str) -> tuple[dict, Path]:
        if not probe_id.startswith("probe_") or len(probe_id) != 38:
            raise ValueError("Unknown probe")
        if any(c not in "0123456789abcdef" for c in probe_id[6:]):
            raise ValueError("Unknown probe")
        directory = self.root / probe_id
        ground_truth = directory / "private_ground_truth.json"
        if directory.is_symlink() or ground_truth.is_symlink():
            raise ValueError("Unknown probe")
        record = json.loads(ground_truth.read_text())
        if record["expires_at"] < time.time():
            raise ValueError("Probe expired; create a new challenge")
        return record, directory


def create_probe_server(root: Path | None = None, port: int = 8766) -> FastMCP:
    store = ProbeStore(root or PROJECT_ROOT / ".runtime" / "probes")
    delivery = HostDeliveryAdapter(ClientProfile(name="gate0_probe", verification="unverified"))
    app = FastMCP("Video Ingest Gate 0", host="127.0.0.1", port=port,
                  instructions="Report only directly perceived probe content. Do not guess unsupported media.")
    annotations = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False,
                                  openWorldHint=False)

    @app.tool(annotations=annotations)
    def probe_text() -> CallToolResult:
        """Create a random text challenge; repeat its text exactly to prove text delivery."""
        token = secrets.token_urlsafe(18)
        probe_id, _ = store.create("text", {"text": token})
        return delivery.render_text({"probe_id": probe_id, "random_text": token,
                                     "host_test_status": "awaiting_operator_verification"})

    @app.tool(annotations=annotations)
    def probe_images() -> CallToolResult:
        """Read the numbers in three actual images and pair them with source timestamps."""
        digits = [str(secrets.randbelow(900000) + 100000) for _ in range(3)]
        timestamps = [1250, 7600, 18250]
        probe_id, directory = store.create("images", {"digits": digits, "timestamps_ms": timestamps})
        frames = []
        for index, (number, timestamp) in enumerate(zip(digits, timestamps)):
            path = directory / f"frame_{index}.png"
            path.write_bytes(pixel_image(number))
            frames.append(({"frame_id": f"fr_{index}", "timestamp_ms": timestamp,
                            "artifact_id": f"art_{index}", "asset_revision": "r1",
                            "selection_reason": "random_pixel_probe", "width": 640, "height": 200}, path))
        return delivery.render_images({"probe_id": probe_id,
                                       "host_test_status": "awaiting_operator_verification"}, frames)

    @app.tool(annotations=annotations)
    def probe_file() -> CallToolResult:
        """Return a resource link; obtain the secret inside the file to test actual file reading."""
        token = secrets.token_urlsafe(18)
        probe_id, directory = store.create("file", {"file_token": token})
        (directory / "payload.txt").write_text(token)
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps({"probe_id": probe_id,
                        "instruction": "Read the resource and repeat its token. A button is not a pass."})),
            ResourceLink(type="resource_link", uri=f"video-ingest-probe://file/{probe_id}",
                         name="random-file-challenge", mimeType="text/plain", size=len(token)),
        ], structuredContent={"probe_id": probe_id, "host_test_status": "awaiting_operator_verification"})

    @app.resource("video-ingest-probe://file/{probe_id}", mime_type="text/plain")
    def read_probe_file(probe_id: str) -> str:
        record, directory = store.record(probe_id)
        if record["kind"] != "file":
            raise ValueError("Unknown file probe")
        return (directory / "payload.txt").read_text()

    @app.tool(annotations=annotations)
    def probe_audio() -> CallToolResult:
        """Count the audible beeps in native MCP audio; this tests audio independently from images."""
        count = secrets.randbelow(4) + 2
        probe_id, _ = store.create("audio", {"beep_count": count})
        rate = 16000
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(rate)
            for _ in range(count):
                wav.writeframes(b"".join(struct.pack("<h", int(14000 * math.sin(2 * math.pi * 550 * i / rate)))
                                          for i in range(rate // 5)))
                wav.writeframes(b"\0\0" * (rate // 5))
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps({"probe_id": probe_id, "instruction": "Count the beeps."})),
            AudioContent(type="audio", mimeType="audio/wav", data=base64.b64encode(output.getvalue()).decode()),
        ], structuredContent={"probe_id": probe_id, "host_test_status": "awaiting_operator_verification"})

    @app.tool(annotations=annotations)
    def probe_video() -> CallToolResult:
        """Experimental MP4 resource probe, separate from image delivery and file download."""
        digits = [str(secrets.randbelow(900000) + 100000) for _ in range(3)]
        probe_id, directory = store.create("video", {"digits_in_order": digits})
        for index, number in enumerate(digits):
            (directory / f"frame_{index}.png").write_bytes(pixel_image(number))
        try:
            completed = subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-framerate", "1", "-i",
                str(directory / "frame_%d.png"), "-frames:v", "3", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(directory / "payload.mp4"),
            ], capture_output=True, timeout=30, check=False,
               env={**os.environ, "TMPDIR": str(PROJECT_ROOT / ".tmp")})
            encoding_failed = completed.returncode != 0
        except (OSError, subprocess.TimeoutExpired):
            encoding_failed = True
        if encoding_failed:
            return delivery.render_text({"probe_id": probe_id, "status": "not_run",
                                         "reason": "fixture_encoding_failed"})
        return CallToolResult(content=[
            TextContent(type="text", text=json.dumps({"probe_id": probe_id,
                        "instruction": "Read the changing numbers from the video, in order.",
                        "transport": "MP4 MCP resource; host native consumption remains unverified"})),
            ResourceLink(type="resource_link", uri=f"video-ingest-probe://video/{probe_id}",
                         name="random-video-challenge", mimeType="video/mp4",
                         size=(directory / "payload.mp4").stat().st_size),
        ], structuredContent={"probe_id": probe_id, "host_test_status": "awaiting_operator_verification"})

    @app.resource("video-ingest-probe://video/{probe_id}", mime_type="video/mp4")
    def read_probe_video(probe_id: str) -> bytes:
        record, directory = store.record(probe_id)
        if record["kind"] != "video":
            raise ValueError("Unknown video probe")
        return (directory / "payload.mp4").read_bytes()

    @app.tool(annotations=annotations)
    def probe_start_job() -> CallToolResult:
        """Create a persistent test job; read its result in a later conversation or after server restart."""
        token = secrets.token_urlsafe(18)
        job_id, _ = store.create("job", {"result": token, "ready_at": time.time() + 5})
        return delivery.render_text({"job_id": job_id, "status": "queued", "poll_after_ms": 5000})

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def probe_get_job(job_id: str) -> CallToolResult:
        """Read a persistent test job without starting new work."""
        record, _ = store.record(job_id)
        if record["kind"] != "job":
            raise ValueError("Unknown job")
        ready = time.time() >= record["truth"]["ready_at"]
        return delivery.render_text({"job_id": job_id, "status": "succeeded" if ready else "queued",
                                     "result": record["truth"]["result"] if ready else None,
                                     "poll_after_ms": None if ready else 5000})

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", type=Path, help="Probe storage directory inside the workspace")
    args = parser.parse_args()
    os.umask(0o077)
    create_probe_server(root=args.data_dir, port=args.port).run(transport=args.transport)


if __name__ == "__main__":
    main()
