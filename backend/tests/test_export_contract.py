"""JFW-4: Exportstatus-Maschine und Readiness — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.contract import (
    assess_readiness,
    can_transition,
    validate_words,
)
from backend.tests.jfw4_sources import words_full


def test_transition_matrix():
    assert can_transition("preparing", "ready")
    assert can_transition("preparing", "ready_with_warnings")
    assert can_transition("preparing", "blocked")
    assert can_transition("ready", "exporting")
    assert can_transition("ready_with_warnings", "exporting")
    assert can_transition("exporting", "exported")
    assert can_transition("exporting", "failed")
    assert can_transition("exporting", "canceled")
    assert can_transition("exported", "invalidated")
    assert can_transition("failed", "exporting")  # Retry ohne Ergebnis
    assert can_transition("canceled", "exporting")


def test_transition_matrix_forbids_resurrections():
    assert not can_transition("exported", "exporting")
    assert not can_transition("canceled", "exported")
    assert not can_transition("exported", "failed")
    assert not can_transition("invalidated", "exported")
    assert not can_transition("blocked", "exported")


def test_readiness_all_green():
    out = assess_readiness(timing="vollstaendig", speakers="vollstaendig")
    assert out["state"] == "ready"
    assert out["warnings"] == []
    assert out["partial_mode"] is None
    assert out["reason_code"] is None


def test_readiness_partial_timing_warns():
    out = assess_readiness(timing="teilweise", speakers="vollstaendig")
    assert out["state"] == "ready_with_warnings"
    assert any(w["code"] == "timing_teilweise" for w in out["warnings"])


def test_readiness_missing_speaker_requires_confirmation():
    out = assess_readiness(timing="vollstaendig", speakers="nicht_verfuegbar")
    assert out["state"] == "blocked"
    assert out["reason_code"] == "teilmodus_bestaetigung_erforderlich"


def test_readiness_missing_speaker_with_confirmation_is_timing_only():
    out = assess_readiness(timing="vollstaendig", speakers="nicht_verfuegbar",
                           partial_confirmed=True)
    assert out["state"] == "ready_with_warnings"
    assert out["partial_mode"] == "timing_only"
    assert any(w["code"] == "teilqualitaet" for w in out["warnings"])


def test_readiness_missing_timing_with_confirmation_is_speaker_only():
    out = assess_readiness(timing="nicht_verfuegbar", speakers="vollstaendig",
                           partial_confirmed=True)
    assert out["state"] == "ready_with_warnings"
    assert out["partial_mode"] == "speaker_only"


def test_readiness_no_content_blocked():
    out = assess_readiness(timing="nicht_verfuegbar", speakers="nicht_verfuegbar",
                           partial_confirmed=True)
    assert out["state"] == "blocked"
    assert out["reason_code"] == "kein_mindestinhalt"


def test_readiness_sources_and_dedupe_warnings():
    out = assess_readiness(timing="teilweise", speakers="teilweise",
                           sources="teilweise", dedupe="unsicher")
    assert out["state"] == "ready_with_warnings"
    codes = {w["code"] for w in out["warnings"]}
    assert {"timing_teilweise", "sprecher_teilweise", "quellen_teilweise",
            "dedupe_unsicher"} <= codes
    assert out["quality_dimensions"]["sources"] == "teilweise"


def test_readiness_file_job_without_jfw11_is_not_a_partial_error():
    out = assess_readiness(timing="vollstaendig", speakers="vollstaendig",
                           sources="nicht_betroffen", dedupe="nicht_betroffen")
    assert out["state"] == "ready"
    assert out["warnings"] == []


def test_validate_words_accepts_valid():
    assert validate_words(words_full(), 10_000) == []


def test_validate_words_rejects_invented_and_missing_bounds():
    bad = words_full()
    bad[0]["start_ms"] = None  # aligned ohne Grenze
    assert validate_words(bad, 10_000)
    bad2 = words_full()
    bad2[1]["timing_status"] = "unaligned"  # Grenze trotz unaligned
    assert validate_words(bad2, 10_000)
    bad3 = words_full()
    bad3[2]["end_ms"] = bad3[2]["start_ms"]  # end <= start
    assert validate_words(bad3, 10_000)
    bad4 = words_full()
    bad4[3]["end_ms"] = 20_000  # ausserhalb der Audiodauer
    assert validate_words(bad4, 10_000)


def test_validate_words_rejects_duplicates():
    bad = words_full()
    bad[5]["word_id"] = "w1"
    assert validate_words(bad, 10_000)
