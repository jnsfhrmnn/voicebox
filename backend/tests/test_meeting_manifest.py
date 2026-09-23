"""JFW-11: Atomarer Capture-Manifest — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_manifest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.manifest import (
    CAPTURE_CONTRACT_VERSION,
    REQUIRED_FIELDS,
    build_capture_manifest,
    canonical_hash,
    is_cue_window,
    validate_manifest,
)
from backend.meeting.tracks import (
    MODE_ENDPOINT,
    MODE_PROCESS,
    ROLE_MIC,
    ROLE_REMOTE,
    SCOPE_ENDPOINT,
    SCOPE_PROCESS_TREE,
    Track,
    TrackSection,
)

HASH = "b" * 64


def _tracks():
    mic_sec = TrackSection(
        section_id="sec-mic-1",
        device_identity={"endpoint_id": "{0.0.1.x}", "stable_id": "{0.0.1.00000000}.{S1}"},
        fmt={"rate_hz": 16000, "channels": 2, "bits_per_sample": 32},
        frames=16000 * 60,
        content_hash=HASH,
        started_at_100ns=0,
        ended_at_100ns=600_000_000,
    )
    rem_sec = TrackSection(
        section_id="sec-rem-1",
        device_identity={"process_tree_id": 4711, "windows_pid": 4711},
        fmt={"rate_hz": 48000, "channels": 2, "bits_per_sample": 32},
        frames=48000 * 60,
        content_hash="c" * 64,
        started_at_100ns=16_110_000,
        ended_at_100ns=600_016_110_000 // 1000 * 1000,
    )
    mic = Track(track_id="track-mic", role=ROLE_MIC, capture_mode=MODE_ENDPOINT,
                scope_label=SCOPE_ENDPOINT, sections=(mic_sec,))
    rem = Track(track_id="track-remote", role=ROLE_REMOTE, capture_mode=MODE_PROCESS,
                scope_label=SCOPE_PROCESS_TREE, sections=(rem_sec,))
    return [mic, rem]


def _manifest():
    return build_capture_manifest(
        meeting_run_id="mr-1",
        jfw6_run_ref="jfw6-run-1",
        started_at_100ns=0,
        ended_at_100ns=60_000_000_00,
        stop_reason="user_stop",
        tracks=_tracks(),
        gaps=[{"track_id": "track-remote", "start_100ns": 100, "end_100ns": 200, "reason_code": "quelle_stumm"}],
        sound_cue_marks=[
            {"role": ROLE_MIC, "start_100ns": 0, "end_100ns": 30_000_00, "kind": "start_ton"},
            {"role": ROLE_REMOTE, "start_100ns": 0, "end_100ns": 30_000_00, "kind": "start_ton"},
        ],
        sync={
            "version": "qpc_offset_drift_v1",
            "quality": "sync_ok",
            "models": {"track-mic": {"drift_ppm": 48.7}, "track-remote": {"drift_ppm": 41.1}},
        },
        recovery_status={"track-mic": "vollstaendig", "track-remote": "teilweise"},
    )


def test_manifest_has_all_required_fields():
    m = _manifest()
    assert m["contract_version"] == CAPTURE_CONTRACT_VERSION
    missing = [f for f in REQUIRED_FIELDS if f not in m]
    assert missing == []
    assert validate_manifest(m) == []


def test_manifest_binds_both_source_roles_and_track_hashes():
    m = _manifest()
    assert m["source_roles"] == [ROLE_MIC, ROLE_REMOTE]
    for track in m["tracks"]:
        for sec in track["sections"]:
            assert sec["content_hash"]
            assert sec["section_id"]
    assert m["jfw6_run_reference"] == "jfw6-run-1"


def test_validate_detects_missing_field_and_wrong_roles():
    m = _manifest()
    del m["stop_reason"]
    assert "pflichtfeld_fehlt:stop_reason" in validate_manifest(m)
    m2 = _manifest()
    m2["source_roles"] = [ROLE_MIC]
    assert "quellenrollen_unvollstaendig" in validate_manifest(m2)


def test_canonical_hash_is_stable_and_sensitive():
    m1, m2 = _manifest(), _manifest()
    assert canonical_hash(m1) == canonical_hash(m2)
    m2["stop_reason"] = "crash"
    assert canonical_hash(m1) != canonical_hash(m2)


def test_sound_cue_windows_are_marked_and_never_speech_evidence():
    m = _manifest()
    assert is_cue_window(m, ROLE_MIC, 10_000_00) is True   # im Startton-Fenster
    assert is_cue_window(m, ROLE_MIC, 100_000_00) is False
    for mark in m["sound_cue_marks"]:
        assert mark["kind"] in ("start_ton", "end_ton")
        assert mark["role"] in (ROLE_MIC, ROLE_REMOTE)
