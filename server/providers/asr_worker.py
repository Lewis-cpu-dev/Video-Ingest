from __future__ import annotations

import json
import os
import socket
import sys

from server.errors import IngestError


def perform(payload):
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", HF_DATASETS_OFFLINE="1")

    def deny_network(*args, **kwargs):
        raise IngestError("UNSAFE_URL", "Local transcription may not access the network.", "transcribing")

    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise IngestError("ASR_UNAVAILABLE", "The optional local ASR dependency is not installed.",
                          "transcribing", next_action="configure_local_asr") from exc
    model = WhisperModel(payload["model"], device="cpu", compute_type="int8", cpu_threads=2,
                         num_workers=1, local_files_only=True)
    language = payload["language"]
    if language:
        language = language.lower().replace("_", "-").split("-")[0]
        if language not in model.supported_languages:
            raise IngestError("LANGUAGE_UNSUPPORTED", "The provisioned ASR model does not support this language.",
                              "transcribing", next_action="configure_suitable_local_model")
    segments, info = model.transcribe(payload["audio_path"], language=language,
                                      beam_size=5, vad_filter=True)
    offset = payload["offset_ms"]
    result = []
    for index, segment in enumerate(segments):
        if not segment.text.strip():
            continue
        result.append({"segment_id": f"asr_{index:06d}", "start_ms": round(segment.start * 1000) + offset,
                       "end_ms": round(segment.end * 1000) + offset, "text": segment.text.strip(),
                       "language": info.language, "provenance": "asr", "artifact_id": ""})
    return {"segments": result}


def main():
    payload = json.loads(sys.stdin.buffer.read(1024**2))
    try:
        result = perform(payload)
    except IngestError as exc:
        result = {"error": exc.as_dict()}
    except Exception:  # noqa: BLE001 - never expose model internals, local paths or decoder text to clients.
        result = {"error": IngestError("ASR_FAILED", "The local transcription process failed.", "transcribing",
                                        next_action="check_local_model").as_dict()}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
