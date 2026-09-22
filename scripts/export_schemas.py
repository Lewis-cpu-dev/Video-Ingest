"""Export versioned public data contracts; run from the repository root."""
import json
from pathlib import Path

from server.contracts.models import (
    Artifact,
    ClientProfile,
    Evidence,
    FrameEvidence,
    Job,
    PrepareRequest,
    TimeRange,
    TranscriptSegment,
    VideoAsset,
    VideoSource,
)


def main():
    directory = Path(__file__).resolve().parents[1] / "server" / "contracts" / "schemas"
    directory.mkdir(exist_ok=True)
    for model in (VideoSource, VideoAsset, Artifact, Evidence, Job, ClientProfile, PrepareRequest,
                  TimeRange, TranscriptSegment, FrameEvidence):
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://video-ingest.invalid/schemas/1.0/{model.__name__}.json"
        (directory / f"{model.__name__}.json").write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
