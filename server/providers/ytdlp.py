"""Public platform extraction with isolated execution and guarded, metered egress."""
from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

from server.config import Settings
from server.contracts.models import VideoMetadata, VideoSource
from server.errors import IngestError
from server.processing.media import MediaProcessor
from server.processing.process import run_process, workspace_path
from server.providers.captions import choose_caption, parse_captions
from server.security.network import GuardedProxy, SafeHTTPClient


class YtDlpProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _request(self, source, operation, *, cancelled=lambda: False, on_bytes=None, **extra):
        stage = "resolving" if operation == "inspect" else "downloading"
        consumed = 0
        billing_lock = threading.Lock()

        def charge(delta):
            nonlocal consumed
            with billing_lock:
                consumed += delta
                if on_bytes is not None:
                    on_bytes(delta)
                limit = self.settings.max_download_bytes if operation == "acquire" else 32 * 1024**2
                if consumed > limit:
                    raise IngestError("BUDGET_EXCEEDED", "The actual transferred bytes exceeded the limit.",
                                      stage, next_action="reduce_scope")

        with GuardedProxy(on_bytes=charge, cancelled=cancelled) as proxy:
            def check_cancelled():
                proxy.raise_if_error()
                return cancelled()

            payload = {"source": source.model_dump(), "operation": operation, "proxy": proxy.url,
                       "max_duration_ms": self.settings.max_duration_ms,
                       "max_download_bytes": self.settings.max_download_bytes, **extra}
            try:
                raw = run_process([sys.executable, "-m", "server.providers.ytdlp_worker"],
                                  timeout=min(self.settings.job_timeout_seconds, 90) if operation == "inspect"
                                  else self.settings.job_timeout_seconds,
                                  max_file_bytes=self.settings.max_disk_bytes, cancelled=check_cancelled,
                                  input_data=json.dumps(payload).encode(), stage=stage,
                                  max_output_bytes=8 * 1024**2,
                                  # V8 reserves inaccessible address cages; writable data remains 2 GiB
                                  # and the allowlisted runtime caps its JS heap to 512 MiB.
                                  max_address_bytes=64 * 1024**3)
                proxy.raise_if_error()
            except IngestError:
                proxy.raise_if_error()
                raise
        try:
            result = json.loads(raw)
        except ValueError as exc:
            raise IngestError("PROVIDER_FAILED", "The platform extractor returned an invalid response.",
                              stage, next_action="check_deployment") from exc
        if "error" in result:
            raise IngestError(**result["error"])
        return result

    def inspect(self, source: VideoSource) -> VideoMetadata:
        result = self._request(source, "inspect")
        return VideoMetadata.model_validate(result["metadata"])

    def fetch_captions(self, source, metadata, language, cancelled):
        if cancelled():
            raise IngestError("CANCELLED", "The operation was cancelled.", "fetching_captions")
        if not metadata.caption_tracks:
            if "CAPTION_AUTH_REQUIRED" in metadata.warnings:
                raise IngestError("AUTH_REQUIRED", "The platform requires authorization for captions.",
                                  "fetching_captions", next_action="use_public_source")
            if "CAPTION_DISCOVERY_FAILED" in metadata.warnings:
                raise IngestError("CAPTION_FETCH_FAILED", "Caption availability could not be established.",
                                  "fetching_captions", next_action="retry_or_request_asr")
        track = choose_caption(metadata.caption_tracks, language)
        try:
            payload = SafeHTTPClient(cancelled=cancelled).get_bytes(
                track.url, max_bytes=min(8 * 1024**2, self.settings.max_download_bytes),
                headers={"Referer": source.canonical_url})
        except IngestError as exc:
            if exc.code in {"UNSAFE_URL", "CANCELLED", "BUDGET_EXCEEDED"}:
                raise
            raise IngestError("CAPTION_FETCH_FAILED", "The advertised caption track could not be fetched.",
                              "fetching_captions", retryable=exc.retryable,
                              next_action="retry_or_request_asr") from exc
        if cancelled():
            raise IngestError("CANCELLED", "The operation was cancelled.", "fetching_captions")
        return parse_captions(payload, track), track.language

    def acquire(self, source, kind, output_dir: Path, quality, cancelled, on_bytes) -> Path:
        if kind not in ("video", "audio") or quality not in ("overview", "detail"):
            raise IngestError("INVALID_ARGUMENT", "Unsupported media acquisition mode.")
        output_dir = workspace_path(output_dir) / uuid.uuid4().hex
        output_dir.mkdir(parents=True, exist_ok=True)
        result = self._request(source, "acquire", cancelled=cancelled, on_bytes=on_bytes,
                               output_dir=str(output_dir), kind=kind, quality=quality)
        paths = [workspace_path(Path(name), exists=True) for name in result["paths"]]
        if not paths or any(not path.is_relative_to(output_dir) for path in paths):
            raise IngestError("UNSAFE_PATH", "The platform extractor returned an invalid artifact path.")
        if len(paths) == 1:
            return paths[0]
        merged = MediaProcessor(self.settings).merge_streams(paths[0], paths[1], output_dir / "media.mkv",
                                                             cancelled)
        for path in paths:
            path.unlink(missing_ok=True)
        return merged
