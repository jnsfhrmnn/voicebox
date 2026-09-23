"""JFW-5: Retry-Vertrag (I/O-frei).

Ein Retry ohne Einstellaenderung verwendet dieselbe Quellen-, Profil- und
Elementrevision, behaelt gueltige autoritative Commits und erzeugt einen
getrennt nachvollziehbaren Versuch fuer die erneut ausgefuehrten Phasen.
Jede fachliche Aenderung (Profil, Quelle, Ausgabezuordnung, Teilfehlerpolitik)
erfordert eine neue Revision statt einer stillen Wiederholung.
"""
from __future__ import annotations

from dataclasses import dataclass

from .profile import profile_hash

REUSABLE_PHASE_STATES = ("succeeded", "partial")


@dataclass
class RetryPlan:
    allowed: bool
    reason_code: str | None
    reused_phases: tuple = ()
    rerun_phases: tuple = ()


def plan_retry(*, item_state: dict, profile: dict, snapshot_profile_hash: str,
               source_ok: bool) -> RetryPlan:
    if not source_ok:
        return RetryPlan(False, "quelle_veraendert")
    if profile_hash(profile) != snapshot_profile_hash:
        return RetryPlan(False, "profil_geaendert_neue_revision_notwendig")
    statuses = item_state.get("phases") or {}
    reused: list[str] = []
    rerun: list[str] = []
    rerun_started = False
    for phase, status in statuses.items():
        if not rerun_started and status in REUSABLE_PHASE_STATES and phase in (item_state.get("commits") or {}):
            reused.append(phase)
        else:
            rerun_started = True
            rerun.append(phase)
    return RetryPlan(True, None, tuple(reused), tuple(rerun))
