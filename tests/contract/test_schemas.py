import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from server.config import Settings
from server.contracts.models import (
    Artifact,
    ClientProfile,
    Evidence,
    Job,
    PrepareRequest,
    TimeRange,
    VideoAsset,
    VideoSource,
)


def test_exported_schemas_match_runtime_contracts():
    root = Path(__file__).resolve().parents[2] / "server" / "contracts" / "schemas"
    for model in (VideoSource, VideoAsset, Artifact, Evidence, Job, ClientProfile, PrepareRequest, TimeRange):
        schema = json.loads((root / f"{model.__name__}.json").read_text())
        Draft202012Validator.check_schema(schema)
        expected = model.model_json_schema()
        for key in expected:
            assert schema[key] == expected[key]


@pytest.mark.parametrize("value", [{"start_ms": 3, "end_ms": 3}, {"start_ms": -1, "end_ms": 3},
                                   {"start_ms": 3, "end_ms": 2}])
def test_time_ranges_are_half_open_and_ordered(value):
    with pytest.raises(ValidationError):
        TimeRange.model_validate(value)


def test_model_cannot_override_operator_limits_or_paths():
    with pytest.raises(ValidationError):
        PrepareRequest.model_validate({"need": ["video"], "outputDir": "/tmp", "max_download_bytes": 9999999999})
    with pytest.raises(ValidationError):
        PrepareRequest.model_validate({"need": ["clip"]})
    with pytest.raises(ValueError):
        Settings(data_dir=Path("/tmp/outside-workspace"))


def test_evidence_requires_exactly_one_modality():
    with pytest.raises(ValidationError):
        Evidence(asset_id="a", asset_revision="r1")
    assert ClientProfile().verification == "unverified"
    assert not ClientProfile().native_video
