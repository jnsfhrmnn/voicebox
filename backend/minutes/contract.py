"""JFW-13: Zustandsmaschine exakt nach Spec „Protokoll Result Contract" (I/O-frei)."""
from __future__ import annotations

STATES = (
    "queued",
    "waiting_for_local_artifact",
    "preparing",
    "generating",
    "generated",
    "generated_with_warnings",
    "failed",
    "canceled",
    "invalidated",
)

NON_TERMINAL_STATES = (
    "queued",
    "waiting_for_local_artifact",
    "preparing",
    "generating",
)

# Teilfehler aus JFW-2/3/11 oder unsichere Pseudonyme -> generated_with_warnings.
WARNING_STATE_KINDS = {
    "partially_aligned",
    "partially_diarized",
    "secured_partial",
    "sync_unsicher",
    "single_source",
    "dual_source_partial",
    "dedupe_unsicher",
    "pseudonym_unklar",
    "pseudonym_nicht_ersetzbar",
}

_ALLOWED = {
    "queued": {"waiting_for_local_artifact", "preparing", "generating",
               "failed", "canceled", "invalidated"},
    "waiting_for_local_artifact": {"preparing", "generating", "queued",
                                   "failed", "canceled", "invalidated"},
    "preparing": {"generating", "failed", "canceled", "invalidated"},
    "generating": {"generated", "generated_with_warnings", "failed",
                   "canceled", "invalidated"},
    "generated": {"invalidated"},
    "generated_with_warnings": {"invalidated"},
    "failed": set(),
    "canceled": set(),
    "invalidated": set(),
}


def can_transition(old: str, new: str) -> bool:
    return new in _ALLOWED.get(old, set())


def outcome_state(warnings) -> str:
    """Terminaler Erfolgszustand: Teilfehler/unsichere Pseudonyme bleiben sichtbar."""
    for warning in warnings or []:
        if warning.get("kind") in WARNING_STATE_KINDS:
            return "generated_with_warnings"
    return "generated"
