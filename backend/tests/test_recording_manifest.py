"""JFW-6: Run-Manifest (JFW-7-Handoff-Pflichtfelder) — Vertragstests (TDD, RED zuerst).

AC „Stop, Verwerfen und Handoff": Das Handoff enthaelt mindestens Run-ID,
Audio-/Manifest-Hash, Format, Start-/Stopzeit, Stopgrund, Geraete-Snapshot,
Luecken-/Cue-Marker und Recovery-Ablauf.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_manifest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.recording.manifest import (
    RUN_CONTRACT_VERSION,
    build_run_manifest,
    canonical_hash,
    validate_manifest,
)
from backend.recording.run_identity import new_run_id


def _manifest(**overrides):
    base = dict(
        run_id=new_run_id(),
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        started_at_100ns=1_000_000,
        ended_at_100ns=9_000_000,
        stop_reason="toggle",
        device_snapshot={"stable_id_hash": "b" * 64, "endpoint": "mic-1"},
        frames={"accepted_frames": 100, "final_frames": 100, "gap_frames": 0},
        gaps=[],
        sound_cue_marks=[{"kind": "start_ton", "start_100ns": 1_000_010, "end_100ns": 1_000_500}],
        recovery_procedure={"ablauf": "max(raw, journal), Verlustfenster <= 250 ms"},
    )
    base.update(overrides)
    return build_run_manifest(**base)


def test_manifest_has_all_required_fields():
    manifest = _manifest()
    assert validate_manifest(manifest) == []
    for field in (
        "run_id",
        "audio_hash",
        "format",
        "started_at_100ns",
        "ended_at_100ns",
        "stop_reason",
        "device_snapshot",
        "frames",
        "gaps",
        "sound_cue_marks",
        "recovery_procedure",
    ):
        assert field in manifest


def test_validate_detects_missing_field():
    manifest = _manifest()
    del manifest["stop_reason"]
    assert "pflichtfeld_fehlt:stop_reason" in validate_manifest(manifest)


def test_validate_rejects_wrong_contract_version():
    manifest = _manifest()
    manifest["contract_version"] = "unbekannt_v9"
    assert "vertragsversion_unbekannt" in validate_manifest(manifest)


def test_validate_requires_stable_device_identity_hash():
    manifest = _manifest(device_snapshot={"endpoint": "mic-1"})
    assert "geraete_snapshot_unvollstaendig" in validate_manifest(manifest)


def test_validate_rejects_invalid_run_id():
    manifest = _manifest(run_id="kaputt")
    assert "run_id_unzulaessig" in validate_manifest(manifest)


def test_validate_rejects_empty_recovery_procedure():
    manifest = _manifest(recovery_procedure={})
    assert "recovery_ablauf_fehlt" in validate_manifest(manifest)


def test_validate_rejects_inverted_time_bounds():
    manifest = _manifest(ended_at_100ns=500_000)
    assert "zeitgrenzen_unzulaessig" in validate_manifest(manifest)


def test_canonical_hash_is_deterministic_and_sensitive():
    manifest = _manifest()
    assert canonical_hash(manifest) == canonical_hash(dict(manifest))
    changed = _manifest(stop_reason="ptt_release")
    assert canonical_hash(changed) != canonical_hash(manifest)


def test_manifest_carries_cue_markers_into_handoff():
    manifest = _manifest()
    assert manifest["sound_cue_marks"][0]["kind"] == "start_ton"
    assert manifest["contract_version"] == RUN_CONTRACT_VERSION
