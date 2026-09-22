"""Local-only FFmpeg processing with timestamps from decoded presentation frames."""
from __future__ import annotations

import bisect
import json
import math
from pathlib import Path

from server.config import Settings
from server.contracts.models import TimeRange
from server.errors import IngestError
from server.processing.process import run_process, workspace_path

SAFE_FORMATS = "mov,matroska,webm,ogg,wav,mp3,flac,aac,mpegts,avi,asf"


class MediaProcessor:
    def __init__(self, settings: Settings):
        self.settings = settings

    def _run(self, args, cancelled=lambda: False, stage="sampling"):
        return run_process(args, timeout=self.settings.job_timeout_seconds,
                           max_file_bytes=self.settings.max_disk_bytes, cancelled=cancelled, stage=stage)

    @staticmethod
    def _input(path: Path):
        return ["-protocol_whitelist", "file,pipe", "-format_whitelist", SAFE_FORMATS,
                "-i", str(workspace_path(path, exists=True))]

    def probe(self, path: Path, cancelled=lambda: False) -> dict:
        raw = self._run(["ffprobe", "-v", "error", *self._input(path), "-show_format", "-show_streams",
                         "-of", "json"], cancelled=cancelled, stage="validating")
        data = json.loads(raw)
        video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), {})
        duration = data.get("format", {}).get("duration")
        if duration is None:
            durations = [float(s["duration"]) for s in data.get("streams", []) if s.get("duration") is not None]
            duration = max(durations, default=None)
        return {"duration_ms": round(float(duration) * 1000) if duration is not None else None,
                "width": video.get("width", 0),
                "height": video.get("height", 0),
                "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
                "start_time_ms": round(float(data.get("format", {}).get("start_time", 0)) * 1000)}

    @staticmethod
    def _range(time_range):
        if time_range is None:
            return []
        return ["-ss", f"{time_range.start_ms / 1000:.6f}", "-t",
                f"{(time_range.end_ms - time_range.start_ms) / 1000:.6f}"]

    def extract_audio(self, path: Path, output_path: Path, time_range=None, cancelled=lambda: False) -> Path:
        output_path = workspace_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", *self._input(path),
                   *self._range(time_range), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000",
                   "-c:a", "pcm_s16le", "-f", "wav", str(output_path)], cancelled, "transcribing")
        return output_path

    def make_clip(self, path: Path, output_path: Path, time_range: TimeRange,
                  cancelled=lambda: False) -> Path:
        output_path = workspace_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Re-encode after decoding: stream-copy seeks would silently start at an earlier keyframe.
        self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", *self._input(path),
                   *self._range(time_range), "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264",
                   "-preset", "veryfast", "-crf", "23", "-threads", "2", "-c:a", "aac",
                   "-movflags", "+faststart", "-f", "mp4", str(output_path)], cancelled, "sampling")
        return output_path

    def merge_streams(self, video: Path, audio: Path, output_path: Path, cancelled=lambda: False) -> Path:
        output_path = workspace_path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", *self._input(video), *self._input(audio),
                   "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-f", "matroska",
                   str(output_path)], cancelled, "downloading")
        return output_path

    def extract_frames(self, path: Path, output_dir: Path, time_range: TimeRange | None,
                       max_frames: int, quality: str, cancelled=lambda: False) -> list[dict]:
        if max_frames < 1 or max_frames > self.settings.overview_frames:
            raise IngestError("LIMIT_EXCEEDED", "Requested frame count exceeds the server limit.")
        if quality not in ("overview", "detail"):
            raise IngestError("INVALID_ARGUMENT", "Unsupported frame quality.")
        output_dir = workspace_path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        metadata = self.probe(path, cancelled=cancelled)
        start_ms = time_range.start_ms if time_range else 0
        end_ms = time_range.end_ms if time_range else metadata["duration_ms"]
        if end_ms is None:
            raise IngestError("DURATION_UNKNOWN", "Specify a time range when source duration is unknown.",
                              "sampling", next_action="specify_range")
        if quality == "detail" and end_ms - start_ms > self.settings.detail_max_ms:
            raise IngestError("LIMIT_EXCEEDED", "Detail sampling is limited to a short source range.",
                              next_action="reduce_scope")
        raw = self._run(["ffprobe", "-v", "error", *self._input(path), "-select_streams", "v:0",
                         "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                         "-of", "json"], cancelled)
        frames = json.loads(raw).get("frames", [])
        # Preserve original decoded frame indices; times are actual presentation times, never frame/fps.
        origin_ms = metadata["start_time_ms"]
        candidates = []
        for index, frame in enumerate(frames):
            if "best_effort_timestamp_time" not in frame:
                continue
            timestamp = round(float(frame["best_effort_timestamp_time"]) * 1000) - origin_ms
            if start_ms <= timestamp < end_ms:
                candidates.append((timestamp, index))
        if not candidates:
            raise IngestError("MEDIA_UNAVAILABLE", "There are no decoded video frames in this range.",
                              "sampling", next_action="change_range")
        times = [item[0] for item in candidates]
        targets = [start_ms + i * (end_ms - start_ms) / max_frames for i in range(max_frames)]
        chosen = []
        for target in targets:
            position = bisect.bisect_left(times, target)
            possible = candidates[max(0, position - 1):min(len(candidates), position + 1)]
            selected = min(possible, key=lambda item: abs(item[0] - target))
            if selected not in chosen:
                chosen.append(selected)
        chosen.sort()
        selection = "+".join(f"eq(n\\,{index})" for _, index in chosen)
        width = 1920 if quality == "detail" else 960
        # Fixed output dimensions simplify host byte budgeting. Never upscale small source images.
        scale = min(1.0, width / max(metadata["width"], 1))
        out_width = max(2, math.floor(metadata["width"] * scale / 2) * 2)
        out_height = max(2, math.floor(metadata["height"] * scale / 2) * 2)
        pattern = output_dir / "frame-%04d.jpg"
        self._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-threads", "2", *self._input(path),
                   "-vf", f"select={selection},scale={out_width}:{out_height}", "-filter_threads", "1",
                   "-vsync", "vfr", "-frames:v", str(len(chosen)), "-q:v", "2",
                   "-threads", "2", str(pattern)], cancelled)
        result = []
        for number, (timestamp, _) in enumerate(chosen, 1):
            image = output_dir / f"frame-{number:04d}.jpg"
            if not image.is_file():
                raise IngestError("DECODE_FAILED", "The decoder returned fewer frames than expected.", "sampling")
            result.append({"path": image, "timestamp_ms": timestamp, "width": out_width,
                           "height": out_height, "selection_reason": "uniform_source_pts_sample"})
        return result
