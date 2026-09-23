"""JFW-13: belegpflichtige Aufgabenliste (I/O-frei).

Belegpflicht: jeder Eintrag enthaelt genau eine Aufforderung, ggf. zustaendige
Person als Pseudonym/Rolle, Frist-/Statushinweis und MINDESTENS EINE existierende
Belegreferenz (Turn-IDs + Zeitspanne) aus dem JFW-4-Snapshot. Eine Aufgabe ohne
belegbare Passage wird nicht ausgegeben (keine erfundenen Aufgaben).
Tätigkeiten ohne Adressierung (Entscheidungen, Feststellungen) werden nie zur
Aufgabe umgedeutet. Mehrfach genannte Aufgaben erscheinen GENAU EINMAL im
spaetesten belegbaren Stand mit allen betroffenen Belegreferenzen.
"""
from __future__ import annotations

UNBELEGT = "aufgabe_ohne_beleg_verworfen"


def _turn_bounds(turns) -> dict:
    return {t["turn_id"]: (int(t["start_ms"]), int(t["end_ms"])) for t in turns}


def valid_evidence(refs, turns) -> list[dict]:
    """Belegreferenzen gegen den Snapshot pruefen — nur existierende zählen."""
    bounds = _turn_bounds(turns)
    valid: list[dict] = []
    for ref in refs or []:
        turn_ids = ref.get("turn_ids") or []
        if not turn_ids or any(t not in bounds for t in turn_ids):
            continue
        start_ms = ref.get("start_ms")
        end_ms = ref.get("end_ms")
        if start_ms is None or end_ms is None or int(start_ms) >= int(end_ms):
            continue
        lo = min(bounds[t][0] for t in turn_ids)
        hi = max(bounds[t][1] for t in turn_ids)
        if int(start_ms) < lo or int(end_ms) > hi:
            continue  # keine erfundene Praezision
        valid.append({
            "turn_ids": list(turn_ids),
            "start_ms": int(start_ms),
            "end_ms": int(end_ms),
        })
    return valid


def _group_key(proposal: dict) -> str:
    return str(proposal.get("task_group") or "").strip() or str(
        proposal.get("action") or "").strip().lower()


def _latest_end(evidence: list[dict]) -> int:
    return max((e["end_ms"] for e in evidence), default=-1)


def build_task_list(proposals, turns, assignment: dict | None = None) -> dict:
    assignment = assignment or {}
    tasks: list[dict] = []
    verworfen: list[dict] = []
    getrennte_punkte: list[dict] = []
    groups: dict[str, dict] = {}

    for proposal in proposals or []:
        kind = str(proposal.get("kind") or "aufgabe")
        evidence = valid_evidence(proposal.get("evidence"), turns)
        if kind != "aufgabe":
            getrennte_punkte.append({
                "task_id": proposal.get("task_id"),
                "kind": kind,
                "action": proposal.get("action"),
                "evidence": evidence,
            })
            continue
        if not evidence:
            verworfen.append({
                "task_id": proposal.get("task_id"),
                "reason_code": UNBELEGT,
            })
            continue

        assignee = None
        raw_assignee = proposal.get("assignee")
        if isinstance(raw_assignee, dict):
            assignee = assignment.get(raw_assignee.get("candidate_id"))
        elif isinstance(raw_assignee, str):
            assignee = raw_assignee

        entry = {
            "task_id": proposal.get("task_id"),
            "action": str(proposal.get("action") or "").strip(),
            "assignee": assignee,
            "due": proposal.get("due"),
            "status_hint": proposal.get("status_hint"),
            "evidence": evidence,
        }
        key = _group_key(proposal)
        current = groups.get(key)
        if current is None:
            groups[key] = entry
        else:
            # Duplikat: spaetester belegbarer Stand gewinnt, Belege verschmelzen
            merged = current["evidence"] + [
                e for e in evidence if e not in current["evidence"]]
            current["evidence"] = merged
            if _latest_end(evidence) >= _latest_end(current["evidence"]):
                current.update({
                    "action": entry["action"],
                    "assignee": entry["assignee"],
                    "due": entry["due"],
                    "status_hint": entry["status_hint"],
                })

    tasks = list(groups.values())
    return {"tasks": tasks, "verworfen": verworfen, "getrennte_punkte": getrennte_punkte}
