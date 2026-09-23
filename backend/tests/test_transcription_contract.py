"""JFW-7: Processing Contract (Zustandsmaschine) — Vertragstests (TDD, RED zuerst).

Spec „Processing Contract" (Tabelle exakt) + AC „Eingabe, Snapshot und Backendbindung",
„Fehler, Retry und Recovery": Wartezustaende ohne stillen Fallback, Attempt-Bindung
bis terminal, sichtbarer neuer Attempt bei Backendwechsel, Terminal-Race = erster
dauerhafter Ausgang gewinnt, kein Teilergebnis als final.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.transcription.contract import (
    CANCELED,
    FAILED,
    INVALIDATED,
    NO_SPEECH,
    QUEUED,
    RAW_READY,
    TERMINAL_STATES,
    TRANSCRIBING,
    WAITING_FOR_BACKEND,
    WAITING_FOR_MODEL,
    Attempt,
    ContractError,
    ProcessingSession,
)


def _run() -> ProcessingSession:
    return ProcessingSession(run_id="jfw7-run-" + "0" * 32, snapshot_hash="s" * 64)


def _started(r: ProcessingSession | None = None) -> tuple[ProcessingSession, Attempt]:
    s = r or _run()
    att = s.start_attempt(
        backend_variant="cpu",
        backend_generation=4,
        model_profile="jfw7-dictate-v1",
        model_ready=True,
        backend_stable=True,
    )
    return s, att


def test_start_queued():
    assert _run().state == QUEUED


def test_start_attempt_geht_nach_transcribing_und_bindet_backend():
    s, att = _started()
    assert s.state == TRANSCRIBING
    assert att.backend_variant == "cpu"
    assert att.backend_generation == 4
    assert s.current_attempt is att
    assert att.outcome is None  # gebunden bis terminal


def test_backend_unsicher_bleibt_waiting_ohne_zustellung():
    s = _run()
    att = s.start_attempt(
        backend_variant="cpu",
        backend_generation=4,
        model_profile="jfw7-dictate-v1",
        model_ready=True,
        backend_stable=False,
    )
    assert att is None
    assert s.state == WAITING_FOR_BACKEND
    assert s.current_attempt is None  # keinem Fallback-Prozess zugestellt


def test_modell_fehlt_bleibt_waiting_ohne_netzwerkaktion():
    s = _run()
    att = s.start_attempt(
        backend_variant="cpu",
        backend_generation=4,
        model_profile="jfw7-dictate-v1",
        model_ready=False,
        backend_stable=True,
    )
    assert att is None
    assert s.state == WAITING_FOR_MODEL
    assert s.current_attempt is None


def test_zweiter_start_mit_selber_generation_ist_blockiert():
    s, _ = _started()
    with pytest.raises(ContractError) as e:
        s.start_attempt(
            backend_variant="cpu",
            backend_generation=4,
            model_profile="jfw7-dictate-v1",
            model_ready=True,
            backend_stable=True,
        )
    assert "attempt_bereits_gebunden" in str(e.value)


def test_backendwechsel_erzeugt_sichtbaren_neuen_attempt():
    s, att1 = _started()
    att2 = s.start_attempt(
        backend_variant="cuda",
        backend_generation=5,
        model_profile="jfw7-dictate-v1",
        model_ready=True,
        backend_stable=True,
    )
    assert att2 is not None
    assert att2 is not att1
    assert att1.outcome == "superseded_by_switch"  # sichtbar, nie stille Fortsetzung
    hist = s.attempt_history()
    assert [h["backend_variant"] for h in hist] == ["cpu", "cuda"]
    assert hist[0]["outcome"] == "superseded_by_switch"


def test_commit_raw_genau_einmal():
    s, att = _started()
    s.commit_raw(revision_id="rev-1", text_hash="t" * 64)
    assert s.state == RAW_READY
    assert s.terminal_outcome == "raw_ready"
    assert att.outcome == "raw_ready"
    with pytest.raises(ContractError):
        s.commit_raw(revision_id="rev-2", text_hash="u" * 64)
    assert s.state == RAW_READY


def test_commit_nur_aus_transcribing():
    s = _run()
    with pytest.raises(ContractError):
        s.commit_raw(revision_id="rev-1", text_hash="t" * 64)


def test_no_speech_terminal_ohne_payload():
    s, att = _started()
    s.mark_no_speech()
    assert s.state == NO_SPEECH
    assert s.terminal_outcome == "no_speech"
    assert att.outcome == "no_speech"


def test_terminal_race_commit_vs_cancel_erster_gewinnt():
    s, _ = _started()
    s.cancel(reason_code="nutzerabbruch")
    assert s.state == CANCELED
    with pytest.raises(ContractError) as e:
        s.commit_raw(revision_id="rev-1", text_hash="t" * 64)
    assert "bereits_terminal" in str(e.value)
    assert s.state == CANCELED  # nach Cancel wird kein Ergebnis autoritativ


def test_terminal_race_cancel_vs_commit_erster_gewinnt():
    s, _ = _started()
    s.commit_raw(revision_id="rev-1", text_hash="t" * 64)
    with pytest.raises(ContractError):
        s.cancel(reason_code="nutzerabbruch")
    assert s.state == RAW_READY


def test_fail_behaelt_diagnose():
    s, att = _started()
    s.fail(reason_code="audio_beschaedigt")
    assert s.state == FAILED
    assert s.reason_code == "audio_beschaedigt"
    assert att.outcome == "failed"


def test_invalidate_auch_nach_raw_ready_neue_revision_erforderlich():
    s, _ = _started()
    s.commit_raw(revision_id="rev-1", text_hash="t" * 64)
    s.invalidate(reason_code="snapshot_geaendert")
    assert s.state == INVALIDATED
    assert s.terminal_outcome == "invalidated"


def test_invalidate_nach_cancel_blockiert():
    s, _ = _started()
    s.cancel()
    with pytest.raises(ContractError):
        s.invalidate(reason_code="audio_geaendert")


def test_terminal_states_definition():
    assert frozenset({RAW_READY, NO_SPEECH, FAILED, CANCELED, INVALIDATED}) == TERMINAL_STATES
