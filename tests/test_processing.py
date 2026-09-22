from __future__ import annotations

import sys
import time
import wave

import pytest

from server.config import PROJECT_ROOT, Settings
from server.contracts.models import TimeRange
from server.errors import IngestError
from server.processing.media import MediaProcessor
from server.processing.process import run_process, workspace_path


@pytest.fixture(scope="module")
def vfr_video(tmp_path_factory):
    path = tmp_path_factory.mktemp("vfr") / "source.mkv"
    run_process(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                 "testsrc=duration=3:size=160x90:rate=10", "-f", "lavfi", "-i",
                 "sine=frequency=431:sample_rate=16000:duration=3", "-map", "0:v:0", "-map", "1:a:0",
                 "-vf", "select=eq(n\\,0)+eq(n\\,2)+eq(n\\,9)+eq(n\\,17)+eq(n\\,25)",
                 "-vsync", "vfr", "-c:v", "ffv1", "-c:a", "pcm_s16le", "-threads", "2",
                 str(path)], timeout=20, max_file_bytes=8 * 1024**2)
    return path


def test_real_vfr_frames_use_decoded_pts(vfr_video, tmp_path):
    processor = MediaProcessor(Settings())
    frames = processor.extract_frames(vfr_video, tmp_path / "frames", TimeRange(end_ms=2600), 5,
                                       "overview")
    # Uniform requests at 0,520,1040,1560,2080 select these actual VFR presentation times.
    assert [frame["timestamp_ms"] for frame in frames] == [0, 200, 900, 1700]
    assert all(frame["path"].read_bytes().startswith(b"\xff\xd8") for frame in frames)
    assert all((frame["width"], frame["height"]) == (160, 90) for frame in frames)
    detail = processor.extract_frames(vfr_video, tmp_path / "detail",
                                       TimeRange(start_ms=800, end_ms=2600), 3, "detail")
    assert [frame["timestamp_ms"] for frame in detail] == [900, 1700]


def test_audio_range_is_exact_source_slice(vfr_video, tmp_path):
    processor = MediaProcessor(Settings())
    full = processor.extract_audio(vfr_video, tmp_path / "full.wav")
    part = processor.extract_audio(vfr_video, tmp_path / "part.wav", TimeRange(start_ms=750, end_ms=1750))
    with wave.open(str(full), "rb") as source, wave.open(str(part), "rb") as clip:
        source.setpos(12000)
        assert clip.getnframes() == 16000
        assert source.readframes(16000) == clip.readframes(16000)


def test_reencoded_clip_duration_and_dimensions(vfr_video, tmp_path):
    processor = MediaProcessor(Settings())
    clip = processor.make_clip(vfr_video, tmp_path / "clip.mp4", TimeRange(start_ms=500, end_ms=2000))
    metadata = processor.probe(clip)
    assert abs(metadata["duration_ms"] - 1500) <= 150
    assert metadata["width"] == 160 and metadata["height"] == 90
    assert metadata["has_audio"]


def test_merge_local_audio_video(vfr_video, tmp_path):
    processor = MediaProcessor(Settings())
    audio = processor.extract_audio(vfr_video, tmp_path / "audio.wav")
    merged = processor.merge_streams(vfr_video, audio, tmp_path / "merged.mkv")
    assert processor.probe(merged)["has_audio"]


def test_processor_rejects_playlist_demuxer(tmp_path):
    media = tmp_path / "untrusted.mp4"
    media.write_text("#EXTM3U\n#EXTINF:10,\nhttp://127.0.0.1/private\n")
    with pytest.raises(IngestError) as caught:
        MediaProcessor(Settings()).probe(media)
    assert caught.value.code == "DECODE_FAILED"


def test_workspace_path_rejects_parent():
    with pytest.raises(IngestError) as caught:
        workspace_path(PROJECT_ROOT.parent / "outside.wav")
    assert caught.value.code == "UNSAFE_PATH"


def test_unknown_duration_remains_unknown(monkeypatch, tmp_path):
    file = tmp_path / "undated.mp4"
    file.write_bytes(b"fixture")
    processor = MediaProcessor(Settings())
    monkeypatch.setattr(processor, "_run", lambda *args, **kwargs: b'{"streams":[],"format":{}}')
    assert processor.probe(file)["duration_ms"] is None


def test_process_cancellation_and_deadline():
    started = time.monotonic()
    with pytest.raises(IngestError) as caught:
        run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=5,
                    max_file_bytes=1024, cancelled=lambda: time.monotonic() - started > 0.2)
    assert caught.value.code == "CANCELLED"
    assert time.monotonic() - started < 2
    with pytest.raises(IngestError) as caught:
        run_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.3,
                    max_file_bytes=1024)
    assert caught.value.code == "TIMEOUT"


def test_child_cannot_write_over_file_limit(tmp_path):
    file = tmp_path / "bounded.bin"
    code = "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'x' * 8192)"
    with pytest.raises(IngestError):
        run_process([sys.executable, "-c", code, str(file)], timeout=3, max_file_bytes=1024)
    assert file.stat().st_size <= 1024


def test_child_environment_does_not_inherit_credentials(monkeypatch):
    monkeypatch.setenv("VIDEO_INGEST_TOKEN", "fake-private-credential")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "fake-cloud-credential")
    code = "import os; print(any(key in os.environ for key in ['VIDEO_INGEST_TOKEN','AWS_SECRET_ACCESS_KEY']))"
    result = run_process([sys.executable, "-c", code], timeout=3, max_file_bytes=1024)
    assert result == b"False\n"


def test_frame_limits_and_empty_range(vfr_video, tmp_path):
    processor = MediaProcessor(Settings())
    with pytest.raises(IngestError) as caught:
        processor.extract_frames(vfr_video, tmp_path, None, 25, "overview")
    assert caught.value.code == "LIMIT_EXCEEDED"
    with pytest.raises(IngestError) as caught:
        processor.extract_frames(vfr_video, tmp_path, TimeRange(start_ms=4000, end_ms=5000), 2, "overview")
    assert caught.value.code == "MEDIA_UNAVAILABLE"
