"""JFW-6: Sound-Cue-Marken — Vertragstests (TDD, RED zuerst).

Start-/Stopp-/Fehlerton-Fenster werden im Run-Manifest markiert und sind nie
Sprach- oder Aufnahme-Evidenz (Muster JFW-11 ``is_cue_window``).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_cues.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.recording.cues import (
    CUE_KINDS,
    CueMark,
    is_cue_window,
    to_manifest,
    validate_cues,
)

START = 1_000_000
END = 9_000_000


def _mark(kind="start_ton", start=START + 10, end=START + 500):
    return CueMark(kind=kind, start_100ns=start, end_100ns=end)


def test_cue_kinds_are_fixed():
    assert CUE_KINDS == ("start_ton", "stopp_ton", "fehler_ton")


def test_valid_cues_pass():
    marks = [
        _mark("start_ton", START + 10, START + 500),
        _mark("stopp_ton", END - 500, END - 10),
    ]
    assert validate_cues(marks, started_at_100ns=START, ended_at_100ns=END) == []


def test_unknown_cue_kind_flagged():
    errors = validate_cues([_mark("witzton")], started_at_100ns=START, ended_at_100ns=END)
    assert errors
    assert errors[0].startswith("cue_unbekannt:")


def test_cue_window_must_be_positive():
    bad = CueMark(kind="start_ton", start_100ns=START + 500, end_100ns=START + 10)
    errors = validate_cues([bad], started_at_100ns=START, ended_at_100ns=END)
    assert "cue_fenster_unzulaessig:0" in errors


def test_cue_outside_run_bounds_flagged():
    before = _mark("start_ton", START - 100, START - 10)
    after = _mark("stopp_ton", END + 10, END + 100)
    errors = validate_cues([before, after], started_at_100ns=START, ended_at_100ns=END)
    assert "cue_ausserhalb_des_runs:0" in errors
    assert "cue_ausserhalb_des_runs:1" in errors


def test_overlapping_cues_flagged():
    marks = [
        _mark("start_ton", START + 10, START + 500),
        _mark("fehler_ton", START + 400, START + 900),
    ]
    errors = validate_cues(marks, started_at_100ns=START, ended_at_100ns=END)
    assert "cue_ueberlappung:1" in errors


def test_cue_window_is_never_speech_evidence():
    marks = [_mark("start_ton", START + 10, START + 500)]
    assert is_cue_window(marks, START + 100) is True
    assert is_cue_window(marks, START + 5_000) is False


def test_to_manifest_roundtrip():
    marks = [_mark("start_ton", START + 10, START + 500)]
    payload = to_manifest(marks)
    assert payload == [{"kind": "start_ton", "start_100ns": START + 10, "end_100ns": START + 500}]
    # Validierung akzeptiert die Manifest-Form (Dicts) identisch zu CueMark.
    assert validate_cues(payload, started_at_100ns=START, ended_at_100ns=END) == []
    assert is_cue_window(payload, START + 100) is True
