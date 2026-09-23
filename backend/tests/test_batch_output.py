"""JFW-5: deterministische Ausgabezuordnung und Konfliktregeln — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_output.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.output import assign_outputs, resolve_conflicts


def element(item_id, path, rel):
    return {"item_id": item_id, "source": {"path": path}, "relative_path": rel}


def policy(rule="blockieren", target_root="C:/out"):
    return {"target_root": target_root, "structure": "relative_source", "conflict_rule": rule}


def test_assignment_is_deterministic_and_keeps_relative_origin():
    els = [
        element("jfw5-item-aaaa", "C:/data/sub/deep.mp3", "sub/deep.mp3"),
        element("jfw5-item-bbbb", "C:/data/clip.wav", "clip.wav"),
    ]
    a = assign_outputs(els, policy())
    b = assign_outputs(list(reversed(els)), policy())
    by_id_a = {e["item_id"]: e["output"]["target_path"] for e in a}
    by_id_b = {e["item_id"]: e["output"]["target_path"] for e in b}
    assert by_id_a == by_id_b
    assert "sub" in by_id_a["jfw5-item-aaaa"]
    assert by_id_a["jfw5-item-aaaa"] != by_id_a["jfw5-item-bbbb"]


def test_collisions_are_visible_before_start():
    els = [
        element("jfw5-item-aaaa", "C:/data/a/clip.wav", "a/clip.wav"),
        element("jfw5-item-bbbb", "C:/data/b/clip.wav", "b/clip.wav"),
    ]
    out = assign_outputs(els, policy(target_root="C:/out"))
    # unterschiedliche Relativpfade -> keine Kollision
    assert all(not e["output"]["collision"] for e in out)


def test_same_relative_target_collides():
    els = [
        element("jfw5-item-aaaa", "C:/data/a/clip.wav", "clip.wav"),
        element("jfw5-item-bbbb", "C:/data/b/clip.wav", "clip.wav"),
    ]
    out = assign_outputs(els, policy())
    assert all(e["output"]["collision"] for e in out)


def test_conflict_rule_blockieren_marks_batch_unstartable():
    els = [
        element("jfw5-item-aaaa", "C:/data/a/clip.wav", "clip.wav"),
        element("jfw5-item-bbbb", "C:/data/b/clip.wav", "clip.wav"),
    ]
    out = resolve_conflicts(assign_outputs(els, policy("blockieren")), policy("blockieren"), set())
    assert out["ok"] is False
    assert out["reason_code"] == "zielkollision_blockiert"


def test_conflict_rule_element_suffix_resolves_deterministically():
    els = [
        element("jfw5-item-aaaa", "C:/data/a/clip.wav", "clip.wav"),
        element("jfw5-item-bbbb", "C:/data/b/clip.wav", "clip.wav"),
    ]
    out = resolve_conflicts(assign_outputs(els, policy("element_suffix")), policy("element_suffix"), set())
    assert out["ok"] is True
    targets = [e["output"]["target_path"] for e in out["assignments"]]
    assert len(set(targets)) == 2
    assert any("__" in t for t in targets)
    # erneuter Lauf: identische Zuordnung
    again = resolve_conflicts(assign_outputs(els, policy("element_suffix")), policy("element_suffix"), set())
    assert [e["output"]["target_path"] for e in again["assignments"]] == targets


def test_existing_target_is_never_silently_overwritten():
    els = [element("jfw5-item-aaaa", "C:/data/clip.wav", "clip.wav")]
    assigned = assign_outputs(els, policy("blockieren"))
    target = assigned[0]["output"]["target_path"]
    out = resolve_conflicts(assigned, policy("blockieren"), {target})
    assert out["ok"] is False
    assert out["reason_code"] == "ziel_belegt"
    out2 = resolve_conflicts(assign_outputs(els, policy("element_suffix")), policy("element_suffix"), {target})
    assert out2["ok"] is True
    assert out2["assignments"][0]["output"]["target_path"] != target
