"""Optional real, offline inference tests; provisioning is always a separate operator action."""
from __future__ import annotations

import importlib.util
import shutil

import pytest

from server.config import PROJECT_ROOT, Settings
from server.contracts.models import PrepareRequest, TimeRange, VideoMetadata
from server.errors import IngestError
from server.processing.media import MediaProcessor
from server.processing.process import run_process
from server.providers.asr import FasterWhisperASR
from server.service import VideoService

MODEL = PROJECT_ROOT / ".models" / "faster-whisper-tiny.en"
pytestmark = [pytest.mark.asr, pytest.mark.skipif(
    not (MODEL / "model.bin").is_file() or importlib.util.find_spec("faster_whisper") is None,
    reason="Requires explicitly provisioned local tiny.en model and optional ASR dependencies")]


@pytest.fixture(scope="module")
def speech_video(tmp_path_factory):
    directory = tmp_path_factory.mktemp("real-asr")
    audio, video = directory / "source.wav", directory / "source.mkv"
    text = "The purple triangle is beside the green square. The number is forty two."
    run_process(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                 f"flite=text={text}:voice=slt", "-af", "adelay=4000", "-ar", "16000", "-ac", "1", str(audio)],
                timeout=20, max_file_bytes=8 * 1024**2)
    run_process(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "color=c=blue:size=160x90:rate=5", "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
                 "-c:v", "libx264", "-threads", "2", "-c:a", "pcm_s16le", "-shortest", str(video)],
                timeout=20, max_file_bytes=8 * 1024**2)
    return audio, video


def test_real_vad_restores_leading_silence_and_supplied_offset(speech_video, tmp_path):
    audio, _ = speech_video
    settings = Settings(data_dir=tmp_path / "state", allow_asr=True, asr_model_path=str(MODEL))
    segments = FasterWhisperASR(settings).transcribe(audio, "en", 12000, lambda: False)
    transcript = " ".join(segment.text for segment in segments).lower()
    assert "purple triangle" in transcript and "green square" in transcript
    # Speech begins four seconds into this source, in addition to the explicit source chunk offset.
    assert abs(segments[0].start_ms - 16000) <= 1000
    assert all(segment.provenance == "asr" and segment.language == "en" for segment in segments)


def test_english_only_model_rejects_unsupported_language(speech_video, tmp_path):
    settings = Settings(data_dir=tmp_path / "state", allow_asr=True, asr_model_path=str(MODEL))
    with pytest.raises(IngestError) as caught:
        FasterWhisperASR(settings).transcribe(speech_video[0], "zh-CN", 0, lambda: False)
    assert caught.value.code == "LANGUAGE_UNSUPPORTED"


def test_real_silence_has_no_invented_words(tmp_path):
    audio = tmp_path / "silence.wav"
    run_process(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "anullsrc=r=16000:cl=mono", "-t", "3", str(audio)],
                timeout=10, max_file_bytes=1024**2)
    settings = Settings(data_dir=tmp_path / "state", allow_asr=True, asr_model_path=str(MODEL))
    assert FasterWhisperASR(settings).transcribe(audio, "en", 0, lambda: False) == []


def test_real_no_caption_service_chain(speech_video, tmp_path):
    _, video = speech_video
    settings = Settings(data_dir=tmp_path / "state", allow_asr=True, asr_model_path=str(MODEL))
    duration = MediaProcessor(settings).probe(video)["duration_ms"]

    class LocalFixtureSource:
        def inspect(self, source):
            return VideoMetadata(title="This title is deliberately unrelated to speech", duration_ms=duration)

        def fetch_captions(self, *args):
            raise IngestError("NO_CAPTIONS", "This self-authored local fixture contains no subtitle track.",
                              "fetching_captions")

        def acquire(self, source, kind, output_dir, quality, cancelled, on_bytes):
            path = output_dir / "source.mkv"
            on_bytes(video.stat().st_size)
            shutil.copyfile(video, path)
            return path

    service = VideoService(settings, provider=LocalFixtureSource(), start_worker=False)
    try:
        asset = service.resolve_video("fixture-owner", "https://www.youtube.com/watch?v=BaW_jenozKc")
        job = service.prepare_video("fixture-owner", asset["asset_id"],
                                    PrepareRequest(need=["transcript"], language="en",
                                                   time_range=TimeRange(start_ms=1000, end_ms=duration)))
        service.run_once()
        result = service.get_job("fixture-owner", job["job_id"])
        assert result["status"] == "succeeded", result
        page = service.read_transcript("fixture-owner", asset["asset_id"])
        text = " ".join(segment["text"] for segment in page["segments"]).lower()
        assert "purple triangle" in text and "green square" in text
        assert "unrelated" not in text
        assert abs(page["segments"][0]["start_ms"] - 4000) <= 1000
        assert page["segments"][0]["provenance"] == "asr"
        assert [0, 1000] in page["coverage"]["unprocessed_ranges_ms"]
    finally:
        service.close()
