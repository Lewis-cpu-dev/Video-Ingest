"""Lifecycle/security regressions discovered during independent service review."""
from __future__ import annotations

from contextlib import suppress
from pathlib import Path

import pytest
from starlette.requests import Request

from server.config import Settings
from server.contracts.models import PrepareRequest, TranscriptSegment, VideoMetadata
from server.errors import IngestError
from server.service import VideoService
from server.tools.mcp import build_server

URL = "https://www.youtube.com/watch?v=BaW_jenozKc"
SECRET_EVIDENCE = "private original evidence"


class CaptionFixture:
    caption_calls = 0
    inspect_calls = 0

    def inspect(self, source):
        self.inspect_calls += 1
        return VideoMetadata(title="fixture", duration_ms=4000)

    def fetch_captions(self, source, metadata, language, cancelled):
        self.caption_calls += 1
        return [TranscriptSegment(segment_id="fixture", start_ms=0, end_ms=1000,
                                  text=SECRET_EVIDENCE, language="en", provenance="human_caption")], "en"


@pytest.fixture
def service(tmp_path):
    instance = VideoService(Settings(data_dir=tmp_path / "state"), provider=CaptionFixture(),
                            processor=object(), start_worker=False)
    yield instance
    instance.close()


def prepared(service):
    asset = service.resolve_video("alice", URL)
    request = PrepareRequest(need=["transcript"])
    result = service.prepare_video("alice", asset["asset_id"], request)
    assert service.run_once()
    assert service.get_job("alice", result["job_id"])["status"] == "succeeded"
    return asset["asset_id"], result["job_id"], request


def test_missing_artifact_can_be_reprepared(service):
    asset_id, old_job_id, request = prepared(service)
    artifact = service.store.artifacts("alice", asset_id, "transcript")[0]
    service.store.artifact_path("alice", artifact["id"]).unlink()
    with pytest.raises(IngestError) as error:
        service.read_transcript("alice", asset_id)
    assert error.value.code in {"ARTIFACT_EXPIRED", "PROCESSING_REQUIRED"}
    replacement = service.prepare_video("alice", asset_id, request)
    assert replacement["job_id"] != old_job_id, "A stale success must not prevent artifact recovery"
    assert service.run_once()
    assert service.read_transcript("alice", asset_id)["segments"][0]["text"] == SECRET_EVIDENCE
    assert service.provider.caption_calls == 2


def test_job_directory_failure_becomes_persistent_failure(service, monkeypatch):
    asset_id = service.resolve_video("alice", URL)["asset_id"]
    result = service.prepare_video("alice", asset_id, PrepareRequest(need=["transcript"]))
    original_mkdir = Path.mkdir

    def fail_job_directory(path, *args, **kwargs):
        if path.name == result["job_id"]:
            raise OSError("Simulated full disk")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_job_directory)
    assert service.run_once(), "The worker must survive a job storage failure"
    job = service.get_job("alice", result["job_id"])
    assert job["status"] == "failed"
    assert job["errors"]


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["delete", "expiry"])
async def test_http_delivery_rechecks_revocation_before_sending_bytes(service, revocation):
    asset_id, _job_id, _request = prepared(service)
    artifact = service.store.artifacts("alice", asset_id, "transcript")[0]
    # Hold one pending worker so deletion revokes access without removing the file yet.
    service.prepare_video("alice", asset_id, PrepareRequest(need=["audio"]))
    assert service.store.claim_next()
    app = build_server(service, owner="alice").streamable_http_app()
    route = next(route for route in app.routes if getattr(route, "path", "") == "/artifacts/{artifact_id}")
    scope = {"type": "http", "method": "GET", "path": f"/artifacts/{artifact['id']}",
             "path_params": {"artifact_id": artifact["id"]}, "headers": [], "http_version": "1.1",
             "asgi": {"spec_version": "2.4"}}
    response = await route.endpoint(Request(scope))
    if revocation == "delete":
        service.delete_asset("alice", asset_id)
    else:
        with service.store.transaction():
            service.store.db.execute("UPDATE assets SET expires=0 WHERE id=?", (asset_id,))
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await response(scope, receive, send)
    body = b"".join(message.get("body", b"") for message in sent)
    assert SECRET_EVIDENCE.encode() not in body, "Revoked bytes must not start streaming"
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert starts[0]["status"] in {403, 404, 410}


@pytest.mark.parametrize("revocation", ["delete", "expiry"])
def test_inflight_bytes_remain_accounted_after_revocation(service, revocation):
    asset_id = service.resolve_video("alice", URL)["asset_id"]
    service.store.charge_bytes("alice", asset_id, 5)
    if revocation == "delete":
        service.store.revoke("alice", asset_id)
    else:
        with service.store.transaction():
            service.store.db.execute("UPDATE assets SET expires=0 WHERE id=?", (asset_id,))
    # Bytes were already received by the transport before it invokes this callback.
    with suppress(IngestError):
        service.store.charge_bytes("alice", asset_id, 7)
    assert service.store.asset("alice", asset_id, include_deleted=True)["download_bytes"] == 12



def test_restart_prunes_unregistered_bytes_and_preserves_evidence(tmp_path):
    settings = Settings(data_dir=tmp_path / "recovery")
    original = VideoService(settings, provider=CaptionFixture(), processor=object(), start_worker=False)
    try:
        asset_id, _job_id, _request = prepared(original)
        interrupted = original.prepare_video("alice", asset_id, PrepareRequest(need=["video"]))
        assert original.store.claim_next()
        directory = original.store.asset_dir(asset_id) / interrupted["job_id"]
        directory.mkdir(parents=True)
        orphan = directory / "unfinished.part"
        orphan.write_bytes(b"unregistered interrupted download")
    finally:
        original.close()
    recovered = VideoService(settings, provider=CaptionFixture(), processor=object(), start_worker=False)
    try:
        job = recovered.get_job("alice", interrupted["job_id"])
        assert job["status"] == "failed"
        assert job["errors"][0]["code"] == "WORKER_INTERRUPTED"
        assert not orphan.exists()
        assert recovered.read_transcript("alice", asset_id)["segments"][0]["text"] == SECRET_EVIDENCE
    finally:
        recovered.close()


def test_asset_capacity_rejects_before_platform_request(tmp_path):
    provider = CaptionFixture()
    svc = VideoService(Settings(data_dir=tmp_path / "capacity", max_assets=1),
                       provider=provider, processor=object(), start_worker=False)
    try:
        first = svc.resolve_video("alice", URL)
        with pytest.raises(IngestError) as error:
            svc.resolve_video("alice", "https://www.bilibili.com/video/BV1eZdzBGEvE")
        assert error.value.code == "LIMIT_EXCEEDED"
        assert provider.inspect_calls == 1, "A rejected request must not launch the platform extractor"
        assert svc.resolve_video("alice", URL)["asset_id"] == first["asset_id"]
        assert provider.inspect_calls == 1
    finally:
        svc.close()
