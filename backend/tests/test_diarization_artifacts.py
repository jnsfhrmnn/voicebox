"""JFW-3: Modell-/Lizenz-Gate (fail-closed, JFW-3-Regel) — Vertragstests.

Manifest-Bindung (Repo, 40-Hex-Revision, Datei-SHA-256, Lizenz-ID,
``license_ack``), fail-closed Gründe ohne Netzwerkzugriff und Bündelungssperre
für Modellgewichte im Release-Inventar.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_diarization_artifacts.py
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.diarization.artifacts import (
    DIARIZATION_ARTIFACTS,
    MANIFEST_NAME,
    ArtifactGateError,
    ArtifactSpec,
    check_bundle_policy,
    verify_artifact,
)

MODEL_ID = "pyannote/speaker-diarization-community-1"

def write_manifest(base: Path, **overrides) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    payload = base / "pipeline.bin"
    payload.write_bytes(b"modellgewicht")
    spec = DIARIZATION_ARTIFACTS[MODEL_ID]
    manifest = {
        "model_id": MODEL_ID,
        "repo": spec.repo_url,
        "revision": "1" * 40,
        "license_id": spec.license_id,
        "license_ack": True,
        "install_source": "https://huggingface.co/pyannote/speaker-diarization-community-1",
        "files": {
            "pipeline.bin": hashlib.sha256(b"modellgewicht").hexdigest(),
        },
    }
    manifest.update(overrides)
    (base / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return base

def test_registry_declares_fail_closed_license_row():
    spec = DIARIZATION_ARTIFACTS[MODEL_ID]
    assert spec.license_id == "CC-BY-4.0"
    assert spec.personal_use_allowed is True
    assert spec.bundle_allowed is False  # keine Bündelung ins Release (JFW-3-Regel)
    assert spec.distribution_allowed is False

def test_valid_manifest_yields_provenance(tmp_path):
    base = write_manifest(tmp_path / "model")
    prov = verify_artifact(MODEL_ID, base)
    assert prov["model_id"] == MODEL_ID
    assert prov["model_revision"] == "1" * 40
    assert prov["model_license"] == "CC-BY-4.0"
    assert len(prov["model_sha256"]) == 64

def test_missing_manifest_is_waiting_without_network(tmp_path):
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, tmp_path / "leer")
    assert exc.value.reason_code == "waiting_for_local_artifact"

def test_unknown_model_is_artifact_missing(tmp_path):
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact("unbekannt/modell", tmp_path)
    assert exc.value.reason_code == "artifact_missing"

def test_non_immutable_revision_rejected(tmp_path):
    base = write_manifest(tmp_path / "model", revision="main")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "artifact_provenance_mismatch"

def test_wrong_repo_rejected(tmp_path):
    base = write_manifest(tmp_path / "model", repo="https://evil.example/modell")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "artifact_provenance_mismatch"

def test_license_mismatch_rejected(tmp_path):
    base = write_manifest(tmp_path / "model", license_id="UNBEKANNT")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "license_unapproved"

def test_missing_license_ack_rejected(tmp_path):
    base = write_manifest(tmp_path / "model", license_ack=False)
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "license_ack_missing"

def test_file_hash_mismatch_rejected(tmp_path):
    base = write_manifest(tmp_path / "model")
    (base / "pipeline.bin").write_bytes(b"getauscht")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact(MODEL_ID, base)
    assert exc.value.reason_code == "artifact_hash_mismatch"

def test_bundle_policy_blocks_model_weights(tmp_path):
    spec = DIARIZATION_ARTIFACTS[MODEL_ID]
    with pytest.raises(ArtifactGateError) as exc:
        check_bundle_policy(spec, ["backend/app.py", "models/diarization/pipeline.bin"])
    assert exc.value.reason_code == "model_bundling_forbidden"
    check_bundle_policy(spec, ["backend/app.py", "assets/icon.png"])  # kein Fehler

def test_bundle_policy_allows_only_when_licensed():
    permissive = ArtifactSpec(
        model_id="test/modell",
        repo_url="https://example.com",
        license_id="MIT",
        personal_use_allowed=True,
        commercial_use_allowed=True,
        bundle_allowed=True,
        distribution_allowed=True,
    )
    check_bundle_policy(permissive, ["models/diarization/pipeline.bin"])
