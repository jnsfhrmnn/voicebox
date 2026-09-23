"""JFW-2: Modell-/Lizenz-Gate (fail-closed) — Vertragstests.

Kein Alignment-Modell wird ohne gueltiges Installationsmanifest geladen.
Unklare/unvereinbare Lizenz, Hash- oder Revisionsabweichung blockieren; die
Bündelungssperre (CC-BY-NC) ist erzwungen.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_alignment_artifacts.py
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.alignment.artifacts import (  # noqa: E402
    ALIGNMENT_ARTIFACTS,
    ArtifactGateError,
    check_bundle_policy,
    verify_artifact,
)

REV = "0" * 40


def make_install(tmp_path: Path, **manifest_kw):
    """Legt eine gueltige Artefaktdatei + Manifest an; liefert (base, manifest)."""
    model_file = tmp_path / "model.bin"
    model_file.write_bytes(b"weights")
    manifest = {
        "model_id": "facebook/mms_fa",
        "repo": ALIGNMENT_ARTIFACTS["facebook/mms_fa"].repo_url,
        "revision": REV,
        "license_id": "CC-BY-NC-4.0",
        "personal_use_ack": True,
        "install_source": "user-initiated: https://huggingface.co/facebook/mms_fa",
        "files": {
            "model.bin": hashlib.sha256(b"weights").hexdigest(),
        },
    }
    manifest.update(manifest_kw)
    (tmp_path / "alignment-artifact.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return tmp_path, manifest


def test_registry_blocks_bundling_for_mms_fa():
    spec = ALIGNMENT_ARTIFACTS["facebook/mms_fa"]
    assert spec.bundle_allowed is False
    assert spec.distribution_allowed is False
    assert spec.personal_use_allowed is True
    assert spec.license_id == "CC-BY-NC-4.0"


def test_missing_manifest_is_rejected(tmp_path):
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact("facebook/mms_fa", tmp_path)
    assert exc.value.reason_code == "waiting_for_local_artifact"


def test_valid_manifest_passes(tmp_path):
    base, _ = make_install(tmp_path)
    prov = verify_artifact("facebook/mms_fa", base)
    assert prov["model_id"] == "facebook/mms_fa"
    assert prov["model_revision"] == REV
    assert prov["model_license"] == "CC-BY-NC-4.0"
    assert prov["model_sha256"]


def test_hash_mismatch_is_rejected(tmp_path):
    base, manifest = make_install(tmp_path, files={"model.bin": "0" * 64})
    (base / "alignment-artifact.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact("facebook/mms_fa", base)
    assert exc.value.reason_code == "artifact_hash_mismatch"


def test_non_immutable_revision_is_rejected(tmp_path):
    base, manifest = make_install(tmp_path, revision="main")
    (base / "alignment-artifact.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactGateError):
        verify_artifact("facebook/mms_fa", base)


def test_missing_personal_use_ack_is_rejected(tmp_path):
    base, manifest = make_install(tmp_path, personal_use_ack=False)
    (base / "alignment-artifact.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactGateError) as exc:
        verify_artifact("facebook/mms_fa", base)
    assert exc.value.reason_code == "license_ack_missing"


def test_license_mismatch_is_rejected(tmp_path):
    base, manifest = make_install(tmp_path, license_id="apache-2.0")
    (base / "alignment-artifact.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ArtifactGateError):
        verify_artifact("facebook/mms_fa", base)


def test_bundle_policy_rejects_packaged_model_weights(tmp_path):
    spec = ALIGNMENT_ARTIFACTS["facebook/mms_fa"]
    packaged = ["backend/app.py", "models/alignment/model.bin"]
    with pytest.raises(ArtifactGateError) as exc:
        check_bundle_policy(spec, packaged)
    assert exc.value.reason_code == "model_bundling_forbidden"
    check_bundle_policy(spec, ["backend/app.py"])  # sauberes Inventar: OK
