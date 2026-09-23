"""JFW-5: Element-Pipeline — Abarbeitung in Serie ueber injizierbare Phasen-Executor (I/O-frei).

Jede Phase laeuft mit derselben Element-, Quellen- und Profilrevision. Ein
autoritativer Phasen-Commit (``commit_ref``) ist der einzige sichere Checkpoint
und bleibt auch bei Fehlern, Abbruch oder Teilfehler erhalten. Die
Teilfehlerpolitik gilt pro Batch konstant: ``mit_belegten_daten_fortfahren``
oder ``element_blockieren``. Ein Erfolg ohne validierten eigenen Commit ist
kein Erfolg.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .profile import phases_for, profile_hash
from .state import end_state_for


@dataclass
class PhaseOutcome:
    status: str  # succeeded | partial | failed | blocked | skipped | canceled
    commit_ref: dict | None = None
    reason_code: str | None = None
    warnings: list = field(default_factory=list)
    carry: dict = field(default_factory=dict)  # transienter Textfluss, NIE persistiert


def run_item(*, item: dict, profile: dict, executors: dict, cancel_requested=None) -> dict:
    """Fuehrt die bestaetigte Phasenreihenfolge eines Elements aus (Serie)."""
    phases = phases_for(profile)
    p_hash = profile_hash(profile)
    policy = profile["partial_failure_policy"]
    phase_info: dict[str, dict] = {}
    commits: dict[str, dict] = {}
    warnings: list = []
    carry: dict = {}  # Zwischendaten (z. B. Text) fließen transient, nicht in Commits
    upstream_blocked: str | None = None

    for phase in phases:
        base = {
            "phase": phase,
            "item_id": item["item_id"],
            "profile_hash": p_hash,
            "source_path": item.get("source", {}).get("path"),
        }
        if cancel_requested is not None and cancel_requested():
            phase_info[phase] = {**base, "status": "canceled",
                                 "reason_code": "nutzerabbruch", "commit_ref": None}
            upstream_blocked = "nutzerabbruch"
            continue
        if upstream_blocked is not None:
            phase_info[phase] = {
                **base,
                "status": "canceled" if upstream_blocked == "nutzerabbruch" else "blocked",
                "reason_code": upstream_blocked if upstream_blocked == "nutzerabbruch" else "upstream_fehlt",
                "commit_ref": None,
            }
            continue

        outcome = executors[phase](phase, {"item": item, "profile": profile,
                                           "commits": dict(commits),
                                           "carry": dict(carry)})
        carry.update(getattr(outcome, "carry", None) or {})
        if outcome.status == "succeeded" and not outcome.commit_ref:
            outcome = PhaseOutcome(status="failed", commit_ref=None,
                                   reason_code="kein_autoritativer_commit",
                                   warnings=list(outcome.warnings))
        phase_info[phase] = {
            **base,
            "status": outcome.status,
            "reason_code": outcome.reason_code,
            "commit_ref": outcome.commit_ref,
        }
        warnings.extend(outcome.warnings or [])
        if outcome.status in ("succeeded", "partial") and outcome.commit_ref:
            commits[phase] = outcome.commit_ref
        if outcome.status == "failed" or (
            outcome.status == "partial" and not outcome.commit_ref
        ):
            upstream_blocked = outcome.reason_code or "phase_fehlgeschlagen"
        elif outcome.status == "partial" and policy == "element_blockieren":
            upstream_blocked = "element_blockieren"
        elif outcome.status in ("blocked", "canceled", "skipped"):
            upstream_blocked = outcome.reason_code or outcome.status

    return {
        "item_id": item["item_id"],
        "profile_hash": p_hash,
        "phases": phase_info,
        "commits": commits,
        "warnings": warnings,
        "end_state": end_state_for(
            {p: phase_info[p]["status"] for p in phase_info}, warnings, policy
        ),
    }
