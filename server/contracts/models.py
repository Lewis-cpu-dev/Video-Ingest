from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TimeRange(Contract):
    start_ms: int = Field(default=0, ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must exceed start_ms")
        return self


class VideoSource(Contract):
    platform: Literal["youtube", "bilibili"]
    canonical_url: str
    source_id: str
    part_id: str = "1"
    focus_timestamp_ms: int | None = None

    @property
    def identity(self) -> str:
        return f"{self.platform}:{self.source_id}:{self.part_id}"


class CaptionTrack(Contract):
    language: str
    provenance: Literal["human_caption", "platform_auto_caption", "unknown_caption"]
    format: str
    url: str


class VideoMetadata(Contract):
    title: str
    duration_ms: int | None = Field(default=None, ge=0)
    caption_tracks: list[CaptionTrack] = Field(default_factory=list)
    media_available: bool = True
    is_live: bool = False
    warnings: list[str] = Field(default_factory=list)


class TranscriptSegment(Contract):
    segment_id: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    language: str
    provenance: Literal["human_caption", "platform_auto_caption", "asr", "unknown_caption"]
    artifact_id: str = ""
    source_locator: str | None = None


class FrameEvidence(Contract):
    frame_id: str
    timestamp_ms: int = Field(ge=0)
    artifact_id: str
    selection_reason: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    asset_revision: str = "r1"
    source_locator: str | None = None
    image_content_index: int | None = Field(default=None, ge=0)


class ClientProfile(Contract):
    name: str = "unverified"
    text: bool = True
    images: bool = True
    file_resources: bool = False
    native_audio: bool = False
    native_video: bool = False
    verification: Literal["unverified", "local_protocol", "host_verified"] = "unverified"


class PrepareRequest(Contract):
    need: list[Literal["transcript", "visual_index", "audio", "video", "clip"]] = Field(min_length=1)
    time_range: TimeRange | None = None
    language: str | None = Field(default=None, max_length=32, pattern=r"^[a-zA-Z0-9_-]+$")
    quality: Literal["overview", "detail"] = "overview"

    @model_validator(mode="after")
    def validate_range(self):
        self.need = sorted(set(self.need))
        if "clip" in self.need and self.time_range is None:
            raise ValueError("clip requires time_range")
        return self


class Artifact(Contract):
    # Provider-specific evidence metadata is extensible; identity/lifecycle fields remain fixed.
    model_config = ConfigDict(extra="allow")
    artifact_id: str
    kind: Literal["video", "audio", "transcript", "frame", "visual_index", "clip"]
    status: Literal["missing", "processing", "ready", "failed", "expired"]
    mime_type: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    sha256: str | None = None
    expires_at: float | None = None


class Evidence(Contract):
    asset_id: str
    asset_revision: str
    timebase: Literal["source_presentation_ms"] = "source_presentation_ms"
    transcript: TranscriptSegment | None = None
    frame: FrameEvidence | None = None

    @model_validator(mode="after")
    def one_modality(self):
        if (self.transcript is None) == (self.frame is None):
            raise ValueError("evidence must contain exactly one transcript segment or frame")
        return self


class VideoAsset(Contract):
    schema_version: Literal["1.0"] = "1.0"
    asset_id: str
    asset_revision: str = "r1"
    source: VideoSource
    duration_ms: int | None = None
    expires_at: float
    metadata: dict = Field(default_factory=dict)
    capabilities: dict = Field(default_factory=dict)
    artifacts: dict = Field(default_factory=dict)
    timebase: Literal["source_presentation_ms"] = "source_presentation_ms"
    coverage: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    client_profile: ClientProfile = Field(default_factory=ClientProfile)


class Job(Contract):
    job_id: str
    asset_id: str
    status: Literal["queued", "running", "succeeded", "partial", "failed", "cancel_requested", "cancelled"]
    stage: Literal["resolving", "fetching_captions", "downloading", "transcribing", "sampling", "validating"]
    requested: PrepareRequest
    progress: dict | None = None
    errors: list[dict] = Field(default_factory=list)
    completed_outputs: list[str] = Field(default_factory=list)
    available_artifacts: list[dict] = Field(default_factory=list)
    usage: dict = Field(default_factory=dict)
    next_poll_after_ms: int | None = None
