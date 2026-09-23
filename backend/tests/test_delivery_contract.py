"""JFW-8: Delivery State Contract — Vertragstests (TDD, RED zuerst).

Spec „Delivery State Contract": received/persisted/attempt_intent_committed/
attempting/succeeded/failed/unknown/manual_only/deleted mit Budget-Spalte.
Höchstens ein automatischer Versuch pro Operation (``attempt_intent_committed``
verbraucht das Budget dauerhaft VOR externer Eingabe); ``unknown`` verbraucht es
ebenfalls; kein Auto-Retry; Retry = append-only Kindoperation mit eigenem Budget;
während aktiver Operation keine konkurrierende Operation; Crash zwischen
Wirkung und Commit -> ``unknown`` (nie Retry).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.contract import (
    ACTIVE_STATES,
    DELIVERY_STATES,
    TERMINAL_STATES,
    budget_consumed,
    can_commit_attempt_intent,
    is_active,
    new_retry_operation,
    next_state,
    recover_to_unknown,
)


def test_zustandsmaschine_exakt_nach_spec_tabelle():
    assert DELIVERY_STATES == (
        "received",
        "persisted",
        "attempt_intent_committed",
        "attempting",
        "succeeded",
        "failed",
        "unknown",
        "manual_only",
        "deleted",
    )
    assert next_state("received", "persist") == "persisted"
    assert next_state("persisted", "commit_attempt_intent") == "attempt_intent_committed"
    assert next_state("attempt_intent_committed", "begin_attempt") == "attempting"
    assert next_state("attempting", "succeed") == "succeeded"
    assert next_state("attempting", "fail") == "failed"
    assert next_state("attempting", "to_unknown") == "unknown"

def test_exactly_once_budget():
    assert budget_consumed("persisted") is False
    assert budget_consumed("received") is False
    for state in (
        "attempt_intent_committed",
        "attempting",
        "succeeded",
        "failed",
        "unknown",
    ):
        assert budget_consumed(state) is True, state
    # Budget nur einmal: ein zweiter Intent ist keine gültige Transition.
    assert next_state("attempt_intent_committed", "commit_attempt_intent") is None
    assert next_state("attempting", "commit_attempt_intent") is None
    assert can_commit_attempt_intent("persisted") is True
    assert can_commit_attempt_intent("attempt_intent_committed") is False

def test_manual_only_ohne_automatischen_versuch():
    assert next_state("received", "mark_manual") == "manual_only"
    assert next_state("persisted", "mark_manual") == "manual_only"
    assert budget_consumed("manual_only") is False
    # Nach einem Versuch gibt es kein Zurück auf manual_only.
    assert next_state("attempting", "mark_manual") is None

def test_keine_konkurrierende_operation():
    for state in ACTIVE_STATES:
        assert is_active(state) is True
        assert new_retry_operation(parent_state=state) is None
    assert is_active("succeeded") is False

def test_retry_ist_append_only_mit_eigenem_budget():
    for state in ("succeeded", "failed", "unknown", "manual_only"):
        spec = new_retry_operation(parent_state=state)
        assert spec == {
            "parent_operation_id_required": True,
            "new_target_snapshot_required": True,
            "own_auto_budget": True,
        }
    # Gelöschte Operationen werden nie wiederbelebt.
    assert new_retry_operation(parent_state="deleted") is None

def test_crash_recovery_stellt_unknown_statt_retry():
    # Zwischen externer Wirkung und Ergebniscommit: Ausgang unbekannt.
    assert recover_to_unknown("attempt_intent_committed") == "unknown"
    assert recover_to_unknown("attempting") == "unknown"
    # Vor dem Intent ist nichts passiert — kein erfundener Ausgang.
    assert recover_to_unknown("received") is None
    assert recover_to_unknown("persisted") is None
    # Terminale Ausgänge bleiben unangetastet.
    assert recover_to_unknown("succeeded") is None

def test_loeschung_nur_aus_ruhe_und_idempotent():
    for state in ("received", "persisted", "attempt_intent_committed", "attempting"):
        assert next_state(state, "delete") is None
    for state in ("succeeded", "failed", "unknown", "manual_only"):
        assert next_state(state, "delete") == "deleted"
    assert next_state("deleted", "delete") == "deleted"

def test_unbekannte_zustaende_und_ereignisse_sind_fail_closed():
    with pytest.raises(ValueError, match="zustand_unbekannt"):
        next_state("hubschrauber", "persist")
    with pytest.raises(ValueError, match="ereignis_unbekannt"):
        next_state("received", "teleport")
    for state in DELIVERY_STATES:
        assert state in ACTIVE_STATES or state in TERMINAL_STATES
