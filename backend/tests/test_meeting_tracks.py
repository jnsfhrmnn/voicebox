"""JFW-11: Track-Vertrag (zwei autoritative Rohspuren) — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_tracks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.tracks import (
    MODE_ENDPOINT,
    MODE_PROCESS,
    ROLE_MIC,
    ROLE_REMOTE,
    SCOPE_ENDPOINT,
    SCOPE_PROCESS_TREE,
    Track,
    TrackSection,
    validate_tracks,
)

HASH = "a" * 64


def _section(sid="sec-1", frames=48000, identity=None, start=0, end=1_000_000):
    return TrackSection(
        section_id=sid,
        device_identity=identity or {"endpoint_id": "{0.0.1.x}", "stable_id": "{0.0.1.00000000}.{S1}"},
        fmt={"rate_hz": 16000, "channels": 2, "bits_per_sample": 32},
        frames=frames,
        content_hash=HASH,
        started_at_100ns=start,
        ended_at_100ns=end,
    )


def _mic():
    return Track(
        track_id="track-mic",
        role=ROLE_MIC,
        capture_mode=MODE_ENDPOINT,
        scope_label=SCOPE_ENDPOINT,
        sections=(_section(),),
    )


def _remote():
    return Track(
        track_id="track-remote",
        role=ROLE_REMOTE,
        capture_mode=MODE_PROCESS,
        scope_label=SCOPE_PROCESS_TREE,
        sections=(_section(sid="sec-2", identity={"process_tree_id": 4711, "windows_pid": 4711}),),
    )


def test_valid_two_track_setup_passes():
    assert validate_tracks([_mic(), _remote()]) == []


def test_single_track_rejected():
    errors = validate_tracks([_mic()])
    assert "zwei_quellen_erforderlich" in errors


def test_duplicate_role_rejected():
    a, b = _mic(), _mic()
    b = Track(track_id="track-mic-2", role=a.role, capture_mode=a.capture_mode,
              scope_label=a.scope_label, sections=a.sections)
    errors = validate_tracks([a, b])
    assert "rollen_eindeutig" in errors


def test_endpoint_mode_requires_stable_id():
    sec = _section(identity={"endpoint_id": "{0.0.1.x}"})  # ohne stable_id
    mic = Track(track_id="t1", role=ROLE_MIC, capture_mode=MODE_ENDPOINT,
                scope_label=SCOPE_ENDPOINT, sections=(sec,))
    errors = validate_tracks([mic, _remote()])
    assert "stable_id_fehlt" in errors


def test_single_tab_scope_is_forbidden():
    mic = _mic()
    remote = Track(track_id="track-remote", role=ROLE_REMOTE, capture_mode=MODE_PROCESS,
                   scope_label="einzelner_tab", sections=_remote().sections)
    errors = validate_tracks([mic, remote])
    assert "scope_unzulaessig" in errors


def test_process_scope_must_be_process_tree():
    remote = Track(track_id="track-remote", role=ROLE_REMOTE, capture_mode=MODE_PROCESS,
                   scope_label=SCOPE_ENDPOINT, sections=_remote().sections)
    errors = validate_tracks([_mic(), remote])
    assert "scope_passt_nicht_zum_modus" in errors


def test_sections_need_own_provenance_and_hashes():
    bad = TrackSection(
        section_id="sec-1",
        device_identity={"endpoint_id": "x", "stable_id": "y"},
        fmt={"rate_hz": 48000, "channels": 2, "bits_per_sample": 32},
        frames=-5,
        content_hash="kurz",
        started_at_100ns=10,
        ended_at_100ns=5,
    )
    mic = Track(track_id="t1", role=ROLE_MIC, capture_mode=MODE_ENDPOINT,
                scope_label=SCOPE_ENDPOINT, sections=(bad,))
    errors = validate_tracks([mic, _remote()])
    assert "frames_ungueltig" in errors
    assert "hash_ungueltig" in errors
    assert "abschnitt_zeit_ungueltig" in errors


def test_empty_sections_rejected():
    mic = Track(track_id="t1", role=ROLE_MIC, capture_mode=MODE_ENDPOINT,
                scope_label=SCOPE_ENDPOINT, sections=())
    errors = validate_tracks([mic, _remote()])
    assert "abschnitte_fehlen" in errors
