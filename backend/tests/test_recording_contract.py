"""JFW-6: Aufnahmezustandsvertrag (State Contract) — Vertragstests (TDD, RED zuerst).

Spec „State Contract" + AC-Gruppen „Hotkey, Run-Identität und Sichtbarkeit",
„Aufnahmebeginn und Mikrofon", „Stop, Verwerfen und Handoff", „Recovery und
Fehler". Exactly-once Start/Stopp inklusive Auto-Repeat-Deduplizierung.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.recording.contract import (
    CANCELED,
    FAILED,
    IDLE,
    RECORDING,
    SECURED,
    STARTING,
    STOPPING,
    RunSession,
    RunStateError,
)
from backend.recording.run_identity import new_run_id


def _session() -> RunSession:
    return RunSession(run_id=new_run_id())


def _starting(s: RunSession) -> RunSession:
    s.begin(hotkey_ready=True, t_100ns=1_000)
    return s


def _recording(s: RunSession) -> RunSession:
    _starting(s)
    s.bind_format("pcm_s16le", 1, 48_000)
    s.activate(stream_open=True, run_persisted=True, first_block_accepted=True, t_100ns=2_000)
    return s


def test_full_happy_path_idle_to_secured():
    s = _recording(_session())
    assert s.state == RECORDING
    intent = s.request_stop(cause="toggle", t_100ns=5_000)
    assert intent["cause"] == "toggle"
    assert s.state == STOPPING
    s.secure(t_100ns=5_200)
    assert s.state == SECURED


def test_initial_state_is_idle():
    assert _session().state == IDLE


def test_start_repeat_is_deduplicated_no_second_run():
    s = _starting(_session())
    with pytest.raises(RunStateError):
        s.begin(hotkey_ready=True, t_100ns=1_500)  # Auto-Repeat / Mehrfachfeuer
    assert s.state == STARTING


def test_start_blocked_without_registered_hotkey():
    s = _session()
    with pytest.raises(RunStateError):
        s.begin(hotkey_ready=False, t_100ns=1_000)
    assert s.state == IDLE  # keine stille Ersatzbelegung, kein Run


def test_activate_requires_all_three_confirmations():
    for flags in (
        dict(stream_open=False, run_persisted=True, first_block_accepted=True),
        dict(stream_open=True, run_persisted=False, first_block_accepted=True),
        dict(stream_open=True, run_persisted=True, first_block_accepted=False),
    ):
        s = _starting(_session())
        with pytest.raises(RunStateError):
            s.activate(t_100ns=2_000, **flags)
        assert s.state == STARTING  # „Startet" — keine Behauptung aktiver Aufnahme


def test_activate_requires_bound_format():
    s = _starting(_session())
    with pytest.raises(RunStateError):
        s.activate(stream_open=True, run_persisted=True, first_block_accepted=True, t_100ns=2_000)
    assert s.state == STARTING


def test_format_bound_at_start_and_change_blocked():
    s = _starting(_session())
    fmt = s.bind_format("pcm_s16le", 1, 48_000)
    assert fmt == {"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000}
    with pytest.raises(RunStateError):
        s.bind_format("pcm_s16le", 2, 48_000)  # kein zweites, abweichendes Format
    s.activate(stream_open=True, run_persisted=True, first_block_accepted=True, t_100ns=2_000)
    # Unzulaessige Formatänderung waehrend der Aufnahme: blockiert statt still konvertiert.
    with pytest.raises(RunStateError):
        s.assert_format(sample_format="pcm_s16le", channels=2, rate_hz=48_000)
    s.assert_format(sample_format="pcm_s16le", channels=1, rate_hz=48_000)


def test_second_stop_intent_rejected_no_second_completion():
    s = _recording(_session())
    s.request_stop(cause="toggle", t_100ns=5_000)
    with pytest.raises(RunStateError):
        s.request_stop(cause="ptt_release", t_100ns=5_100)  # Auto-Repeat des Hotkeys
    assert len(s.stop_intents) == 1


def test_stop_requires_known_cause():
    s = _recording(_session())
    with pytest.raises(RunStateError):
        s.request_stop(cause="unbekannt", t_100ns=5_000)
    assert s.state == RECORDING


@pytest.mark.parametrize(
    "cause",
    ["toggle", "ptt_release", "bedienung", "system_gesperrt", "standby", "benutzerwechsel", "session_ende"],
)
def test_stop_accepts_ui_and_system_causes(cause):
    s = _recording(_session())
    intent = s.request_stop(cause=cause, t_100ns=5_000)
    assert intent["cause"] == cause


def test_stop_time_must_be_monotonic():
    s = _recording(_session())
    with pytest.raises(RunStateError):
        s.request_stop(cause="toggle", t_100ns=1_500)  # vor dem Startpunkt / Ruecklauf
    assert s.state == RECORDING


def test_stop_only_from_recording():
    s = _starting(_session())
    with pytest.raises(RunStateError):
        s.request_stop(cause="toggle", t_100ns=5_000)


def test_secure_only_from_stopping():
    s = _recording(_session())
    with pytest.raises(RunStateError):
        s.secure(t_100ns=5_000)
    assert s.state == RECORDING


def test_unzulaessige_transitions_fail_closed():
    s = _recording(_session())
    s.request_stop(cause="toggle", t_100ns=5_000)
    s.secure(t_100ns=5_200)
    # Terminal: kein weiterer Übergang, keine zweite Sicherung.
    with pytest.raises(RunStateError):
        s.secure(t_100ns=5_300)
    with pytest.raises(RunStateError):
        s.request_stop(cause="toggle", t_100ns=5_400)
    with pytest.raises(RunStateError):
        s.fail(reason_code="geraetefeher", t_100ns=5_500)


def test_device_failure_marks_failed_and_keeps_no_auto_restart():
    s = _recording(_session())
    s.fail(reason_code="geraetefeher", t_100ns=5_000)
    assert s.state == FAILED
    with pytest.raises(RunStateError):
        s.begin(hotkey_ready=True, t_100ns=6_000)  # kein automatischer Neustart


def test_fail_can_hit_stop_race_from_recording_and_stopping():
    # Stop und Geräte-/Prozessfehler nahezu gleichzeitig: beide Wege sind
    # zulaessig, der dauerhafte Ausgang wird atomar in der Persistenz entschieden.
    s = _recording(_session())
    s.request_stop(cause="prozessfehler", t_100ns=5_000)
    s.fail(reason_code="prozessfehler", t_100ns=5_050)
    assert s.state == FAILED


def test_failed_run_can_recover_to_secured():
    s = _recording(_session())
    s.fail(reason_code="geraetefeher", t_100ns=5_000)
    s.complete_recovery(t_100ns=9_000)  # Recovery/Behalt des recoverbaren Audios
    assert s.state == SECURED


def test_discard_start_without_audio():
    s = _starting(_session())
    s.discard(confirmed=False, has_audio=False, deletion_contract_hash=None, t_100ns=3_000)
    assert s.state == CANCELED


def test_discard_with_audio_requires_confirmation():
    s = _recording(_session())
    s.fail(reason_code="geraetefeher", t_100ns=5_000)
    with pytest.raises(RunStateError):
        s.discard(confirmed=False, has_audio=True, deletion_contract_hash="d" * 64, t_100ns=6_000)
    assert s.state == FAILED


def test_discard_with_audio_requires_deletion_contract():
    s = _recording(_session())
    s.fail(reason_code="geraetefeher", t_100ns=5_000)
    with pytest.raises(RunStateError):
        s.discard(confirmed=True, has_audio=True, deletion_contract_hash=None, t_100ns=6_000)
    assert s.state == FAILED
    s.discard(confirmed=True, has_audio=True, deletion_contract_hash="d" * 64, t_100ns=6_100)
    assert s.state == CANCELED
    assert s.deletion_contract_hash == "d" * 64


def test_discard_from_terminal_secured_is_blocked():
    s = _recording(_session())
    s.request_stop(cause="toggle", t_100ns=5_000)
    s.secure(t_100ns=5_200)
    with pytest.raises(RunStateError):
        s.discard(confirmed=True, has_audio=True, deletion_contract_hash="d" * 64, t_100ns=6_000)
