"""JFW-8: Scroll-Lock-Vertrag und Verifikations-Doppelauslosung (I/O-frei).

Leitsaetze der sicheren appuebergreifenden Zieluebergabe:

* **Verifikations-Doppelauslosung:** jede zielautorisierte Verifikation wird
  zweimal unabhaengig ausgeloest; nur zwei gleiche Ausgaben gelten als
  bestaetigt, eine Abweichung ist ``unsicher`` (fail-closed).
* **Scroll-Lock-Vertrag:** inhaltsfreie Erreichbarkeitsprobe fuer synthetische
  Tastatureingabe ueber die appuebergreifende Uebergabe — Umschaltzustand
  toggeln, auslesen, zuruecktoggeln, auslesen. Bleibt der Zustandswechsel aus
  (z. B. UIPI-Block durch ein erhoehetes Ziel), ist Eingabe nicht zustellbar
  (``input_not_reachable``) — kein Versuch. Der Ausgangszustand wird
  wiederhergestellt.

Die echte Win32-Ausloesung ist eine injizierbare Zielsystem-Seam
(``ScrollLockProbe``); ohne Provider gilt fail-closed
``provider_runtime_missing``.
"""
from __future__ import annotations

from collections.abc import Callable

BESTAETIGT = "bestaetigt"
UNSICHER = "unsicher"

PROVIDER_RUNTIME_MISSING = "provider_runtime_missing"
INPUT_NOT_REACHABLE = "input_not_reachable"
RESTORE_UNSICHER = "restore_unsicher"


def double_verify(read: Callable[[], object]) -> dict:
    """Verifikations-Doppelauslosung: zwei unabhaengige Ausloesungen."""
    first = read()
    second = read()
    if first == second and first is not None:
        return {"status": BESTAETIGT, "value": first, "reads": 2}
    return {"status": UNSICHER, "value": None, "reads": 2}


def run_scroll_lock_contract(probe) -> dict:
    """Scroll-Lock-Vertrag: Ausloesung + Wiederherstellung, inhaltsfrei.

    ``probe`` implementiert ``toggle()`` und ``state()`` (Zielsystem-Seam).
    """
    if probe is None:
        return {
            "ok": False,
            "reason": PROVIDER_RUNTIME_MISSING,
            "restored": False,
            "actuations": 0,
        }
    initial = probe.state()
    probe.toggle()
    after_first = probe.state()
    probe.toggle()
    after_second = probe.state()
    if after_first == initial:
        # Kein Zustandswechsel: synthetische Eingabe erreicht die Pipeline nicht.
        return {
            "ok": False,
            "reason": INPUT_NOT_REACHABLE,
            "restored": after_second == initial,
            "actuations": 2,
        }
    if after_second != initial:
        # Fremde Aktuierung oder fluechtiger Zustand: fail-closed, kein Verlass.
        return {
            "ok": False,
            "reason": RESTORE_UNSICHER,
            "restored": False,
            "actuations": 2,
        }
    return {
        "ok": True,
        "reason": None,
        "restored": True,
        "actuations": 2,
    }


def verify_target_effect(*, read_effect: Callable[[], str], expected_text_hash: str) -> dict:
    """Wirkungsverifikation am Ziel: nur doppelt bestaetigter, exakt passender
    Zielinhalt gilt als bestaetigt — alles andere ist ``unsicher``."""
    out = double_verify(read_effect)
    if out["status"] == BESTAETIGT and out["value"] == expected_text_hash:
        return {"status": BESTAETIGT, "value": expected_text_hash, "reads": 2}
    return {"status": UNSICHER, "value": None, "reads": 2}
