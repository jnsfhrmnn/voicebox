"""JFW-5: Persistenz- und Exactly-once-Vertrag (batches/batch_items/batch_attempts) — TDD.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.batch.discovery import discover
from backend.batch.identity import new_batch_id
from backend.batch.snapshot import build_snapshot, snapshot_hash
from backend.database.models import Base
from backend.services import batch_contract as store
from backend.tests.test_batch_discovery import tree_fs
from backend.tests.test_batch_profile import prof


def make_snapshot(**over):
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


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def submit(s, snap=None):
    snap = snap or make_snapshot()
    return store.submit_batch(s, snap, app_epoch="epoch-1")


def test_submit_creates_batch_and_items(session):
    snap = make_snapshot()
    out = submit(session, snap)
    assert out["outcome"] == "created"
    assert out["status"] == "ready"
    assert out["snapshot_hash"] == snapshot_hash(snap)
    assert out["item_count"] == len(snap["items"])
    detail = store.get_batch(session, out["identity_hash"])
    assert detail["status"] == "ready"
    assert all(i["status"] == "waiting" for i in detail["items"])


def test_submit_is_idempotent_and_conflict_fails_closed(session):
    snap = make_snapshot()
    first = submit(session, snap)
    again = submit(session, snap)
    assert again["outcome"] == "existing"
    # fail-closed: abweichender Payload bei derselben Identitaet => conflict
    row = store._batch_row(session, first["identity_hash"])
    row.payload_hash = "fremd"
    session.commit()
    conflict = submit(session, snap)
    assert conflict["outcome"] == "conflict"


def test_begin_attempt_allows_exactly_one_active(session):
    out = submit(session)
    key = out["identity_hash"]
    item_id = out["item_ids"][0]
    a1 = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    assert a1["outcome"] == "started"
    assert a1["attempt_id"].startswith("jfw5-attempt-")
    a2 = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    assert a2["outcome"] == "not_startable"


def test_finalize_is_exactly_once_and_keeps_result_refs(session):
    out = submit(session)
    key, item_id = out["identity_hash"], out["item_ids"][0]
    att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    store.record_phase_commit(session, att["attempt_id"], "transcribe", {"result_hash": "h1"})
    res = store.finalize_item_attempt(
        session, att["attempt_id"], "failed", reason_code="modell_fehlt",
        phase_states={"transcribe": "failed"},
    )
    assert res == "finalized"
    again = store.finalize_item_attempt(session, att["attempt_id"], "succeeded")
    assert again == "already_terminal"
    detail = store.get_batch(session, key)
    item = next(i for i in detail["items"] if i["item_id"] == item_id)
    assert item["status"] == "failed"
    # gesicherte Commits bleiben gesichert
    assert item["result_refs"]["transcribe"] == {"result_hash": "h1"}


def test_cancel_commit_race_first_terminal_wins(session):
    out = submit(session)
    key, item_id = out["identity_hash"], out["item_ids"][0]
    att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    assert store.finalize_item_attempt(session, att["attempt_id"], "succeeded") == "finalized"
    # Abbruch trifft spaeter ein: der Commit bleibt autoritativ (zu spaet).
    assert store.cancel_attempt(session, att["attempt_id"]) == "too_late"
    detail = store.get_batch(session, key)
    assert next(i for i in detail["items"] if i["item_id"] == item_id)["status"] == "succeeded"


def test_cancel_waiting_item_never_starts_and_isolates_others(session):
    out = submit(session)
    key = out["identity_hash"]
    i1, i2 = out["item_ids"][0], out["item_ids"][1]
    assert store.cancel_item(session, key, i1) == "canceled"
    detail = store.get_batch(session, key)
    states = {i["item_id"]: i["status"] for i in detail["items"]}
    assert states[i1] == "canceled"
    assert states[i2] == "waiting"


def test_recover_interrupted_marks_attempts_items_and_pauses_batch(session):
    out = submit(session)
    key, item_id = out["identity_hash"], out["item_ids"][0]
    store.start_batch(session, key)
    att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-alt")
    n = store.recover_interrupted(session, current_epoch="epoch-1")
    assert n == 1
    detail = store.get_batch(session, key)
    assert detail["status"] == "paused"
    assert detail["reason_code"] == "unterbrochen"
    item = next(i for i in detail["items"] if i["item_id"] == item_id)
    assert item["status"] == "interrupted"
    attempt = next(a for a in detail["attempts"] if a["attempt_id"] == att["attempt_id"])
    assert attempt["status"] == "interrupted"


def test_resume_needs_explicit_confirmation(session):
    out = submit(session)
    key = out["identity_hash"]
    store.start_batch(session, key)
    store.pause_batch(session, key)
    assert store.resume_batch(session, key, confirmed_original=False) == "nicht_bestaetigt"
    assert store.resume_batch(session, key, confirmed_original=True) == "running"
    detail = store.get_batch(session, key)
    assert detail["status"] == "running"


def test_illegal_state_transitions_are_refused(session):
    out = submit(session)
    key = out["identity_hash"]
    assert store.cancel_batch(session, key) == "canceled"
    assert store.start_batch(session, key) == "not_startable"


def test_retry_creates_new_attempt_and_keeps_commits(session):
    out = submit(session)
    key, item_id = out["identity_hash"], out["item_ids"][0]
    att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    store.record_phase_commit(session, att["attempt_id"], "transcribe", {"result_hash": "h1"})
    store.finalize_item_attempt(session, att["attempt_id"], "failed",
                                reason_code="x", phase_states={"transcribe": "succeeded", "align": "failed"})
    plan = store.retry_item(session, key, item_id, reused_phases=("transcribe",),
                            rerun_phases=("align",), reason_code=None)
    assert plan["outcome"] == "retry_scheduled"
    detail = store.get_batch(session, key)
    item = next(i for i in detail["items"] if i["item_id"] == item_id)
    assert item["status"] == "waiting"
    assert item["result_refs"]["transcribe"] == {"result_hash": "h1"}
    assert len(detail["attempts"]) == 2
    assert detail["attempts"][-1]["reused_commits"] == {"transcribe": {"result_hash": "h1"}}


def test_aggregate_view_derives_batch_status_from_items(session):
    out = submit(session)
    key = out["identity_hash"]
    store.start_batch(session, key)
    for item_id in out["item_ids"]:
        att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
        store.finalize_item_attempt(session, att["attempt_id"], "succeeded")
    store.finalize_batch(session, key)
    detail = store.get_batch(session, key)
    assert detail["status"] == "completed"
    assert detail["aggregates"]["counts"] == {"succeeded": 3}


def test_pause_waits_for_active_item_checkpoint_before_reporting_paused(session):
    out = submit(session)
    key = out["identity_hash"]
    store.start_batch(session, key)
    item_id = out["item_ids"][0]
    att = store.begin_item_attempt(session, key, item_id, app_epoch="epoch-1")
    # aktives Element vorhanden -> Pausier-Anforderung nur sichtbar (pausing)
    assert store.pause_batch(session, key) == "pausing"
    assert store.get_batch(session, key)["status"] == "pausing"
    assert store.finalize_item_attempt(session, att["attempt_id"], "succeeded") == "finalized"
    # erster sicherer Checkpunkt erreicht -> jetzt erst meldet der Batch paused
    assert store.get_batch(session, key)["status"] == "paused"
