"""Optional local ASR. Model provisioning and downloading are never implicit."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from server.config import Settings
from server.contracts.models import TranscriptSegment
from server.errors import IngestError
from server.processing.process import run_process, workspace_path


class FasterWhisperASR:
    def __init__(self, settings: Settings):
        self.settings = settings

    def transcribe(self, audio_path, language, offset_ms, cancelled):
        if not self.settings.allow_asr or not self.settings.asr_model_path:
            raise IngestError("ASR_UNAVAILABLE", "Local ASR requires an explicitly provisioned model.",
                              "transcribing", next_action="configure_local_asr")
        model = workspace_path(Path(self.settings.asr_model_path))
        if not model.is_dir() or not (model / "model.bin").is_file():
            raise IngestError("ASR_UNAVAILABLE", "The configured local ASR model is not provisioned.",
                              "transcribing", next_action="configure_local_asr")
        payload = {"audio_path": str(workspace_path(audio_path, exists=True)), "model": str(model),
                   "language": language, "offset_ms": offset_ms}
        raw = run_process([sys.executable, "-m", "server.providers.asr_worker"],
                          timeout=self.settings.job_timeout_seconds,
                          max_file_bytes=self.settings.max_disk_bytes, cancelled=cancelled,
                          input_data=json.dumps(payload).encode(), stage="transcribing")
        data = json.loads(raw)
        if "error" in data:
            raise IngestError(**data["error"])
        return [TranscriptSegment.model_validate(item) for item in data["segments"]]
