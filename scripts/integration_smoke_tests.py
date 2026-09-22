#!/usr/bin/env python3
"""Opt-in public-platform evidence checks. Reports backend results, never host perception."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import secrets
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def local_path(value: str) -> Path:
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise argparse.ArgumentTypeError("Paths must stay within the workspace")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", action="store_true", help="Explicitly allow requests to the selected platforms")
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--matrix", type=local_path, help="JSON matrix; only enabled rows with a URL are run")
    parser.add_argument("--captions", action="store_true")
    parser.add_argument("--media", action="store_true", help="Download bounded real video and extract an image")
    parser.add_argument("--language")
    parser.add_argument("--timeout-seconds", type=int, default=90)
    parser.add_argument("--region", default="unrecorded")
    parser.add_argument("--max-download-mib", type=int, default=32)
    parser.add_argument("--output", type=local_path, default=ROOT / "docs/runs/platform_smoke.json")
    args = parser.parse_args()
    if not args.network:
        parser.error("No network work runs without --network")
    if args.timeout_seconds < 5 or args.timeout_seconds > 1800:
        parser.error("--timeout-seconds must be between 5 and 1800")
    if args.max_download_mib < 1 or args.max_download_mib > 1024:
        parser.error("--max-download-mib must be between 1 and 1024")
    entries = [{"id": f"CLI-{i + 1:02}", "url": url, "permission_basis": "explicit operator request"}
               for i, url in enumerate(args.url)]
    if args.matrix:
        entries += [row for row in json.loads(args.matrix.read_text())["samples"]
                    if row.get("enabled") and row.get("url")]
    if not entries:
        parser.error("Supply at least one --url or enabled --matrix entry")

    from server.adapters.delivery import HostDeliveryAdapter
    from server.adapters.platforms import normalize_url
    from server.config import Settings
    from server.contracts.models import TimeRange
    from server.errors import IngestError
    from server.processing.media import MediaProcessor
    from server.providers.ytdlp import YtDlpProvider

    run_id = secrets.token_hex(12)
    root = ROOT / ".runtime/smoke" / run_id
    settings = Settings(data_dir=root, max_download_bytes=args.max_download_mib * 1024**2,
                        job_timeout_seconds=args.timeout_seconds)
    provider = YtDlpProvider(settings)
    processor = MediaProcessor(settings)
    rows = []
    for row in entries:
        sample_id = row["id"]
        record = {"id": sample_id, "canonical_source": None, "status": "failed", "steps": {}, "errors": [],
                  "host_consumption": "not_tested", "download_bytes": 0}
        rows.append(record)
        if not row.get("permission_basis"):
            record["error"] = {"code": "PERMISSION_BASIS_MISSING", "stage": "validating"}
            continue
        try:
            source = normalize_url(row["url"])
            record["canonical_source"] = source.model_dump()
            metadata = provider.inspect(source)
            record["steps"]["metadata"] = {"status": "passed", "duration_ms": metadata.duration_ms,
                                             "caption_track_count": len(metadata.caption_tracks),
                                             "media_available": metadata.media_available, "warnings": metadata.warnings}
            if args.captions:
                try:
                    segments, language = provider.fetch_captions(source, metadata, args.language, lambda: False)
                    encoded = json.dumps([segment.model_dump() for segment in segments], sort_keys=True).encode()
                    record["steps"]["captions"] = {
                        "status": "passed", "segments": len(segments), "language": language,
                        "sha256": hashlib.sha256(encoded).hexdigest(),
                        "provenance": sorted({segment.provenance for segment in segments}),
                    }
                except IngestError as exc:
                    record["steps"]["captions"] = {"status": "failed", "code": exc.code}
                    record["errors"].append({"code": exc.code, "stage": exc.stage})
            if args.media:
                output = root / secrets.token_hex(12)
                output.mkdir(parents=True)

                def on_bytes(delta, record=record):
                    record["download_bytes"] += delta
                    if record["download_bytes"] > settings.max_download_bytes:
                        raise IngestError("BUDGET_EXCEEDED", "Smoke download budget exceeded", stage="downloading")

                path = provider.acquire(source, "video", output, "overview", lambda: False, on_bytes)
                information = processor.probe(path)
                with path.open("rb") as downloaded:
                    checksum = hashlib.file_digest(downloaded, "sha256").hexdigest()
                record["steps"]["media"] = {"status": "passed", "bytes": path.stat().st_size,
                                             "sha256": checksum,
                                             "duration_ms": information.get("duration_ms")}
                end_ms = min(information.get("duration_ms") or 3000, 3000)
                frames = processor.extract_frames(path, output / "frames", TimeRange(start_ms=0, end_ms=end_ms),
                                                  1, "overview", lambda: False)
                delivery = HostDeliveryAdapter(settings.client_profile).render_images({}, [
                    ({key: value for key, value in frame.items() if key != "path"}, Path(frame["path"]))
                    for frame in frames
                ])
                record["steps"]["image_delivery"] = {
                    "status": "local_protocol_passed", "images": len(frames),
                    "content_types": [item.type for item in delivery.content],
                    "host_perception": "not_tested",
                }
            record["status"] = "partial" if record["errors"] else "requested_backend_steps_passed"
        except IngestError as exc:
            record["status"] = "partial" if record["steps"] else "failed"
            record["error"] = {"code": exc.code, "stage": exc.stage, "retryable": exc.retryable,
                               "next_action": exc.next_action}
        except Exception as exc:  # noqa: BLE001 — redact unexpected upstream diagnostics
            # Do not serialize exception text: upstream errors can contain signed URLs or paths.
            record["status"] = "partial" if record["steps"] else "failed"
            record["error"] = {"code": "UNEXPECTED_ERROR", "exception_type": type(exc).__name__}
    report = {"run_id": run_id, "created_at": datetime.now(UTC).isoformat(),
              "scope": "backend smoke; target ChatGPT host not exercised", "network_region": args.region,
              "credentials": "none", "versions": {name: importlib.metadata.version(name)
                                                     for name in ["mcp", "yt-dlp", "Pillow"]},
              "requested": {"captions": args.captions, "media": args.media}, "samples": rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"report": str(args.output.relative_to(ROOT)), "samples": len(rows),
                      "passed_requested_backend_steps": sum(r["status"] == "requested_backend_steps_passed"
                                                            for r in rows)}))
    return 0 if all(r["status"] == "requested_backend_steps_passed" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
