"""JFW-5: Batch-Router `/batch/*` — Vertragstests (TDD).

Endpunktfunktionen werden direkt gegen eine Datei-DB aufgerufen (Muster JFW-4:
``starlette.testclient`` bleibt wegen httpx-Altlast draussen).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_router.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.batch.pipeline import PhaseOutcome
from backend.database.models import Base
from backend.routes import batch as batch_routes
from backend.tests.test_batch_discovery import tree_fs
from backend.tests.test_batch_profile import prof


def _session():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _profile(**over):
    p = prof(**over)
    p = dict(p, output_policy=dict(p["output_policy"], target_root="C:/out"))
    return p


def _patch_fs(monkey_fake):
    batch_routes.LocalDiscoveryFs = lambda: monkey_fake


def _confirm(session, **profile_over):
    _patch_fs(tree_fs())
    body = batch_routes.ConfirmRequest(
        selection=[{"kind": "folder", "path": "C:/data"}],
        profile=_profile(**profile_over),
    )
    return batch_routes.confirm_batch(body, session)


def test_router_declares_lifecycle_endpoints():
    declared = {(r.path, tuple(sorted(r.methods))) for r in batch_routes.router.routes}
    expected = {
        ("/batch/discover", ("POST",)),
        ("/batch/confirm", ("POST",)),
        ("/batch/{batch_id}/start", ("POST",)),
        ("/batch/{batch_id}/pause", ("POST",)),
        ("/batch/{batch_id}/resume", ("POST",)),
        ("/batch/{batch_id}/cancel", ("POST",)),
        ("/batch/{batch_id}/reorder", ("POST",)),
        ("/batch/{batch_id}/items/{item_id}/run", ("POST",)),
        ("/batch/{batch_id}/items/{item_id}/cancel", ("POST",)),
        ("/batch/{batch_id}/items/{item_id}/retry", ("POST",)),
        ("/batch/{batch_id}", ("GET",)),
        ("/batch/{batch_id}/items/{item_id}", ("GET",)),
    }
    assert expected <= declared


def test_discover_previews_without_writing():
    session = _session()
    _patch_fs(tree_fs())
    body = batch_routes.DiscoverRequest(selection=[{"kind": "folder", "path": "C:/data"}])
    out = batch_routes.discover_batch(body, session)
    assert out["counts"]["included"] >= 2
    assert out["excluded"]
    # keine Persistenz ohne Bestaetigung
    assert session.query(batch_routes.Batch).count() == 0


def test_confirm_freezes_snapshot_and_starts_nothing():
    session = _session()
    out = _confirm(session)
    assert out["outcome"] == "created"
    assert out["status"] == "ready"
    assert out["item_ids"]
    detail = batch_routes.get_batch(out["batch_id"], session)
    assert detail["status"] == "ready"
    assert all(i["status"] == "waiting" for i in detail["items"])
    assert detail["profile_hash"]


def test_start_pause_resume_cancel_lifecycle():
    session = _session()
    out = _confirm(session)
    bid = out["batch_id"]
    assert batch_routes.start_batch(bid, session)["status"] == "running"
    assert batch_routes.pause_batch(bid, session)["status"] == "paused"
    assert batch_routes.resume_batch(bid, batch_routes.ResumeRequest(confirmed_original=False),
                                     session)["outcome"] == "nicht_bestaetigt"
    assert batch_routes.resume_batch(bid, batch_routes.ResumeRequest(confirmed_original=True),
                                     session)["outcome"] == "running"
    assert batch_routes.cancel_batch(bid, session)["outcome"] == "canceled"
    assert batch_routes.start_batch(bid, session)["outcome"] == "not_startable"


def test_run_item_drives_pipeline_and_persists_commits():
    session = _session()
    out = _confirm(session)
    bid, item_id = out["batch_id"], out["item_ids"][0]

    def fake_executors(session_arg=None, app_epoch=None, **kw):
        return {
            p: (lambda phase, ctx: PhaseOutcome(
                status="succeeded", commit_ref={"result_hash": f"h_{phase}"},
                reason_code=None, warnings=[]))
            for p in ("transcribe", "align", "diarize", "export")
        }

    batch_routes.default_executors = fake_executors
    assert batch_routes.start_batch(bid, session)["status"] == "running"
    result = batch_routes.run_item(bid, item_id, session)
    assert result["end_state"] == "succeeded"
    detail = batch_routes.get_batch(bid, session)
    item = next(i for i in detail["items"] if i["item_id"] == item_id)
    assert item["status"] == "succeeded"
    assert item["result_refs"]["transcribe"] == {"result_hash": "h_transcribe"}
    assert detail["attempts"]


def test_item_cancel_and_retry_endpoints():
    session = _session()
    out = _confirm(session)
    bid, item_id = out["batch_id"], out["item_ids"][0]
    assert batch_routes.cancel_item(bid, item_id, session)["outcome"] == "canceled"
    retry = batch_routes.retry_item(bid, item_id, session)
    assert retry["outcome"] == "retry_scheduled"
    detail = batch_routes.get_batch(bid, session)
    item = next(i for i in detail["items"] if i["item_id"] == item_id)
    assert item["status"] == "waiting"


def test_reorder_creates_new_revision_without_touching_started_items():
    session = _session()
    out = _confirm(session)
    bid = out["batch_id"]
    order = list(reversed(out["item_ids"]))
    result = batch_routes.reorder_items(bid, batch_routes.ReorderRequest(order=order), session)
    assert result["outcome"] == "new_revision"
    assert result["revision_no"] == 2
    detail = batch_routes.get_batch(bid, session)
    assert [i["item_id"] for i in detail["items"]] == order


def test_fail_fast_holds_batch_after_first_failure_but_is_never_default():
    session = _session()
    out = _confirm(session, fail_fast=True)
    bid, item_id = out["batch_id"], out["item_ids"][0]

    def failing_executors(session_arg=None, app_epoch=None, **kw):
        def fail(phase, ctx):
            return PhaseOutcome(status="failed", commit_ref=None,
                                reason_code="provider_error:Test", warnings=[])
        return {p: fail for p in ("transcribe", "align", "diarize", "export")}

    batch_routes.default_executors = failing_executors
    assert batch_routes.start_batch(bid, session)["status"] == "running"
    result = batch_routes.run_item(bid, item_id, session)
    assert result["end_state"] == "failed"
    assert result["fail_fast_paused"] is True
    detail = batch_routes.get_batch(bid, session)
    assert detail["status"] == "paused"
