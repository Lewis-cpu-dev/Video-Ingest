from __future__ import annotations

import base64
import io
import json

import pytest
from PIL import Image

from server.adapters.delivery import HostDeliveryAdapter
from server.contracts.models import ClientProfile
from server.errors import IngestError
from server.probe import pixel_image


def test_image_bytes_bound_to_correct_timestamp(tmp_path):
    frames = []
    for index, timestamp in enumerate([1234, 5690]):
        path = tmp_path / f"frame{index}.png"
        path.write_bytes(pixel_image(str(index + 111111)))
        frames.append(({"frame_id": f"fr_{index}", "timestamp_ms": timestamp,
                        "path": str(path)}, path))
    result = HostDeliveryAdapter(ClientProfile()).render_images({"asset_id": "asset_test"}, frames)
    for metadata, (original, path) in zip(result.structuredContent["frames"], frames):
        assert metadata["timestamp_ms"] == original["timestamp_ms"]
        block = result.content[metadata["image_content_index"]]
        assert block.type == "image"
        assert base64.b64decode(block.data) == path.read_bytes()
        assert Image.open(io.BytesIO(base64.b64decode(block.data))).size == (640, 200)
        assert "path" not in metadata
    assert str(tmp_path) not in result.model_dump_json()
    assert result.structuredContent["host_delivery"]["host_consumption_proven"] is False


def test_delivery_budget_rejects_instead_of_silent_truncation(tmp_path):
    path = tmp_path / "frame.png"
    path.write_bytes(pixel_image("123456"))
    with pytest.raises(IngestError, match="budget") as error:
        HostDeliveryAdapter(ClientProfile(), max_bytes=200).render_images({}, [({}, path)])
    assert error.value.code == "LIMIT_EXCEEDED"
    with pytest.raises(IngestError):
        HostDeliveryAdapter(ClientProfile(), max_bytes=500).render_text({"text": "long " * 500})


def test_images_unsupported_does_not_substitute_description(tmp_path):
    with pytest.raises(IngestError) as error:
        HostDeliveryAdapter(ClientProfile(images=False)).render_images({}, [])
    assert error.value.code == "CLIENT_MODALITY_UNSUPPORTED"


def test_unverified_file_retains_reference_without_host_claim():
    payload = {"resource": {"uri": "video-ingest://artifacts/art_a", "name": "artifact",
                             "mimeType": "video/mp4", "size": 10}}
    result = HostDeliveryAdapter(ClientProfile()).render_artifact_reference(payload)
    assert result.structuredContent["resource"] == payload["resource"]
    assert not result.structuredContent["host_delivery"]["native_media_consumption_proven"]
    assert [c.type for c in result.content] == ["text"]
    verified = HostDeliveryAdapter(ClientProfile(file_resources=True)).render_artifact_reference(payload)
    assert [c.type for c in verified.content] == ["text", "resource_link"]


def test_local_paths_and_false_dimensions_are_rejected(tmp_path):
    with pytest.raises(IngestError):
        HostDeliveryAdapter(ClientProfile()).render_artifact_reference({
            "resource": {"uri": "file:///secret", "name": "secret"}})
    path = tmp_path / "frame.png"
    path.write_bytes(pixel_image("123456"))
    with pytest.raises(IngestError) as error:
        HostDeliveryAdapter(ClientProfile()).render_images({}, [({"width": 1}, path)])
    assert error.value.code == "DECODE_FAILED"


def test_text_is_model_visible_and_structured():
    result = HostDeliveryAdapter(ClientProfile()).render_text({"text": "random abc"})
    assert json.loads(result.content[0].text) == result.structuredContent
