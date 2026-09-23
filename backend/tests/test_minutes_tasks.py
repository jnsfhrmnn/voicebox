"""JFW-13: belegpflichtige Aufgabenliste — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_tasks.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.pseudonym import confirm_register, label_map, propose_register
from backend.minutes.tasks import build_task_list
from backend.tests.jfw13_sources import (
    candidates_names,
    names_doc,
    tasks_duplicates,
    tasks_proposals,
)

TURN_ORDER = {"t1": 0, "t2": 1, "t3": 2}


def assignment():
    r = confirm_register(propose_register(candidates_names(), TURN_ORDER))
    return label_map(r)


def turns_of_doc():
    return names_doc()["turns"]


def test_evidenced_task_is_emitted():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    assert len(out["tasks"]) == 1
    task = out["tasks"][0]
    assert task["action"] == "Aufgabe uebernehmen"
    assert task["due"] == "Freitag"
    assert task["evidence"]


def test_assignee_resolves_to_pseudonym():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    assert out["tasks"][0]["assignee"] == "Person 2"


def test_decision_is_not_turned_into_task():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    actions = [t["action"] for t in out["tasks"]]
    assert "Option A gewaehlt" not in actions
    kinds = {p["kind"] for p in out["getrennte_punkte"]}
    assert "entscheidung" in kinds


def test_unevidenced_task_is_dropped_visibly():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    assert any(w["reason_code"] == "aufgabe_ohne_beleg_verworfen" for w in out["verworfen"])


def test_task_without_any_evidence_is_dropped():
    proposal = [{"task_id": "x", "kind": "aufgabe", "action": "Ohne Beleg", "evidence": []}]
    out = build_task_list(proposal, turns_of_doc(), assignment())
    assert out["tasks"] == []
    assert out["verworfen"]


def test_evidence_outside_turn_bounds_is_dropped():
    proposal = [{"task_id": "x", "kind": "aufgabe", "action": "Falsche Zeit",
                 "evidence": [{"turn_ids": ["t1"], "start_ms": 0, "end_ms": 999_999}]}]
    out = build_task_list(proposal, turns_of_doc(), assignment())
    assert out["tasks"] == []


def test_duplicates_appear_exactly_once_at_latest_state():
    out = build_task_list(tasks_duplicates(), turns_of_doc(), assignment())
    assert len(out["tasks"]) == 1
    task = out["tasks"][0]
    assert task["action"] == "Aufgabe uebernehmen"  # spaetester belegbarer Stand
    assert len(task["evidence"]) == 2  # beide Belegreferenzen betroffen
    assert any(e["turn_ids"] == ["t1"] for e in task["evidence"])
    assert any(e["turn_ids"] == ["t2"] for e in task["evidence"])


def test_task_has_exactly_one_action():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    for task in out["tasks"]:
        assert isinstance(task["action"], str)
        assert task["action"].strip()
        assert "\n" not in task["action"]


def test_evidence_refs_exist_in_snapshot():
    out = build_task_list(tasks_proposals(), turns_of_doc(), assignment())
    known = {t["turn_id"] for t in turns_of_doc()}
    for task in out["tasks"]:
        for ref in task["evidence"]:
            assert set(ref["turn_ids"]) <= known
