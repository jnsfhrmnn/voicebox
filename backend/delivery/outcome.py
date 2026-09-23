"""JFW-8: Zielergebnis-Auswertung (I/O-frei).

Spec AC „Automatische Einfuegung und Ergebnis":

* ``succeeded`` nur bei eindeutig bestaetigter Zielwirkung (Verifikations-
  Doppelauslosung) — und verbraucht das Budget dauerhaft.
* ``failed`` nur bei eindeutiger Ablehnung, bevor eine Aenderung moeglich war;
  ein Retry erfolgt nur nach ausdruecklicher Nutzeraktion und neuer Zielpruefung.
* Wirkung oder Zielzustand nicht eindeutig bestaetigbar (inkl. Timeout):
  ``unknown`` — Budget verbraucht, KEIN Auto-Retry.
"""
from __future__ import annotations

from .contract import new_retry_operation
from .verification import BESTAETIGT


def evaluate_outcome(*, effect: dict | None, rejected_before_change: bool = False) -> dict:
    """Terminaler Ausgang genau eines Versuchs — niemals ein Auto-Retry."""
    if effect is not None and effect.get("status") == BESTAETIGT:
        outcome = "succeeded"
    elif rejected_before_change:
        outcome = "failed"
    else:
        outcome = "unknown"
    return {"outcome": outcome, "budget": "verbraucht", "auto_retry": False}


def evaluate_retry(
    *, parent_state: str, user_action: bool, fresh_target_check: bool
) -> dict:
    """``Erneut einfügen`` ist ausschliesslich eine ausdrueckliche Nutzeraktion
    mit neuer Zielpruefung und erzeugt eine append-only Kindoperation."""
    allowed = bool(
        user_action and fresh_target_check and new_retry_operation(parent_state=parent_state)
    )
    return {"allowed": allowed, "creates": "kindoperation", "own_auto_budget": True}
