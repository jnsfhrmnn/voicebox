"""JFW-4: Persistenz- und Set-Commit-Vertrag — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base
from backend.export.provenance import expected_file_names, export_key
from backend.services.export_contract import (
    begin_export,
    cancel_export,
    commit_set,
    fail_attempt,
    invalidate_other_revisions,
    recover_interrupted,
    submit_export,
)
from backend.tests.jfw4_sources import req


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


READY = {"state": "ready", "partial_mode": None, "warnings": [],
         "quality_dimensions": {"timing": "vollstaendig", "speakers": "vollstaendig",
                                "sources": "nicht_betroffen", "dedupe": "nicht_betroffen"},
         "reason_code": None}


def _submit(s, request=None):
    request = request or req(target_dir="C:/target")
    return submit_export(s, request, expected_file_names(request), READY)


def test_submit_creates_order_ready(session):
    out = _submit(session)
    assert out["outcome"] == "created"
    assert out["status"] == "ready"
    assert out["export_key"] == export_key(req())


def test_submit_idempotent_existing(session):
    _submit(session)
    out = _submit(session)
    assert out["outcome"] == "existing"


def test_submit_conflict_on_different_payload(session):
    r1 = req(target_dir="C:/target")
    submit_export(session, r1, expected_file_names(r1), READY)
    r2 = req(target_dir="D:/other")  # gleicher export_key, anderes Ziel
    assert export_key(r1) == export_key(r2)
    out = submit_export(session, r2, expected_file_names(r2), READY)
    assert out["outcome"] == "conflict"


def test_begin_export_exactly_once(session):
    _submit(session)
    key = export_key(req())
    assert begin_export(session, key, "epoch-1") is not None
    assert begin_export(session, key, "epoch-1") is None


def test_commit_set_exports_and_stores_manifest(session):
    _submit(session)
    key = export_key(req())
    begin_export(session, key, "epoch-1")
    manifest = [{"role": "srt", "file_name": "f.srt", "bytes": 3, "sha256": "a" * 64}]
    outcome = commit_set(session, key, manifest=manifest, result_hash="b" * 64)
    assert outcome == "committed"
    out = _submit(session)
    assert out["outcome"] == "existing"
    assert out["result_hash"] == "b" * 64


def test_cancel_wins_no_set_becomes_authoritative(session):
    _submit(session)
    key = export_key(req())
    begin_export(session, key, "epoch-1")
    assert cancel_export(session, key) == "canceled"
    assert commit_set(session, key, manifest=[], result_hash="b" * 64) == "canceled"


def test_commit_wins_cancel_too_late(session):
    _submit(session)
    key = export_key(req())
    begin_export(session, key, "epoch-1")
    commit_set(session, key, manifest=[], result_hash="b" * 64)
    assert cancel_export(session, key) == "too_late"


def test_recover_interrupted_marks_failed(session):
    _submit(session)
    key = export_key(req())
    begin_export(session, key, "epoch-alt")
    assert recover_interrupted(session, "epoch-neu") == 1
    # kein autoritativer Set, aber wieder startfaehig
    assert begin_export(session, key, "epoch-neu") is not None


def test_invalidate_on_revision_change(session):
    r = req(target_dir="C:/target")
    submit_export(session, r, expected_file_names(r), READY)
    key = export_key(r)
    begin_export(session, key, "epoch-1")
    commit_set(session, key, manifest=[], result_hash="b" * 64)
    n = invalidate_other_revisions(session, job_id="job-1",
                                   transcript_revision_id="rev-2",
                                   jfw2_result_hash="9" * 64,
                                   jfw3_result_hash="2" * 64,
                                   jfw11_commit_hash=None)
    assert n == 1
    out = _submit(session)
    assert out["status"] == "invalidated"


def test_failed_can_retry_but_exported_cannot(session):
    _submit(session)
    key = export_key(req())
    begin_export(session, key, "epoch-1")
    assert fail_attempt(session, key, "schreibfehler") is True
    assert begin_export(session, key, "epoch-1") is not None  # Retry moeglich
    commit_set(session, key, manifest=[], result_hash="b" * 64)
    assert begin_export(session, key, "epoch-1") is None  # nie doppelt exportieren
