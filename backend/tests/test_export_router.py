"""JFW-4: Export-Router — Vertragstests (TDD).

Endpunktfunktionen werden direkt gegen eine Datei-DB aufgerufen (Muster JFW-11:
``starlette.testclient`` bleibt wegen httpx-Altlast draussen).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_router.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, ExportJob
from backend.export.provenance import export_key
from backend.routes import export as export_routes
from backend.tests.jfw4_sources import req, sources_full


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_export_api_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _body(target_dir, **over):
    base = dict(
        job_id="job-1",
        audio_asset_id="asset-1",
        audio_hash="a" * 64,
        audio_duration_ms=10_000,
        timebase="audio_ms_v1",
        transcript_run_id="run-1",
        transcript_revision_id="rev-1",
        transcript_revision_hash="b" * 64,
        text="Guten Tag, das ist ein Test.",
        jfw2_result_hash="1" * 64,
        jfw2_status="aligned",
        jfw3_result_hash="2" * 64,
        jfw3_status="diarized",
        jfw11_commit_hash=None,
        jfw11_status=None,
        jfw11_expected=False,
        sources=sources_full(),
        formats=["json", "srt", "vtt"],
        export_profile="lesbare_untertitel_v1",
        name_policy="neutral",
        partial_mode=None,
        partial_confirmed=False,
        target_dir=str(target_dir),
        replace_existing=False,
    )
    base.update(over)
    return export_routes.ExportPrepareRequest(**base)


def test_router_declares_lifecycle_endpoints():
    declared = {(r.path, tuple(sorted(r.methods))) for r in export_routes.router.routes}
    expected = {
        ("/export/prepare", ("POST",)),
        ("/export/run", ("POST",)),
        ("/export/{export_key}/cancel", ("POST",)),
        ("/export/{export_key}", ("GET",)),
    }
    assert expected <= declared


def test_prepare_returns_readiness_and_names():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.prepare_export(_body(target), db)
    assert out["outcome"] == "created"
    assert out["status"] == "ready"
    assert any(n.endswith(".json") for n in out["expected_files"])
    assert any(n.endswith(".srt") for n in out["expected_files"])
    assert out["readiness"]["state"] == "ready"


def test_prepare_blocked_persists_no_order():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.prepare_export(
        _body(target, jfw2_result_hash="9" * 64), db)
    assert out["outcome"] == "blocked"
    assert db.query(ExportJob).count() == 0


def test_prepare_detects_target_conflicts():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.prepare_export(_body(target), db)
    (target / out["expected_files"][0]).write_bytes(b"vorhanden")
    out2 = export_routes.prepare_export(_body(target), db)
    assert out["expected_files"][0] in out2["existing_targets"]


def test_run_writes_set_and_commits():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.run_export(_body(target), db)
    assert out["outcome"] == "exported"
    for name in out["expected_files"]:
        assert (target / name).exists()
    summary = export_routes.get_export(export_key(req(formats=("json", "srt", "vtt"))), db)
    assert summary["status"] == "exported"
    assert summary["result_hash"] == out["result_hash"]
    for entry in summary["manifest"]:
        data = (target / entry["file_name"]).read_bytes()
        import hashlib
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()


def test_run_conflict_without_replace_fails_closed():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.prepare_export(_body(target), db)
    (target / out["expected_files"][0]).write_bytes(b"VORHANDEN")
    run = export_routes.run_export(_body(target), db)
    assert run["outcome"] == "failed"
    assert run["reason_code"] == "target_conflict"
    assert (target / out["expected_files"][0]).read_bytes() == b"VORHANDEN"
    summary = export_routes.get_export(export_key(req(formats=("json", "srt", "vtt"))), db)
    assert summary["status"] == "failed"


def test_run_with_replace_replaces_whole_set():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    out = export_routes.prepare_export(_body(target), db)
    names = out["expected_files"]
    for n in names:
        (target / n).write_bytes(b"ALT")
    run = export_routes.run_export(_body(target, replace_existing=True), db)
    assert run["outcome"] == "exported"
    for n in names:
        assert (target / n).read_bytes() != b"ALT"


def test_run_is_idempotent_on_rerun():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    run1 = export_routes.run_export(_body(target), db)
    run2 = export_routes.run_export(_body(target), db)
    assert run1["outcome"] == "exported"
    assert run2["outcome"] == "already_exported"
    assert run2["result_hash"] == run1["result_hash"]


def test_cancel_endpoint():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    export_routes.prepare_export(_body(target), db)
    out = export_routes.cancel_export(export_key(req(formats=("json", "srt", "vtt"))), db)
    assert out["outcome"] in ("canceled", "not_active")
