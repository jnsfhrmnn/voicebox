"""JFW-8: Scroll-Lock-Vertrag und Verifikations-Doppelauslösung (TDD, RED zuerst).

Leitsätze: jede zielautorisierte Verifikation wird zweimal unabhängig
ausgelöst — nur zwei gleiche Ausgaben gelten als bestätigt (Abweichung =
``unsicher``). Der Scroll-Lock-Vertrag ist die appübergreifende, inhaltsfreie
Erreichbarkeitsprobe vor externer Tastatureingabe: toggeln, auslesen,
zurücktoggeln, auslesen; bleibt der Zustandswechsel aus, ist synthetische
Eingabe nicht zustellbar (``input_not_reachable``), kein Versuch. Fehlender
Provider ist fail-closed ``provider_runtime_missing``.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_verification.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.delivery.verification import (
    BESTAETIGT,
    UNSICHER,
    double_verify,
    run_scroll_lock_contract,
    verify_target_effect,
)
from backend.transcription.raw_transcript import text_hash


class FakeProbe:
    """Injizierbare Scroll-Lock-Probe (Zielsystem-Seam)."""

    def __init__(self, initial: bool = False, frozen: bool = False, flaky_restore: bool = False):
        self._state = initial
        self._frozen = frozen
        self._flaky_restore = flaky_restore
        self.toggle_calls = 0

    def toggle(self) -> None:
        self.toggle_calls += 1
        if self._frozen:
            return
        if self._flaky_restore and self.toggle_calls >= 2:
            return  # Wiederherstellung schlägt still fehl
        self._state = not self._state

    def state(self) -> bool:
        return self._state

def test_doppelauslsung_zwei_gleiche_ausgaben_gelten_bestaetigt():
    out = double_verify(lambda: {"pid": 1})
    assert out["status"] == BESTAETIGT
    assert out["value"] == {"pid": 1}
    assert out["reads"] == 2

def test_doppelauslsung_abweichung_ist_unsicher():
    values = iter([{"pid": 1}, {"pid": 2}])
    out = double_verify(lambda: next(values))
    assert out["status"] == UNSICHER
    assert out["value"] is None

def test_scroll_lock_vertrag_uebergabe_und_wiederherstellung():
    probe = FakeProbe(initial=False)
    out = run_scroll_lock_contract(probe)
    assert out == {
        "ok": True,
        "reason": None,
        "restored": True,
        "actuations": 2,
    }
    assert probe.toggle_calls == 2
    assert probe.state() is False  # Ausgangszustand wiederhergestellt

def test_scroll_lock_ohne_zustandswechsel_ist_unzustellbar():
    probe = FakeProbe(frozen=True)
    out = run_scroll_lock_contract(probe)
    assert out["ok"] is False
    assert out["reason"] == "input_not_reachable"
    assert probe.toggle_calls == 2  # Vertrag versucht Auslösung UND Rückweg

def test_scroll_lock_fehlende_wiederherstellung_ist_fail_closed():
    probe = FakeProbe(flaky_restore=True)
    out = run_scroll_lock_contract(probe)
    assert out["ok"] is False
    assert out["reason"] == "restore_unsicher"
    assert out["restored"] is False

def test_scroll_lock_ohne_provider_ist_fail_closed():
    out = run_scroll_lock_contract(None)
    assert out == {
        "ok": False,
        "reason": "provider_runtime_missing",
        "restored": False,
        "actuations": 0,
    }

def test_wirkungsverifikation_nur_bei_doppelt_bestaetigtem_zielinhalt():
    expected = text_hash("fertig.")
    out = verify_target_effect(
        read_effect=lambda: text_hash("fertig."), expected_text_hash=expected
    )
    assert out["status"] == BESTAETIGT
    # Abweichende Lesart: unsicher, kein Erfolg.
    values = iter([text_hash("fertig."), text_hash("fetig.")])
    out = verify_target_effect(
        read_effect=lambda: next(values), expected_text_hash=expected
    )
    assert out["status"] == UNSICHER

def test_probes_sind_inhaltsfrei():
    # Der Scroll-Lock-Vertrag berührt nie Text, Zwischenablage oder Titel.
    calls = []

    def read():
        calls.append(1)
        return {"toggle_state": True}

    out = double_verify(read)
    assert out["status"] == BESTAETIGT
    assert len(calls) == 2
