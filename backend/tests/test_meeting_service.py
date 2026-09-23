"""JFW-11: Atomare Persistenz des Meeting-Ergebnisses — Vertragstests (TDD).

Muster JFW-2/JFW-3: Idempotenz ueber ``payload_hash``, fail-closed Provenienz-
konflikt, atomarer Commit vs. Cancel-Race, Crash-Recovery, Revisions-Invalidierung.
Geteilte Datei-DB (kein ``sqlite://``) — siehe Pitfall pytest-Concurrency.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_service.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base
from backend.meeting.provenance import MeetingRequest
from backend.services.meeting_contract import (
    begin_attempt,
    cancel_meeting,
    commit_result,
    fail_attempt,
    invalidate_other_revisions,
    recover_interrupted,
    submit_meeting,
)


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_meeting_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _request(manifest_hash="c" * 64):
    return MeetingRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=("a" * 64, "b" * 64),
        manifest_hash=manifest_hash,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )


def _built():
    return {
        "status": "secured_dual",
        "stop_reason": "user_stop",
        "tracks": [{"track_id": "track-mic"}, {"track_id": "track-remote"}],
        "gaps": [],
        "sync": {"version": "qpc_offset_drift_v1", "quality": "sync_ok"},
        "sound_cue_marks": [],
        "recovery_status": {"track-mic": "vollstaendig", "track-remote": "vollstaendig"},
        "dedupe": [],
        "name_mappings": [],
        "result_hash": "f" * 64,
        "reason_code": None,
    }


def test_submit_created_existing_conflict():
    s = _session()
    req = _request()
    assert submit_meeting(s, req)["outcome"] == "created"
    assert submit_meeting(s, req)["outcome"] == "existing"
    # Gleiche Identitaet (dasselbe Manifest), abweichende Track-Hashes:
    # fail-closed abgelehnt (Spec: „abweichender Hashkonflikt").
    other = MeetingRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=("a" * 64, "e" * 64),
        manifest_hash="c" * 64,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )
    out = submit_meeting(s, other)
    assert out["outcome"] == "conflict"
    assert out["result_hash"] is None


def test_commit_is_idempotent_and_atomic():
    s = _session()
    req = _request()
    submit_meeting(s, req)
    ih = req.identity_hash()
    assert begin_attempt(s, ih, "epoch-1") is not None
    assert commit_result(s, ih, _built()) == "committed"
    assert begin_attempt(s, ih, "epoch-1") is None  # kein Doppellauf
    assert commit_result(s, ih, _built()) == "already_terminal"


def test_commit_vs_cancel_race_terminal_wins():
    s = _session()
    req = _request()
    submit_meeting(s, req)
    ih = req.identity_hash()
    begin_attempt(s, ih, "epoch-1")
    assert cancel_meeting(s, ih) == "canceled"
    assert commit_result(s, ih, _built()) == "canceled"


def test_cancel_after_commit_is_too_late():
    s = _session()
    req = _request()
    submit_meeting(s, req)
    ih = req.identity_hash()
    begin_attempt(s, ih, "epoch-1")
    commit_result(s, ih, _built())
    assert cancel_meeting(s, ih) == "too_late"


def test_failed_without_result_hash_allows_retry():
    s = _session()
    req = _request()
    submit_meeting(s, req)
    ih = req.identity_hash()
    begin_attempt(s, ih, "epoch-1")
    assert fail_attempt(s, ih, "capture_failed") is True
    assert begin_attempt(s, ih, "epoch-2") is not None  # identischer Retry
    assert commit_result(s, ih, _built()) == "committed"


def test_recover_interrupted_resets_foreign_epoch_only():
    s = _session()
    req = _request()
    submit_meeting(s, req)
    ih = req.identity_hash()
    begin_attempt(s, ih, "epoch-old")
    assert recover_interrupted(s, "epoch-new") == 1
    assert begin_attempt(s, ih, "epoch-new") is not None


def test_invalidate_other_revisions_keeps_history():
    s = _session()
    req_old = _request(manifest_hash="d" * 64)
    submit_meeting(s, req_old)
    begin_attempt(s, req_old.identity_hash(), "epoch-1")
    commit_result(s, req_old.identity_hash(), _built())

    req_new = _request(manifest_hash="e" * 64)
    submit_meeting(s, req_new)
    assert invalidate_other_revisions(s, "job-1", req_new.identity_hash()) == 1
    out = submit_meeting(s, req_old)
    assert out["status"] == "invalidated"
    assert out["result_hash"] == "f" * 64  # alte Revision bleibt nachvollziehbar
