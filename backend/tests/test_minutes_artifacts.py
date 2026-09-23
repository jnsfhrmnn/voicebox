"""JFW-13: Modell-/Lizenz-Gate (fail-closed) — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_artifacts.py
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.minutes.artifacts import (
    MANIFEST_NAME,
    MINUTES_ARTIFACTS,
    ArtifactGateError,
    check_bundle_policy,
    verify_artifact,
)

MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"


def write_artifact(tmp_path: Path, **manifest_over) -> Path:
    weight = tmp_path / "model.safetensors"
    weight.write_bytes(b"weights")
    digest = hashlib.sha256(b"weights").hexdigest()
    manifest = {
        "model_id": MODEL_ID,
        "repo": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
        "revision": "a09a35458c702b33eeacc393d103063234e8bc28",
        "license_id": "apache-2.0",
        "install_source": "https://huggingface.co/Qwen/Qwen2.5-7B-Instruct",
        "files": {"model.safetensors": digest},
    }
    manifest.update(manifest_over)
    (tmp_path / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return tmp_path


def test_registry_holds_approved_model_with_license():
    spec = MINUTES_ARTIFACTS[MODEL_ID]
    assert spec.license_id == "apache-2.0"
    assert spec.bundle_allowed is False


def test_missing_manifest_blocks():
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, Path("C:/does/not/exist"))
    assert exc.value.reason_code == "waiting_for_local_artifact"


def test_unknown_model_blocks():
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact("evil/model", Path("C:/does/not/exist"))
    assert exc.value.reason_code == "artifact_missing"


def test_valid_artifact_yields_provenance(tmp_path):
    out = verify_artifact(MODEL_ID, write_artifact(tmp_path))
    assert out["model_id"] == MODEL_ID
    assert out["model_revision"] == "a09a35458c702b33eeacc393d103063234e8bc28"
    assert out["model_license"] == "apache-2.0"


def test_non_immutable_revision_blocks(tmp_path):
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, write_artifact(tmp_path, revision="main"))
    assert exc.value.reason_code == "artifact_provenance_mismatch"


def test_unapproved_license_blocks(tmp_path):
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, write_artifact(tmp_path, license_id="cc-by-nc-4.0"))
    assert exc.value.reason_code == "license_unapproved"


def test_hash_mismatch_blocks(tmp_path):
    base = write_artifact(tmp_path)
    (tmp_path / "model.safetensors").write_bytes(b"tampered")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "artifact_hash_mismatch"


def test_bundle_policy_forbids_weights_in_release():
    spec = MINUTES_ARTIFACTS[MODEL_ID]
    with pytest.raises(ArtifactGateError) as exc:
        check_bundle_policy(spec, ["app.exe", "models/minutes/model.safetensors"])
    assert exc.value.reason_code == "model_bundling_forbidden"
    check_bundle_policy(spec, ["app.exe", "backend/schema.py"])  # ohne Gewichte ok
