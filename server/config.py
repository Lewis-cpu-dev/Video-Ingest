from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from server.contracts.models import ClientProfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / ".runtime")
    max_duration_ms: int = 120 * 60 * 1000
    max_download_bytes: int = 1024**3
    max_disk_bytes: int = 4 * 1024**3
    max_frames: int = 6
    overview_frames: int = 24
    detail_max_ms: int = 60_000
    ttl_seconds: int = 86_400
    job_timeout_seconds: int = 1800
    max_image_response_bytes: int = 8 * 1024**2
    max_transcript_chars: int = 32_000
    max_queued_jobs: int = 16
    max_assets: int = 128
    asr_model_path: str | None = None
    allow_asr: bool = False
    client_profile: ClientProfile = field(default_factory=ClientProfile)

    def __post_init__(self):
        root = self.data_dir.resolve()
        if not root.is_relative_to(PROJECT_ROOT) or root == PROJECT_ROOT:
            raise ValueError("data_dir must be a subdirectory of this workspace")
        object.__setattr__(self, "data_dir", root)
        for key in ("max_duration_ms", "max_download_bytes", "max_disk_bytes", "max_frames", "overview_frames",
                    "detail_max_ms", "ttl_seconds", "job_timeout_seconds", "max_image_response_bytes",
                    "max_transcript_chars", "max_queued_jobs", "max_assets"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.max_frames > 6:
            raise ValueError("max_frames cannot exceed the six-image protocol budget")

    @classmethod
    def from_env(cls):
        return cls(data_dir=Path(os.getenv("VIDEO_INGEST_DATA_DIR", str(PROJECT_ROOT / ".runtime"))),
                   asr_model_path=os.getenv("VIDEO_INGEST_ASR_MODEL"),
                   allow_asr=os.getenv("VIDEO_INGEST_ALLOW_ASR") == "1")
