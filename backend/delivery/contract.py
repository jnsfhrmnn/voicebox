"""JFW-8: Delivery State Contract (I/O-frei).

Spec „Delivery State Contract" (exakte Zustandstabelle inkl. Budget-Spalte):

* ``attempt_intent_committed`` verbraucht das automatische Budget dauerhaft —
  und zwar VOR der externen Eingabe (Exactly-once).
* ``unknown`` verbraucht das Budget ebenfalls: nie ein Auto-Retry.
* ``manual_only``: kein sicherer Auto-Adapter/Ziel — kein automatischer Versuch.
* ``deleted``: Recovery-Inhalt nach Vertrag entfernt (inhaltsfreier Tombstone).
* Waehrend einer nicht terminalen Operation entsteht keine konkurrierende
  Operation; Wiederverwendung ist ausschliesslich eine append-only
  Kindoperation mit eigenem Einmalbudget.
* Crash zwischen externer Wirkung und Ergebniscommit: Recovery stellt
  ``unknown`` — nie Retry, nie ein erfundener Ausgang.
"""
from __future__ import annotations

DELIVERY_STATES = (
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

#: Nichtterminale Zustaende, in denen die Operation aktiv geschuetzt laeuft.
ACTIVE_STATES = ("received", "persisted", "attempt_intent_committed", "attempting")

#: Terminale Zustaende (Ruhe; Loeschung nur aus diesen zulaessig).
TERMINAL_STATES = ("succeeded", "failed", "unknown", "manual_only", "deleted")

#: Zustaende, in denen das automatische Budget dauerhaft verbraucht ist.
AUTO_BUDGET_STATES = (
    "attempt_intent_committed",
    "attempting",
    "succeeded",
    "failed",
    "unknown",
)

_EVENTS = frozenset(
    {
        "persist",
        "commit_attempt_intent",
        "begin_attempt",
        "succeed",
        "fail",
        "to_unknown",
        "mark_manual",
        "delete",
    }
)

#: Exakte Transitionstabelle der Spec-Zustandsmaschine.
_TRANSITIONS = {
    ("received", "persist"): "persisted",
    ("received", "commit_attempt_intent"): "attempt_intent_committed",
    ("received", "mark_manual"): "manual_only",
    ("persisted", "commit_attempt_intent"): "attempt_intent_committed",
    ("persisted", "mark_manual"): "manual_only",
    ("attempt_intent_committed", "begin_attempt"): "attempting",
    ("attempting", "succeed"): "succeeded",
    ("attempting", "fail"): "failed",
    ("attempting", "to_unknown"): "unknown",
    ("succeeded", "delete"): "deleted",
    ("failed", "delete"): "deleted",
    ("unknown", "delete"): "deleted",
    ("manual_only", "delete"): "deleted",
    ("deleted", "delete"): "deleted",
}


def next_state(current: str, event: str) -> str | None:
    """Folgezustand oder None (keine Transition). Unbekanntes ist fail-closed."""
    if current not in DELIVERY_STATES:
        raise ValueError(f"zustand_unbekannt:{current!r}")
    if event not in _EVENTS:
        raise ValueError(f"ereignis_unbekannt:{event!r}")
    return _TRANSITIONS.get((current, event))


def is_active(state: str) -> bool:
    return state in ACTIVE_STATES


def budget_consumed(state: str) -> bool:
    """Das Einmalbudget ist genau ab ``attempt_intent_committed`` verbraucht."""
    return state in AUTO_BUDGET_STATES


def can_commit_attempt_intent(state: str) -> bool:
    return state in ("received", "persisted") and not budget_consumed(state)


def new_retry_operation(*, parent_state: str) -> dict | None:
    """``Erneut einfügen``: append-only Kindoperation — die alte Operation wird
    nie zurueckgesetzt. Nur aus Ruhezustaenden zulaessig."""
    if parent_state not in ("succeeded", "failed", "unknown", "manual_only"):
        return None
    return {
        "parent_operation_id_required": True,
        "new_target_snapshot_required": True,
        "own_auto_budget": True,
    }


def recover_to_unknown(state: str) -> str | None:
    """Crash-Pfad: moeglicherweise wirkende Versuche enden ``unknown``."""
    if state in ("attempt_intent_committed", "attempting"):
        return "unknown"
    return None
