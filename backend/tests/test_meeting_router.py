"""JFW-11: Meeting-Router — Vertragstests (TDD, RED zuerst beobachtet).

Bindet ``services/meeting_contract.py`` an die lokale API (Muster JFW-2/JFW-3:
``routes/alignment.py``/``routes/diarization.py``). Endpunktfunktionen werden
direkt gegen eine geteilte Datei-DB aufgerufen — ``starlette.testclient`` ist
hier bewusst nicht im Spiel (httpx-Altlast, siehe QA-Evidence JFW-11 §4).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_router.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base
from backend.meeting.provenance import MeetingRequest
from backend.routes import meeting as meeting_routes


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_meeting_api_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()

def _body(manifest_hash="c" * 64):
    return meeting_routes.MeetingSubmitRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=["a" * 64, "b" * 64],
        manifest_hash=manifest_hash,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )

def _built():
    return meeting_routes.MeetingCommitRequest(
        status="secured_dual",
        stop_reason="user_stop",
        tracks=[{"track_id": "track-mic"}, {"track_id": "track-remote"}],
        gaps=[],
        sync={"version": "qpc_offset_drift_v1", "quality": "sync_ok"},
        sound_cue_marks=[],
        recovery_status={"track-mic": "vollstaendig", "track-remote": "vollstaendig"},
        dedupe=[],
        name_mappings=[],
        result_hash="f" * 64,
    )

def test_router_declares_lifecycle_endpoints():
    """Der HTTP-Vertrag deckt den Dual-Source-Lebenszyklus ab (submit/begin/
    commit/cancel/status) — keine verbotenen Flaechen, keine Capture-Helfer-
    Abhaengigkeit im Router (I/O bleibt im Folge-Block)."""
    declared = {(r.path, tuple(sorted(r.methods))) for r in meeting_routes.router.routes}
    expected = {
        ("/meeting/submit", ("POST",)),
        ("/meeting/{identity_hash}/begin", ("POST",)),
        ("/meeting/{identity_hash}/commit", ("POST",)),
        ("/meeting/{identity_hash}/cancel", ("POST",)),
        ("/meeting/{identity_hash}", ("GET",)),
    }
    assert expected <= declared

def test_submit_is_idempotent_and_conflict_fail_closed():
    s = _session()
    out = meeting_routes.submit_meeting(_body(), s)
    assert out["outcome"] == "created"
    assert meeting_routes.submit_meeting(_body(), s)["outcome"] == "existing"
    other = meeting_routes.MeetingSubmitRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=["a" * 64, "e" * 64],
        manifest_hash="c" * 64,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )
    with pytest.raises(HTTPException) as exc:
        meeting_routes.submit_meeting(other, s)
    assert exc.value.status_code == 409
    assert exc.value.detail == "conflict"

def test_full_lifecycle_submit_begin_commit_get():
    s = _session()
    body = _body()
    meeting_routes.submit_meeting(body, s)
    ih = MeetingRequest(
        job_id=body.job_id,
        meeting_run_id=body.meeting_run_id,
        jfw6_run_reference=body.jfw6_run_reference,
        track_hashes=tuple(body.track_hashes),
        manifest_hash=body.manifest_hash,
        jfw2_result_hash=body.jfw2_result_hash,
        jfw3_result_hash=body.jfw3_result_hash,
    ).identity_hash()
    begun = meeting_routes.begin_meeting(ih, meeting_routes.MeetingBeginRequest(), s)
    assert begun["attempt_id"]
    committed = meeting_routes.commit_meeting(ih, _built(), s)
    assert committed["outcome"] == "committed"
    summary = meeting_routes.get_meeting(ih, s)
    assert summary["status"] == "secured_dual"
    assert summary["result_hash"] == "f" * 64
    assert summary["identity_hash"] == ih
    # Kein Doppellauf nach dem Commit (fail-closed).
    with pytest.raises(HTTPException) as exc:
        meeting_routes.begin_meeting(ih, meeting_routes.MeetingBeginRequest(), s)
    assert exc.value.status_code == 409

def test_commit_after_cancel_is_visible_race_loss():
    s = _session()
    body = _body()
    meeting_routes.submit_meeting(body, s)
    ih = MeetingRequest(
        job_id=body.job_id,
        meeting_run_id=body.meeting_run_id,
        jfw6_run_reference=body.jfw6_run_reference,
        track_hashes=tuple(body.track_hashes),
        manifest_hash=body.manifest_hash,
    ).identity_hash()
    meeting_routes.begin_meeting(ih, meeting_routes.MeetingBeginRequest(), s)
    assert meeting_routes.cancel_meeting(ih, s)["outcome"] == "canceled"
    # Cancel/Commit-Race: der zuerst gespeicherte terminale Ausgang gewinnt,
    # der Verlierer sieht den Verlust sichtbar (409) statt still zu ueberschreiben.
    with pytest.raises(HTTPException) as exc:
        meeting_routes.commit_meeting(ih, _built(), s)
    assert exc.value.status_code == 409
    assert exc.value.detail == "canceled"

def test_cancel_after_commit_reports_too_late():
    s = _session()
    body = _body()
    meeting_routes.submit_meeting(body, s)
    ih = MeetingRequest(
        job_id=body.job_id,
        meeting_run_id=body.meeting_run_id,
        jfw6_run_reference=body.jfw6_run_reference,
        track_hashes=tuple(body.track_hashes),
        manifest_hash=body.manifest_hash,
    ).identity_hash()
    meeting_routes.begin_meeting(ih, meeting_routes.MeetingBeginRequest(), s)
    meeting_routes.commit_meeting(ih, _built(), s)
    assert meeting_routes.cancel_meeting(ih, s)["outcome"] == "too_late"

def test_get_unknown_identity_404():
    s = _session()
    with pytest.raises(HTTPException) as exc:
        meeting_routes.get_meeting("0" * 64, s)
    assert exc.value.status_code == 404
    assert exc.value.detail == "unknown_identity"
