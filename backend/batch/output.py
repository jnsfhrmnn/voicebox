"""JFW-5: deterministische Ausgabezuordnung (I/O-frei).

Ziel: Relativpfad der Quelle unter dem bestaetigten Zielwurzelverzeichnis.
Kollisionen (mehrere Elemente auf denselben Zielstamm) und bereits belegte
Ziele sind vor Start sichtbar; die bestaetigte Regel ist ``blockieren`` oder
``element_suffix``. Ueberschreiben ist grundsaetzlich verboten.
"""
from __future__ import annotations

import ntpath

from .identity import path_identity_key


def _target_for(element: dict, policy: dict) -> str:
    rel = str(element.get("relative_path") or ntpath.basename(element["source"]["path"]))
    return ntpath.normpath(ntpath.join(policy["target_root"], rel))


def _suffixed(target: str, item_id: str) -> str:
    drive, rest = ntpath.splitdrive(target)
    stem, ext = ntpath.splitext(rest)
    return drive + stem + "__" + item_id[-6:] + ext


def assign_outputs(elements: list[dict], policy: dict) -> list[dict]:
    """Haengt je Element eine deterministische Zielzuordnung an (Kopien)."""
    assignments = [dict(e) for e in elements]
    counts: dict[str, int] = {}
    for el in assignments:
        key = path_identity_key(_target_for(el, policy))
        counts[key] = counts.get(key, 0) + 1
    for el in assignments:
        target = _target_for(el, policy)
        collision = counts[path_identity_key(target)] > 1
        el["output"] = {
            "target_path": target,
            "collision": bool(collision),
            "decision": "kollision" if collision else "primaer",
            "reason_code": "zielkollision" if collision else None,
        }
    return assignments


def resolve_conflicts(assignments: list[dict], policy: dict, existing_targets) -> dict:
    """Loest Kollisionen/belegte Ziele ueber die bestaetigte Regel — nie ueberschreiben."""
    existing = {path_identity_key(str(t)) for t in (existing_targets or set())}
    rule = policy["conflict_rule"]
    out = [dict(e, output=dict(e["output"])) for e in assignments]
    if rule == "blockieren":
        for el in out:
            if el["output"]["collision"]:
                return {"ok": False, "reason_code": "zielkollision_blockiert", "assignments": out}
            if path_identity_key(el["output"]["target_path"]) in existing:
                return {"ok": False, "reason_code": "ziel_belegt", "assignments": out}
        return {"ok": True, "reason_code": None, "assignments": out}
    if rule == "element_suffix":
        seen = set()
        for el in out:
            target = el["output"]["target_path"]
            if el["output"]["collision"] or path_identity_key(target) in existing:
                target = _suffixed(target, el["item_id"])
                el["output"]["decision"] = "element_suffix"
            if path_identity_key(target) in existing or path_identity_key(target) in seen:
                return {"ok": False, "reason_code": "ziel_belegt", "assignments": out}
            seen.add(path_identity_key(target))
            el["output"]["target_path"] = target
            el["output"]["collision"] = False
            el["output"]["reason_code"] = None
        return {"ok": True, "reason_code": None, "assignments": out}
    return {"ok": False, "reason_code": "konfliktregel_unbekannt", "assignments": out}
