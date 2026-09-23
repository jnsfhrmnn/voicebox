"""JFW-5: Batch-/Element-/Phasen-Zustandsmaschinen + abgeleitete Aggregation (I/O-frei).

Der Batch-Status ist ausschliesslich eine nachvollziehbare Aggregation der
Einzelelemente und behauptet keine globale Ergebnis-Transaktion.
"""
from __future__ import annotations

BATCH_STATES = (
    "draft", "validating", "ready", "running", "pausing", "paused",
    "completed", "completed_with_issues", "canceled", "blocked",
)

ITEM_TERMINAL_STATES = (
    "succeeded", "succeeded_with_warnings", "failed", "canceled",
    "blocked", "interrupted", "invalidated",
)

ITEM_STATES = ("waiting", "active", *ITEM_TERMINAL_STATES)

PHASE_STATES = (
    "requested", "waiting", "active", "succeeded", "partial",
    "failed", "canceled", "blocked", "skipped",
)

_BATCH_TRANSITIONS = {
    "draft": ("validating",),
    "validating": ("ready", "blocked", "draft"),
    "ready": ("running", "canceled", "blocked"),
    "running": ("pausing", "paused", "canceled", "blocked",
                "completed", "completed_with_issues"),
    "pausing": ("paused", "canceled", "blocked", "completed", "completed_with_issues"),
    "paused": ("running", "canceled", "blocked", "completed", "completed_with_issues"),
    "completed": (),
    "completed_with_issues": (),
    "canceled": (),
    "blocked": (),
}

_ITEM_TRANSITIONS = {
    "waiting": ("active", "canceled", "blocked", "invalidated"),
    "active": ("succeeded", "succeeded_with_warnings", "failed", "canceled",
               "blocked", "interrupted", "invalidated"),
    # interrupted ist fortsetzbar, aber nie ein fachlicher Erfolg.
    "interrupted": ("waiting", "active", "canceled", "failed", "blocked", "invalidated"),
    "succeeded": (),
    "succeeded_with_warnings": (),
    "failed": (),
    "canceled": (),
    "blocked": (),
    "invalidated": (),
}

_PHASE_TRANSITIONS = {
    "requested": ("waiting", "active", "canceled", "blocked", "skipped"),
    "waiting": ("active", "canceled", "blocked", "skipped"),
    "active": ("succeeded", "partial", "failed", "canceled", "blocked"),
    "succeeded": (),
    "partial": (),
    "failed": (),
    "canceled": (),
    "blocked": (),
    "skipped": (),
}

_TRANSITIONS = {
    "batch": _BATCH_TRANSITIONS,
    "item": _ITEM_TRANSITIONS,
    "phase": _PHASE_TRANSITIONS,
}


def can_transition(kind: str, current: str, target: str) -> bool:
    table = _TRANSITIONS.get(kind)
    if table is None:
        return False
    return target in table.get(current, ())


def end_state_for(phases: dict, warnings: list, partial_policy: str | None = None) -> str:
    """Genau ein erklbaerer Endzustand aus den Phasenzustaenden."""
    statuses = list(phases.values())
    if "failed" in statuses:
        return "failed"
    if "partial" in statuses and partial_policy == "element_blockieren":
        return "failed"
    if "canceled" in statuses:
        return "canceled"
    if "blocked" in statuses:
        return "blocked"
    if warnings or "partial" in statuses or "skipped" in statuses:
        return "succeeded_with_warnings"
    return "succeeded"


def aggregate_batch(item_states: list[str]) -> dict:
    """Reine Element-Aggregation: keine globale Ergebnis-Transaktion."""
    counts: dict[str, int] = {}
    for state in item_states:
        counts[state] = counts.get(state, 0) + 1
    unique = set(item_states)
    if unique == {"succeeded"}:
        status = "completed"
    elif unique == {"canceled"}:
        status = "canceled"
    elif unique <= {"blocked", "invalidated"}:
        status = "blocked"
    else:
        status = "completed_with_issues"
    return {"status": status, "counts": counts}
