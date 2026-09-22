from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import json
import mimetypes
import shutil
import threading
import time
from importlib.metadata import version
from pathlib import Path

from server.config import Settings
from server.contracts.models import PrepareRequest, TimeRange, TranscriptSegment, VideoMetadata, VideoSource
from server.errors import IngestError
from server.storage.store import Store, encode, new_id

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}
PROCESSING_VERSION = "video-ingest-0.1.0/" + version("yt-dlp")


def merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
    merged: list[list[int]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def missing_ranges(start: int, end: int, processed: list[list[int]]) -> list[list[int]]:
    missing, position = [], start
    for a, b in merge_ranges(processed):
        if b <= position or a >= end:
            continue
        if a > position:
            missing.append([position, min(a, end)])
        position = min(end, max(position, b))
    if position < end:
        missing.append([position, end])
    return missing


def public_artifact(artifact: dict) -> dict:
    return {"artifact_id": artifact["id"], "kind": artifact["kind"], "status": "ready",
            "mime_type": artifact["mime"], "size_bytes": artifact["size"],
            "sha256": artifact["sha256"], "expires_at": artifact.get("expires_at"), **artifact["details"]}


class VideoService:
    def __init__(self, settings: Settings | None = None, provider=None, processor=None, asr=None,
                 start_worker: bool = True):
        self.settings = settings or Settings.from_env()
        self.store = Store(self.settings)
        self._worker_lock = (self.store.root / "worker.lock").open("a+")
        try:
            fcntl.flock(self._worker_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self._worker_lock.close()
            self.store.close()
            raise RuntimeError("This data directory already has a worker; use exactly one server process.") from None
        if provider is None:
            from server.providers.ytdlp import YtDlpProvider
            provider = YtDlpProvider(self.settings)
        if processor is None:
            from server.processing.media import MediaProcessor
            processor = MediaProcessor(self.settings)
        self.provider, self.processor, self.asr = provider, processor, asr
        self._resolve_lock = threading.Lock()
        self._stopping, self._wake = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self.store.recover()
        self.store.purge_revoked()
        self.store.prune_temporary_files()
        if start_worker:
            self._thread = threading.Thread(target=self._loop, name="video-ingest-worker", daemon=True)
            self._thread.start()

    def close(self):
        self._stopping.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                raise RuntimeError("Worker has not stopped; data directory remains locked.")
        self.store.close()
        fcntl.flock(self._worker_lock, fcntl.LOCK_UN)
        self._worker_lock.close()

    def resolve_video(self, owner: str, url: str, part: int | None = None) -> dict:
        from server.adapters.platforms import normalize_url
        source = normalize_url(url, part)
        asset = self.store.find_asset(owner, source.identity)
        if asset is None:
            if not self._resolve_lock.acquire(blocking=False):
                raise IngestError("RATE_LIMITED", "A metadata lookup is already running.", stage="resolving",
                                  retryable=True, retry_after_ms=2000, next_action="retry_later")
            try:
                self.store.check_asset_capacity(owner)
                metadata = self.provider.inspect(source)
                if metadata.is_live:
                    raise IngestError("UNSUPPORTED_URL", "Live streams are outside the supported scope.", stage="resolving")
                if metadata.duration_ms is not None and metadata.duration_ms > self.settings.max_duration_ms:
                    raise IngestError("LIMIT_EXCEEDED", "Video exceeds the configured duration limit.", stage="resolving")
                asset = self.store.create_asset(owner, source, metadata)
                self.store.record_usage(owner, asset["id"], "metadata_requests", 1)
            finally:
                self._resolve_lock.release()
        result = self.manifest(owner, asset["id"])
        result["source"]["focus_timestamp_ms"] = source.focus_timestamp_ms
        return result

    def manifest(self, owner: str, asset_id: str) -> dict:
        asset = self.store.asset(owner, asset_id)
        metadata = VideoMetadata.model_validate(asset["metadata"])
        arts = self.store.artifacts(owner, asset_id)
        kinds = {a["kind"] for a in arts}
        return {"schema_version": "1.0", "asset_id": asset_id, "asset_revision": asset["revision"],
                "source": asset["source"], "metadata": {"title": metadata.title,
                    "caption_tracks": [{"language": t.language, "provenance": t.provenance} for t in metadata.caption_tracks]},
                "duration_ms": metadata.duration_ms, "timebase": "source_presentation_ms",
                "capabilities": {"captions": bool(metadata.caption_tracks), "media": metadata.media_available,
                                 "asr_configured": bool(self.settings.allow_asr and (self.asr or self.settings.asr_model_path))},
                "artifacts": {kind: {"status": "ready" if kind in kinds else "missing",
                                    "artifact_ids": [a["id"] for a in arts if a["kind"] == kind]}
                              for kind in ("transcript", "video", "audio", "visual_index", "clip")},
                "coverage": {"transcript_processed_ranges_ms": merge_ranges([r for a in arts if a["kind"] == "transcript"
                                for r in a["details"].get("processed_ranges_ms", [])]),
                             "visual_sampled_timestamps_ms": sorted({a["details"]["timestamp_ms"] for a in arts if a["kind"] == "frame"}),
                             "visual_exhaustive": False},
                "warnings": metadata.warnings + (["VISUALS_ARE_SPARSE_SAMPLES"] if "visual_index" in kinds else []),
                "expires_at": asset["expires"], "usage": self.store.usage(owner, asset_id),
                "client_profile": self.settings.client_profile.model_dump()}

    def _range(self, asset: dict, time_range: TimeRange | None) -> TimeRange:
        duration = asset["metadata"].get("duration_ms")
        if time_range:
            if duration is not None and time_range.end_ms > duration:
                raise IngestError("INVALID_RANGE", "Requested range exceeds video duration.")
            if time_range.end_ms > self.settings.max_duration_ms:
                raise IngestError("LIMIT_EXCEEDED", "Requested range exceeds the duration limit.")
            return time_range
        if duration is None:
            raise IngestError("PROCESSING_REQUIRED", "Duration is unknown; prepare video or audio to probe the timeline.",
                              next_action="prepare_video")
        if duration <= 0:
            raise IngestError("MEDIA_UNAVAILABLE", "Video has no measurable presentation duration.")
        return TimeRange(end_ms=duration)

    def prepare_video(self, owner: str, asset_id: str, request: PrepareRequest) -> dict:
        asset = self.store.asset(owner, asset_id)
        self.store.reconcile_artifacts(owner, asset_id)
        if request.time_range:
            self._range(asset, request.time_range)
        if request.quality == "detail" and "visual_index" in request.need:
            target = self._range(asset, request.time_range)
            if target.end_ms - target.start_ms > self.settings.detail_max_ms:
                raise IngestError("LIMIT_EXCEEDED", "Detail sampling requires a range of at most 60 seconds.",
                                  next_action="narrow_time_range")
        key = hashlib.sha256(encode({"owner": owner, "asset_id": asset_id, "identity": asset["identity"],
                                    "revision": asset["revision"], "request": request.model_dump(),
                                    "provider_version": PROCESSING_VERSION,
                                    "asr": self.settings.asr_model_path,
                                    "sampling": self.settings.overview_frames}).encode()).hexdigest()
        job, reused = self.store.enqueue(owner, asset_id, key, request.model_dump())
        self._wake.set()
        return {**self.get_job(owner, job["id"]), "reused": reused,
                "budget": {"max_duration_ms": self.settings.max_duration_ms,
                           "remaining_transfer_bytes": max(0, self.settings.max_download_bytes - asset["download_bytes"]),
                           "external_paid_asr": False, "upstream_transfer_may_cover_full_video": True}}

    def get_job(self, owner: str, job_id: str) -> dict:
        job = self.store.job(owner, job_id)
        self.store.asset(owner, job["asset_id"], include_deleted=True)
        try:
            arts = self.store.artifacts(owner, job["asset_id"])
        except IngestError:
            arts = []
        return {"job_id": job_id, "asset_id": job["asset_id"], "status": job["status"],
                "stage": job["stage"], "requested": job["request"], "completed_outputs": job["completed"],
                "errors": job["errors"], "progress": None,
                "available_artifacts": [public_artifact(a) for a in arts if a["kind"] != "frame"],
                "usage": self.store.usage(owner, job["asset_id"]),
                "next_poll_after_ms": 2000 if job["status"] not in TERMINAL else None}

    def cancel_job(self, owner: str, job_id: str) -> dict:
        self.store.cancel(owner, job_id)
        self._wake.set()
        return self.get_job(owner, job_id)

    def delete_asset(self, owner: str, asset_id: str) -> dict:
        self.store.revoke(owner, asset_id)
        self.store.purge_revoked()
        self._wake.set()
        pending = self.store.asset_dir(asset_id).exists()
        return {"asset_id": asset_id, "access_revoked": True,
                "deletion_status": "pending_worker_shutdown" if pending else "deleted",
                "retained": ["opaque job IDs, request parameters, usage and status", "evidence already delivered to the conversation"],
                "next_action": "delete_asset" if pending else None}

    def _loop(self):
        while not self._stopping.is_set():
            self.store.purge_revoked()
            job = self.store.claim_next()
            if job:
                self._execute(job)
            else:
                self._wake.wait(1)
                self._wake.clear()

    def run_once(self) -> bool:
        """Deterministic worker entry point for offline tests and administrative use."""
        job = self.store.claim_next()
        if job is None:
            return False
        self._execute(job)
        return True

    def _execute(self, job: dict):
        owner, asset_id, job_id = job["owner"], job["asset_id"], job["id"]
        completed, errors = [], []
        started = time.monotonic()
        request = PrepareRequest.model_validate(job["request"])
        directory = self.store.asset_dir(asset_id) / job_id

        def cancelled():
            if self._stopping.is_set():
                return True
            if time.monotonic() - started > self.settings.job_timeout_seconds:
                raise IngestError("TASK_TIMEOUT", "Task exceeded its processing time budget.",
                                  retryable=True, next_action="narrow_time_range")
            try:
                self.store.asset(owner, asset_id)
            except IngestError:
                return True
            return self.store.job(owner, job_id)["status"] == "cancel_requested"

        def check():
            if cancelled():
                raise IngestError("CANCELLED", "Task cancellation was requested.", next_action="use_available_evidence")

        def stage(value):
            check()
            self.store.update_job(job_id, stage=value, completed=completed, errors=errors)

        def download(kind):
            available = [a for a in self.store.artifacts(owner, asset_id, kind)
                         if a["details"].get("full_source") and
                         (kind != "video" or request.quality == "overview" or a["details"].get("quality") == "detail")]
            if available:
                return self.store.artifact_path(owner, available[-1]["id"])
            stage("downloading")
            if self.store.usage(owner, asset_id)["download_bytes"] >= self.settings.max_download_bytes:
                raise IngestError("BUDGET_EXCEEDED", "Media transfer budget is exhausted.", stage="downloading",
                                  next_action="use_available_evidence")
            source = VideoSource.model_validate(self.store.asset(owner, asset_id)["source"])
            self.store.record_usage(owner, asset_id, "media_acquisitions", 1)
            path = self.provider.acquire(source, kind, directory, request.quality, cancelled,
                                         lambda count: self.store.charge_bytes(owner, asset_id, count))
            check()
            probe = self.processor.probe(path)
            duration = probe.get("duration_ms")
            if duration is not None and duration > self.settings.max_duration_ms:
                raise IngestError("LIMIT_EXCEEDED", "Decoded duration exceeds configured limit.", stage="validating")
            if duration and self.store.asset(owner, asset_id)["metadata"].get("duration_ms") is None:
                with self.store.transaction():
                    asset = self.store._asset(owner, asset_id)
                    asset["metadata"]["duration_ms"] = duration
                    self.store.db.execute("UPDATE assets SET metadata=? WHERE id=?", (encode(asset["metadata"]), asset_id))
            mime = mimetypes.guess_type(path.name)[0] or ("video/mp4" if kind == "video" else "audio/mp4")
            self.store.add_artifact(owner, asset_id, kind, path, mime,
                                    {"source_offset_ms": 0, "duration_ms": duration, "full_source": True,
                                     "quality": request.quality})
            return path

        def process_transcript():
            asset = self.store.asset(owner, asset_id)
            metadata, source = VideoMetadata.model_validate(asset["metadata"]), VideoSource.model_validate(asset["source"])
            existing = self.store.artifacts(owner, asset_id, "transcript")
            target = request.time_range
            for art in existing:
                details = art["details"]
                if request.language and details.get("language") != request.language:
                    continue
                if target is None and details.get("full_source"):
                    return
                if target and not missing_ranges(target.start_ms, target.end_ms, details.get("processed_ranges_ms", [])):
                    return
            stage("fetching_captions")
            self.store.record_usage(owner, asset_id, "caption_requests", 1)
            used_asr = False
            try:
                segments, language = self.provider.fetch_captions(source, metadata, request.language, cancelled)
                duration = metadata.duration_ms
                processed = [[0, duration]] if duration else []
            except IngestError as error:
                if error.code != "NO_CAPTIONS":
                    raise
                if not self.settings.allow_asr:
                    raise IngestError("NO_CAPTIONS", "No suitable caption track; local ASR is not enabled.",
                                      stage="fetching_captions", next_action="configure_local_asr") from None
                audio = download("audio")
                target = self._range(self.store.asset(owner, asset_id), request.time_range)
                stage("transcribing")
                self.store.record_usage(owner, asset_id, "asr_reserved_ms", target.end_ms - target.start_ms,
                                        limit=self.settings.max_duration_ms)
                wav = self.processor.extract_audio(audio, directory / "asr.wav", target, cancelled=cancelled)
                if self.asr is None:
                    from server.providers.asr import FasterWhisperASR
                    self.asr = FasterWhisperASR(self.settings)
                segments = self.asr.transcribe(wav, request.language, target.start_ms, cancelled)
                self.store.record_usage(owner, asset_id, "asr_completed_ms", target.end_ms - target.start_ms)
                language = segments[0].language if segments else (request.language or "und")
                processed = [[target.start_ms, target.end_ms]]
                used_asr = True
            check()
            artifact_id = new_id("art")
            normalized = []
            for segment in segments:
                segment = TranscriptSegment.model_validate(segment)
                if segment.end_ms < segment.start_ms:
                    raise IngestError("DECODE_FAILED", "Transcript segment has invalid time bounds.")
                segment.artifact_id = artifact_id
                segment.segment_id = "seg_" + hashlib.sha256(encode([asset["identity"], segment.start_ms,
                    segment.end_ms, segment.text, segment.language, segment.provenance]).encode()).hexdigest()[:24]
                normalized.append(segment.model_dump())
            path = directory / (artifact_id + ".json")
            path.write_text(encode(normalized), encoding="utf-8")
            self.store.add_artifact(owner, asset_id, "transcript", path, "application/json",
                {"processed_ranges_ms": processed, "language": language,
                 "full_source": not used_asr or request.time_range is None,
                 "provenance": "asr" if used_asr else (normalized[0]["provenance"] if normalized else "unknown_caption"),
                 "warnings": [] if processed else ["PROCESSING_RANGE_UNKNOWN"]}, artifact_id=artifact_id)

        # Captions first means a media failure still leaves useful, truthful partial results.
        ordered = sorted(request.need, key=lambda n: {"transcript": 0, "video": 1, "audio": 2, "visual_index": 3, "clip": 4}[n])
        cancelled_job = False
        try:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            for need in ordered:
                try:
                    check()
                    if need == "transcript":
                        process_transcript()
                    elif need == "video":
                        download("video")
                    elif need == "audio":
                        audio = download("audio")
                        if request.time_range:
                            stage("sampling")
                            path = self.processor.extract_audio(audio, directory / "audio-range.wav",
                                                                request.time_range, cancelled=cancelled)
                            self.store.add_artifact(owner, asset_id, "audio", path, "audio/wav",
                                {"range_ms": [request.time_range.start_ms, request.time_range.end_ms],
                                 "source_offset_ms": request.time_range.start_ms, "full_source": False})
                    elif need == "visual_index":
                        video = download("video")
                        target = self._range(self.store.asset(owner, asset_id), request.time_range)
                        stage("sampling")
                        frames = self.processor.extract_frames(video, directory / "frames", target,
                            self.settings.overview_frames, request.quality, cancelled=cancelled)
                        ids = []
                        for frame in frames:
                            check()
                            path = Path(frame.pop("path"))
                            fid = new_id("fr")
                            details = {**frame, "frame_id": fid, "asset_revision": "r1"}
                            art = self.store.add_artifact(owner, asset_id, "frame", path, "image/jpeg", details)
                            ids.append(art["id"])
                        if not ids:
                            raise IngestError("DECODE_FAILED", "No video frames decoded in the requested range.", stage="sampling")
                        index = directory / "visual_index.json"
                        index.write_text(encode(ids), encoding="utf-8")
                        self.store.add_artifact(owner, asset_id, "visual_index", index, "application/json",
                            {"range_ms": [target.start_ms, target.end_ms], "quality": request.quality,
                             "frame_artifact_ids": ids, "visual_exhaustive": False})
                    elif need == "clip":
                        video = download("video")
                        stage("sampling")
                        path = self.processor.make_clip(video, directory / "clip.mp4", request.time_range, cancelled=cancelled)
                        self.store.add_artifact(owner, asset_id, "clip", path, "video/mp4",
                            {"range_ms": [request.time_range.start_ms, request.time_range.end_ms],
                             "source_offset_ms": request.time_range.start_ms})
                    completed.append(need)
                    self.store.update_job(job_id, completed=completed)
                except IngestError as error:
                    errors.append(error.as_dict())
                    if error.code == "CANCELLED" or cancelled():
                        cancelled_job = True
                        break
                    if error.code in {"TASK_TIMEOUT", "BUDGET_EXCEEDED", "LIMIT_EXCEEDED"}:
                        break
                except Exception:  # noqa: BLE001 - provider boundary must preserve partial evidence
                    errors.append(IngestError("INTERNAL_ERROR", "Processing failed; provider details were withheld.",
                                              retryable=False, next_action="inspect_server_logs").as_dict())
            if cancelled():
                cancelled_job = True
        except IngestError as error:
            errors.append(error.as_dict())
            cancelled_job = error.code == "CANCELLED"
        except OSError:
            errors.append(IngestError("STORAGE_UNAVAILABLE", "Private task storage could not be written.",
                                      retryable=True, next_action="check_deployment").as_dict())
        finally:
            status = "cancelled" if cancelled_job else ("succeeded" if set(completed) == set(request.need)
                else "partial" if completed else "failed")
            self.store.update_job(job_id, status=status, stage="validating", completed=completed, errors=errors)
            self.store.record_usage(owner, asset_id, "processing_elapsed_ms", round((time.monotonic() - started) * 1000))
            # Drop unregistered temporary bytes but preserve every committed artifact.
            try:
                keep = {self.store.artifact_path(owner, a["id"]) for a in self.store.artifacts(owner, asset_id)}
                for path in directory.rglob("*"):
                    if path.is_file() and path.resolve() not in keep:
                        path.unlink(missing_ok=True)
            except (IngestError, OSError):
                shutil.rmtree(directory, ignore_errors=True)
            self.store.purge_revoked()

    def _cursor(self, payload: dict) -> str:
        raw = encode(payload).encode()
        signature = hmac.digest(self.store.cursor_secret, raw, "sha256")
        return base64.urlsafe_b64encode(raw + signature).decode().rstrip("=")

    def _offset(self, cursor: str | None, fingerprint: str) -> int:
        if cursor is None:
            return 0
        try:
            if len(cursor) > 4096:
                raise ValueError
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            message, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self.store.cursor_secret, message, "sha256")):
                raise ValueError
            payload = json.loads(message)
            if payload["fingerprint"] != fingerprint or type(payload["offset"]) is not int or payload["offset"] < 0:
                raise ValueError
            return payload["offset"]
        except (ValueError, KeyError, TypeError):
            raise IngestError("INVALID_CURSOR", "Cursor is invalid or evidence changed; restart this read.",
                              next_action="restart_read") from None

    def read_transcript(self, owner: str, asset_id: str, time_range: TimeRange | None = None,
                        cursor: str | None = None, limit: int = 100, language: str | None = None) -> dict:
        if not 1 <= limit <= 200:
            raise IngestError("LIMIT_EXCEEDED", "Transcript page size must be between 1 and 200.")
        asset = self.store.asset(owner, asset_id)
        arts = self.store.artifacts(owner, asset_id, "transcript")
        if language:
            arts = [a for a in arts if a["details"].get("language") == language]
        elif arts:
            language = arts[-1]["details"].get("language")
            arts = [a for a in arts if a["details"].get("language") == language]
        if not arts:
            raise IngestError("PROCESSING_REQUIRED", "Transcript has not been prepared for this language.", next_action="prepare_video")
        processed = merge_ranges([r for a in arts for r in a["details"].get("processed_ranges_ms", [])])
        segments_by_id = {}
        for art in arts:
            segments = json.loads(self.store.artifact_path(owner, art["id"]).read_text(encoding="utf-8"))
            for segment in segments:
                segments_by_id[segment["segment_id"]] = segment
        all_segments = sorted(segments_by_id.values(), key=lambda s: (s["start_ms"], s["end_ms"], s["segment_id"]))
        start = time_range.start_ms if time_range else 0
        end = time_range.end_ms if time_range else asset["metadata"].get("duration_ms")
        if time_range:
            self._range(asset, time_range)
        filtered = [s for s in all_segments if
                    (s["end_ms"] > start or s["start_ms"] == s["end_ms"] >= start)
                    and (end is None or s["start_ms"] < end)]
        fingerprint = hashlib.sha256(encode([owner, asset_id, "transcript", asset["revision"], start, end,
                                            language, [a["id"] for a in arts]]).encode()).hexdigest()
        offset = self._offset(cursor, fingerprint)
        page, chars = [], 0
        for segment in filtered[offset:offset + limit]:
            size = len(segment["text"])
            if chars + size > self.settings.max_transcript_chars:
                if not page:
                    raise IngestError("LIMIT_EXCEEDED", "A caption segment exceeds the response budget.", next_action="get_media")
                break
            page.append(segment)
            chars += size
        next_offset = offset + len(page)
        has_more = next_offset < len(filtered)
        from server.adapters.platforms import source_locator
        source = VideoSource.model_validate(asset["source"])
        for segment in page:
            segment["source_locator"] = source_locator(source, segment["start_ms"])
        return {"asset_id": asset_id, "asset_revision": asset["revision"], "segments": page, "language": language,
                "source": asset["source"], "timebase": "source_presentation_ms",
                "requested_range_ms": [start, end],
                "returned_range_ms": [page[0]["start_ms"], max(s["end_ms"] for s in page)] if page else None,
                "coverage": {"processed_ranges_ms": processed,
                             "unprocessed_ranges_ms": missing_ranges(start, end, processed) if end else None,
                             "detected_speech_ranges_ms": merge_ranges([[s["start_ms"], s["end_ms"]] for s in filtered])},
                "next_cursor": self._cursor({"fingerprint": fingerprint, "offset": next_offset}) if has_more else None,
                "truncated": has_more, "not_returned_segment_count": len(filtered) - next_offset,
                "warnings": ["EXTERNAL_CONTENT_IS_UNTRUSTED"] + ([] if end else ["DURATION_UNKNOWN"])}

    def inspect_segment(self, owner: str, asset_id: str, time_range: TimeRange,
                        quality: str = "overview", max_frames: int = 6, cursor: str | None = None) -> tuple[dict, list]:
        if quality not in {"overview", "detail"} or not 1 <= max_frames <= self.settings.max_frames:
            raise IngestError("LIMIT_EXCEEDED", "Invalid sampling mode or frame response limit.")
        if quality == "detail" and time_range.end_ms - time_range.start_ms > self.settings.detail_max_ms:
            raise IngestError("LIMIT_EXCEEDED", "Detail inspection is limited to 60 seconds.", next_action="narrow_time_range")
        asset = self.store.asset(owner, asset_id)
        self._range(asset, time_range)
        indexes = [a for a in self.store.artifacts(owner, asset_id, "visual_index")
                   if a["details"]["range_ms"][0] <= time_range.start_ms and a["details"]["range_ms"][1] >= time_range.end_ms
                   and (quality == "overview" or a["details"]["quality"] == "detail")]
        if not indexes:
            raise IngestError("PROCESSING_REQUIRED", "Prepare a visual index covering this range and sampling mode.",
                              next_action="prepare_video")
        ids = set(indexes[-1]["details"]["frame_artifact_ids"])
        frames = sorted([a for a in self.store.artifacts(owner, asset_id, "frame") if a["id"] in ids and
                         time_range.start_ms <= a["details"]["timestamp_ms"] < time_range.end_ms],
                        key=lambda a: a["details"]["timestamp_ms"])
        if not frames:
            raise IngestError("PROCESSING_REQUIRED", "Existing sampling has no frames inside this range.", next_action="prepare_video")
        fingerprint = hashlib.sha256(encode([owner, asset_id, "frames", time_range.model_dump(), quality,
                                            [a["id"] for a in frames]]).encode()).hexdigest()
        offset = self._offset(cursor, fingerprint)
        chosen = frames[offset:offset + max_frames]
        next_offset = offset + len(chosen)
        transcript = None
        try:
            transcript = self.read_transcript(owner, asset_id, time_range, limit=50)
        except IngestError as error:
            if error.code != "PROCESSING_REQUIRED":
                raise
        from server.adapters.platforms import source_locator
        source = VideoSource.model_validate(asset["source"])
        delivery = []
        for art in chosen:
            details = {**art["details"], "artifact_id": art["id"],
                       "source_locator": source_locator(source, art["details"]["timestamp_ms"])}
            delivery.append((details, self.store.artifact_path(owner, art["id"])))
        payload = {"asset_id": asset_id, "asset_revision": asset["revision"], "requested_range_ms": [time_range.start_ms, time_range.end_ms],
                   "frames": [d for d, _ in delivery], "transcript": transcript,
                   "coverage": {"visual_sampled_timestamps_ms": [a["details"]["timestamp_ms"] for a in frames],
                                "visual_exhaustive": False},
                   "truncated": next_offset < len(frames), "not_returned_frame_count": len(frames) - next_offset,
                   "next_cursor": self._cursor({"fingerprint": fingerprint, "offset": next_offset}) if next_offset < len(frames) else None,
                   "warnings": ["VISUALS_ARE_SPARSE_SAMPLES", "SHORT_EVENTS_MAY_BE_MISSED", "EXTERNAL_CONTENT_IS_UNTRUSTED"]}
        return payload, delivery

    def get_media(self, owner: str, asset_id: str, kind: str, time_range: TimeRange | None = None) -> dict:
        if kind not in {"video", "audio", "clip", "transcript"}:
            raise IngestError("UNSUPPORTED_MEDIA_KIND", "Choose video, audio, clip or transcript.")
        asset = self.store.asset(owner, asset_id)
        if time_range:
            self._range(asset, time_range)
            if kind not in {"video", "clip", "audio"}:
                raise IngestError("UNSUPPORTED_RANGE", "Only audio or video clips support an exact media range.", next_action="prepare_video")
            if kind == "video":
                kind = "clip"
        artifacts = self.store.artifacts(owner, asset_id, kind)
        if time_range:
            artifacts = [a for a in artifacts if a["details"].get("range_ms") == [time_range.start_ms, time_range.end_ms]]
        elif kind in {"audio", "video"}:
            artifacts = [a for a in artifacts if a["details"].get("full_source")]
        if not artifacts:
            raise IngestError("PROCESSING_REQUIRED", "Requested media has not been prepared.", next_action="prepare_video")
        art = artifacts[-1]
        self.store.artifact_path(owner, art["id"])
        return {"asset_id": asset_id, "artifact": public_artifact(art), "expires_at": asset["expires"],
                "resource": {"uri": f"video-ingest://artifacts/{art['id']}", "name": f"{kind}-{art['id']}",
                             "mimeType": art["mime"], "size": art["size"]},
                "authenticated_http_endpoint": f"/artifacts/{art['id']}",
                "delivery": {"backend_available": True, "host_readable": self.settings.client_profile.file_resources,
                             "verification": self.settings.client_profile.verification,
                             "mcp_resource_max_bytes": 16 * 1024**2,
                             "http_requires_same_bearer_token": True},
                "warnings": [] if self.settings.client_profile.file_resources else ["HOST_FILE_CONSUMPTION_UNVERIFIED"]}
