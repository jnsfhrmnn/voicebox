"""JFW-5: unveraenderlicher Batch-Snapshot + Bindungspruefungen — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_snapshot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.discovery import discover
from backend.batch.identity import content_proof, new_batch_id
from backend.batch.snapshot import (
    build_snapshot,
    snapshot_hash,
    verify_consumed_bytes,
    verify_snapshot_completeness,
    verify_source_binding,
)
from backend.tests.test_batch_discovery import facts, tree_fs
from backend.tests.test_batch_profile import prof


def snap(**over):
    disc = discover([{"kind": "folder", "path": "C:/data"}], tree_fs())
    kwargs = dict(
        batch_id=new_batch_id(),
        discovery=disc,
        profile=prof(),
        revision_no=1,
        parent_snapshot_hash=None,
        created_at="2026-09-23T10:00:00Z",
    )
    kwargs.update(over)
    return build_snapshot(**kwargs)


def test_snapshot_is_complete_and_hash_stable():
    s = snap()
    assert verify_snapshot_completeness(s) == []
    assert snapshot_hash(s) == snapshot_hash(s)
    assert s["contract_version"] == "jfw5_batch_v1"


def test_same_sources_and_profile_give_identical_items_and_order():
    a, b = snap(), snap()
    ids_a = [(i["item_id"], i["order_index"]) for i in a["items"]]
    ids_b = [(i["item_id"], i["order_index"]) for i in b["items"]]
    assert ids_a == ids_b
    assert a["order"] == b["order"]


def test_snapshot_binds_selection_profile_phases_policies():
    s = snap()
    assert s["selection"]
    assert s["profile_hash"]
    assert s["phases"] == ("transcribe", "align", "diarize", "export")
    assert s["partial_failure_policy"] in (
        "mit_belegten_daten_fortfahren", "element_blockieren"
    )
    assert s["resource_policy"]["policy_id"] == "jfw5_serial_v1"
    for item in s["items"]:
        assert item["output"]["target_path"]
        assert item["source"]["content_proof"].startswith("sha256:")
        assert item["source"]["size"] is not None
        assert item["source"]["mtime_ns"] is not None
        assert item["source"]["path_key"]


def test_incomplete_snapshot_is_rejected_fail_closed():
    s = snap()
    broken = dict(s)
    broken["items"] = [dict(s["items"][0])]
    del broken["items"][0]["source"]["content_proof"]
    assert verify_snapshot_completeness(broken)


def test_source_binding_ok_on_unchanged_and_quick_check_only_is_not_authoritative():
    s = snap()
    item = next(i for i in s["items"] if i["source"]["path"].endswith("clip.wav"))
    fs = tree_fs()
    assert verify_source_binding(item, fs)["ok"] is True
    fs.facts["C:/data/clip.wav"] = facts(content=b"same")  # nur mtime geaendert
    assert verify_source_binding(item, fs)["ok"] is True


def test_changed_source_is_invalidated_not_processed_under_old_identity():
    s = snap()
    item = next(i for i in s["items"] if i["source"]["path"].endswith("clip.wav"))
    fs = tree_fs()
    fs.facts["C:/data/clip.wav"] = facts(content=b"Xame")
    out = verify_source_binding(item, fs)
    assert out["ok"] is False
    assert out["state"] == "invalidated"


def test_missing_or_unreadable_source_blocks():
    s = snap()
    item = next(i for i in s["items"] if i["source"]["path"].endswith("clip.wav"))
    fs = tree_fs()
    del fs.facts["C:/data/clip.wav"]
    assert verify_source_binding(item, fs)["state"] == "blocked"
    fs2 = tree_fs()
    fs2.facts["C:/data/clip.wav"] = None
    assert verify_source_binding(item, fs2)["state"] == "blocked"


def test_consumed_bytes_must_match_snapshot_proof_before_commit():
    s = snap()
    item = next(i for i in s["items"] if i["source"]["path"].endswith("clip.wav"))
    good = content_proof(iter([b"same"]))
    bad = content_proof(iter([b"SAME"]))
    assert verify_consumed_bytes(item, good)["ok"] is True
    out = verify_consumed_bytes(item, bad)
    assert out["ok"] is False
    assert out["state"] == "invalidated"


def test_revision_change_is_recorded_not_silent_rewrite():
    s1 = snap()
    s2 = snap(revision_no=2, parent_snapshot_hash=snapshot_hash(s1))
    assert s2["revision_no"] == 2
    assert s2["parent_snapshot_hash"] == snapshot_hash(s1)
    assert snapshot_hash(s1) != snapshot_hash(s2)
