"""JFW-5: Crash-Recovery- und Resume-Vertrag (I/O-frei).

Nach Absturz/Neustart werden aktive Versuche als ``interrupted`` markiert —
fortsetzbar und ausdruecklich KEIN fachlicher Erfolg. Bereits autoritativ
committete Ergebnisse bleiben unangetastet. Die Wiederaufnahme erfordert eine
ausdrueckliche Nutzerbestaetigung und rekonstruiert fehlende Optionen NIEMALS
aus aktuellen globalen Defaults: fehlt der urspruengliche Vertrag, wird
fail-closed blockiert.
"""
from __future__ import annotations

from .profile import validate_profile
from .provenance import CONTRACT_VERSION

ACTIVE_ATTEMPT_STATES = ("active", "running", "pending")


def mark_interrupted(attempts: list[dict], *, current_epoch: str) -> list[dict]:
    """Markiert nicht terminale Versuche ``interrupted`` (idempotent, append-klar).

    ``current_epoch`` wird als ``interrupted_at_epoch`` auf die markierten
    Versuche geschrieben (USCRX-2026-16007/P-02): der Parameter war bisher
    ungenutzt und vermittelte einen Vertrag, den der Code nicht hielt.
    """
    out = []
    for attempt in attempts:
        a = dict(attempt)
        if a.get("status") in ACTIVE_ATTEMPT_STATES:
            a["status"] = "interrupted"
            a["reason_code"] = "unterbrochen"
            a["resumable"] = True
            a["interrupted_at_epoch"] = current_epoch
        elif a.get("status") != "interrupted":
            a["resumable"] = False
        out.append(a)
    return out


def resume_plan(snapshot: dict, *, confirmed_original: bool) -> dict:
    """Fail-closed Resume-Pruefung; das Profil stammt AUSSCHLIESSLICH aus dem Snapshot."""
    if not confirmed_original:
        return {"ok": False, "reason_code": "nicht_bestaetigt", "profile": None}
    if not isinstance(snapshot, dict) or snapshot.get("contract_version") != CONTRACT_VERSION:
        return {"ok": False, "reason_code": "vertrag_nicht_wiederherstellbar", "profile": None}
    profile = snapshot.get("profile")
    if validate_profile(profile or {}):
        return {"ok": False, "reason_code": "vertrag_nicht_wiederherstellbar", "profile": None}
    return {"ok": True, "reason_code": None, "profile": dict(profile)}
