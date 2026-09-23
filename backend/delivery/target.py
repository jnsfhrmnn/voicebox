"""JFW-8: Ziel-Snapshot und Trust Boundary (I/O-frei).

Spec AC „Ziel-Snapshot und Trust Boundary":

* Vor Aufnahmebeginn wird ein Ziel-Snapshot mit Run-ID (JFW-6-Format, Reuse),
  Prozess-/Fenster-/Control-Identitaet, Session-/Desktopgrenze, Capability und
  Erfassungszeit gebunden.
* Titel, Prozessname oder Bildschirmposition allein genuegen nie als sichere
  Bindung — die Identitaet ist ein Verbund.
* Erhoeht, geschuetzt, nicht editierbar, passwortartig, Secure Desktop oder
  ausserhalb derselben bestaetigten Sitzung: Einfuegung fail-closed blockiert.
* Revalidierung: geaendertes/beendetes/neu erzeugtes Ziel wird nie durch ein
  anderes ersetzt — die Ablehnung ist der einzige zulaessige Ausgang.
"""
from __future__ import annotations

from ..recording.run_identity import is_valid_run_id

#: Capability-Kette: direkte Textschnittstelle > verifizierter Paste > manuell > blockiert.
CAPABILITIES = ("direct_text", "verified_paste", "manual_only", "blocked")

#: Trust-Boundary-Flags: jedes einzelne blockiert automatische Einfuegung.
BLOCKING_FLAGS = (
    "elevated",
    "protected",
    "not_editable",
    "password_like",
    "secure_desktop",
    "other_session",
)

_REQUIRED_FIELDS = (
    "run_id",
    "process_identity",
    "window_handle",
    "control_path",
    "session_id",
    "desktop",
    "capability",
    "captured_at_100ns",
    "binding_basis",
)

_PROCESS_FIELDS = ("pid", "started_at_100ns", "exe_hash")


def build_target_snapshot(**fields) -> dict:
    """Zielsnapshot mit normierten Listenfeldern (konstruktiv inhaltsfrei)."""
    snapshot = dict(fields)
    snapshot["blocking_flags"] = list(snapshot.get("blocking_flags") or [])
    return snapshot


def validate_target(snapshot: dict) -> list[str]:
    """Fail-closed Validierung des Ziel-Snapshots; leere Liste = vertragsgemaess."""
    errors: list[str] = []
    snapshot = snapshot or {}
    for name in _REQUIRED_FIELDS:
        if name not in snapshot:
            errors.append(f"pflichtfeld_fehlt:{name}")
    if not is_valid_run_id(snapshot.get("run_id")):
        errors.append("run_id_unzulaessig")
    if snapshot.get("capability") not in CAPABILITIES:
        errors.append("capability_unbekannt")
    captured = snapshot.get("captured_at_100ns")
    if not isinstance(captured, int) or captured <= 0:
        errors.append("zeitpunkt_unzulaessig")
    process = snapshot.get("process_identity") or {}
    if any(name not in process for name in _PROCESS_FIELDS):
        errors.append("identitaet_unzureichend")
    if snapshot.get("binding_basis") != "composite":
        # Titel, Prozessname oder Bildschirmposition allein sind nie Bindung.
        errors.append("schwache_identitaet")
    flags = snapshot.get("blocking_flags") or []
    for flag in flags:
        errors.append(f"ziel_blocked:{flag}")
    if flags and snapshot.get("capability") != "blocked":
        errors.append("blocked_muss_blocked_sein")
    return errors


def revalidate_target(snapshot: dict, current: dict | None) -> dict:
    """Erneute Zielpruefung vor der Uebergabe (Verifikations-Doppelauslosung
    liegt in ``verification``). Geaenderte, beendete oder neu erzeugte Ziele
    werden nie durch ein anderes ersetzt — ``ok: False`` ist der einzige
    zulaessige Ausgang bei Abweichung.
    """
    snapshot = snapshot or {}
    if not current:
        return {"ok": False, "reason": "target_changed"}
    for name in _PROCESS_FIELDS:
        if (snapshot.get("process_identity") or {}).get(name) != (
            current.get("process_identity") or {}
        ).get(name):
            return {"ok": False, "reason": "target_changed"}
    if snapshot.get("window_handle") != current.get("window_handle"):
        return {"ok": False, "reason": "target_changed"}
    if snapshot.get("control_path") != current.get("control_path"):
        return {"ok": False, "reason": "target_changed"}
    if (
        snapshot.get("session_id") != current.get("session_id")
        or snapshot.get("desktop") != current.get("desktop")
    ):
        return {"ok": False, "reason": "session_geaendert"}
    if snapshot.get("capability") != current.get("capability"):
        return {"ok": False, "reason": "capability_geaendert"}
    if current.get("blocking_flags"):
        return {"ok": False, "reason": "ziel_blocked"}
    return {"ok": True, "reason": None}
