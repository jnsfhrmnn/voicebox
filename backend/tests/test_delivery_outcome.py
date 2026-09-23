"""JFW-8: Zielergebnis-Auswertung (TDD, RED zuerst beobachtet).

Spec AC „Automatische Einfügung und Ergebnis": ``succeeded`` nur bei eindeutig
bestätigter Zielwirkung (Verifikations-Doppelauslösung), ``failed`` nur bei
eindeutiger Ablehnung bevor eine Änderung möglich war, sonst ``unknown`` —
Budget bleibt verbraucht, kein Auto-Retry. Retry nur nach ausdrücklicher
Nutzeraktion UND neuer Zielprüfung (Kindoperation).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_outcome.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.delivery.outcome import evaluate_outcome, evaluate_retry
from backend.delivery.verification import BESTAETIGT, UNSICHER


def test_eindeutig_bestaetigte_wirkung_ist_succeeded():
    out = evaluate_outcome(
        effect={"status": BESTAETIGT, "reads": 2}, rejected_before_change=False
    )
    assert out == {"outcome": "succeeded", "budget": "verbraucht", "auto_retry": False}

def test_eindeutige_ablehnung_vor_aenderung_ist_failed():
    out = evaluate_outcome(
        effect={"status": UNSICHER}, rejected_before_change=True
    )
    assert out == {"outcome": "failed", "budget": "verbraucht", "auto_retry": False}

def test_unsichere_wirkung_bleibt_unknown():
    out = evaluate_outcome(
        effect={"status": UNSICHER}, rejected_before_change=False
    )
    assert out == {"outcome": "unknown", "budget": "verbraucht", "auto_retry": False}

def test_timeout_ohne_ablehnung_bleibt_unknown():
    out = evaluate_outcome(effect=None, rejected_before_change=False)
    assert out == {"outcome": "unknown", "budget": "verbraucht", "auto_retry": False}

def test_auto_retry_gibt_es_nie():
    for effect in ({"status": BESTAETIGT}, {"status": UNSICHER}, None):
        assert evaluate_outcome(effect=effect, rejected_before_change=True)["auto_retry"] is False

def test_retry_nur_explizite_nutzeraktion_mit_neuer_zielpruefung():
    out = evaluate_retry(
        parent_state="unknown", user_action=True, fresh_target_check=True
    )
    assert out["allowed"] is True
    assert out["creates"] == "kindoperation"
    assert out["own_auto_budget"] is True
    # Ohne Nutzeraktion oder ohne frische Zielprüfung: nicht zulässig.
    assert evaluate_retry(
        parent_state="failed", user_action=False, fresh_target_check=True
    )["allowed"] is False
    assert evaluate_retry(
        parent_state="failed", user_action=True, fresh_target_check=False
    )["allowed"] is False
    # Während aktiver Operation nie.
    assert evaluate_retry(
        parent_state="attempting", user_action=True, fresh_target_check=True
    )["allowed"] is False
