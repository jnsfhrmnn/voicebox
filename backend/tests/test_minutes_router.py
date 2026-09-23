"""JFW-13: Protokoll-Router — Vertragstests (TDD).

Endpunktfunktionen werden direkt gegen eine Datei-DB aufgerufen (Muster JFW-4:
``starlette.testclient`` bleibt wegen httpx-Altlast draussen).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_router.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base
from backend.routes import minutes as minutes_routes
from backend.tests.jfw13_sources import (
    candidates_names,
    names_doc,
    req13_kwargs,
    summary_proposals,
    tasks_proposals,
)


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_minutes_api_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _body(**over):
    doc = names_doc()
    base = req13_kwargs(doc)
    base.update({"document": doc, "candidates": candidates_names()})
    base.update(over)
    return minutes_routes.MinutesPrepareRequest(**base)


def _run_body(**over):
    doc = names_doc()
    base = req13_kwargs(doc)
    base.update({
        "document": doc,
        "candidates": candidates_names(),
        "proposals": {"tasks": tasks_proposals(), "summary": summary_proposals(),
                      "datum": None},
    })
    base.update(over)
    return minutes_routes.MinutesRunRequest(**base)


def test_prepare_creates_register_proposal():
    db = _session()
    out = minutes_routes.prepare_minutes(_body(), db)
    assert out["outcome"] in ("ready", "ready_with_warnings")
    assert out["register"]["status"] == "vorgeschlagen"
    labels = [e["pseudonym"] for e in out["register"]["entries"]]
    assert "Person 1" in labels
    assert "Organisation A" in labels


def test_prepare_blocked_without_document():
    db = _session()
    out = minutes_routes.prepare_minutes(_body(document=None), db)
    assert out["outcome"] == "blocked"
    assert out["reason_code"] == "jfw4_snapshot_fehlt"


def test_run_requires_confirmed_register():
    db = _session()
    minutes_routes.prepare_minutes(_body(), db)
    out = minutes_routes.run_minutes(_run_body(), db)
    assert out["outcome"] == "blocked"
    assert out["reason_code"] == "register_unbestaetigt"


def test_run_generates_result():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    minutes_routes.confirm_register_view(prepared["register_id"], {"actions": []}, db)
    out = minutes_routes.run_minutes(_run_body(), db)
    assert out["outcome"] == "generated"
    assert out["result_hash"]
    got = minutes_routes.get_minutes(out["minutes_key"], db)
    assert got["status"] == "generated"
    assert got["result_hash"] == out["result_hash"]


def test_run_is_idempotent_for_same_input_and_register():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    minutes_routes.confirm_register_view(prepared["register_id"], {"actions": []}, db)
    first = minutes_routes.run_minutes(_run_body(), db)
    second = minutes_routes.run_minutes(_run_body(), db)
    assert second["outcome"] == "existing"
    assert second["result_hash"] == first["result_hash"]


def test_missing_artifact_blocks_only_minutes_and_allows_cancel():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    minutes_routes.confirm_register_view(prepared["register_id"], {"actions": []}, db)
    out = minutes_routes.run_minutes(_run_body(proposals=None), db)
    assert out["outcome"] == "waiting_for_local_artifact"
    assert minutes_routes.cancel_minutes(out["minutes_key"], db)["outcome"] == "canceled"


def test_register_delete_then_export_check_is_rejected():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    confirmed = minutes_routes.confirm_register_view(
        prepared["register_id"], {"actions": []}, db)
    bound_revision = confirmed["register"]["revision_id"]
    minutes_routes.delete_register_view(prepared["register_id"], db)

    out = minutes_routes.export_check(
        prepared["register_id"], {"register_revision": bound_revision}, db)
    assert out["outcome"] == "abgelehnt"
    assert out["reason_code"] == "register_geloescht"
    # keine Zuordnung im offenen Exportdialog
    assert "entries" not in out
    assert "zuordnung" not in out


def test_export_check_ok_before_deletion():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    confirmed = minutes_routes.confirm_register_view(
        prepared["register_id"], {"actions": []}, db)
    out = minutes_routes.export_check(
        prepared["register_id"],
        {"register_revision": confirmed["register"]["revision_id"]}, db)
    assert out["outcome"] == "ok"


def test_nondeterministic_rerun_creates_separate_revision():
    db = _session()
    prepared = minutes_routes.prepare_minutes(_body(), db)
    minutes_routes.confirm_register_view(prepared["register_id"], {"actions": []}, db)
    first = minutes_routes.run_minutes(_run_body(), db)
    changed = {"tasks": tasks_proposals()[:1], "summary": summary_proposals()[:1],
               "datum": None}
    rerun = minutes_routes.run_minutes(_run_body(proposals=changed, rerun=True), db)
    assert rerun["outcome"] == "nondeterminism_revision"
    assert rerun["result_hash"] == first["result_hash"]  # autoritativ bleibt
    assert rerun["nondeterminism_revision"]["reason_code"] == "modell_nicht_reproduzierbar"
