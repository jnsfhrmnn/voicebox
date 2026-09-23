"""JFW-6: Recording-Router — Vertragstests (TDD, RED zuerst).

Bindet ``services/recording_contract.py`` an die lokale API (Muster JFW-2/JFW-3/
JFW-11). Endpunktfunktionen werden direkt gegen eine geteilte Datei-DB
aufgerufen — ``starlette.testclient`` ist hier bewusst nicht im Spiel
(httpx-Altlast, siehe QA-Evidence JFW-11 §4).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_router.py
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
from backend.recording.manifest import build_run_manifest, canonical_hash
from backend.recording.provenance import RecordingRequest
from backend.recording.run_identity import new_run_id
from backend.routes import recording as recording_routes


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_rec_api_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _submit_body(run_id=None):
    return recording_routes.RecordingSubmitRequest(
        run_id=run_id or new_run_id(),
        device_stable_id_hash="b" * 64,
        format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
    )


def _manifest(run_id: str) -> dict:
    return build_run_manifest(
        run_id=run_id,
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        started_at_100ns=1_000_000,
        ended_at_100ns=9_000_000,
        stop_reason="toggle",
        device_snapshot={"stable_id_hash": "b" * 64},
        frames={"accepted_frames": 10, "final_frames": 10, "gap_frames": 0},
        gaps=[],
        sound_cue_marks=[{"kind": "start_ton", "start_100ns": 1_000_010, "end_100ns": 1_000_500}],
        recovery_procedure={"ablauf": "max(raw, journal)"},
    )


def _commit_body(run_id: str):
    manifest = _manifest(run_id)
    return recording_routes.RecordingCommitRequest(
        stop_reason="toggle",
        audio_hash="a" * 64,
        manifest=manifest,
        frames={"accepted_frames": 10, "final_frames": 10, "gap_frames": 0},
        gaps=[],
        sound_cue_marks=manifest["sound_cue_marks"],
        recovery_status={"status": "vollstaendig", "loss_window_ms": 0.0},
    )


def _identity_hash(body) -> str:
    return RecordingRequest(
        run_id=body.run_id,
        device_stable_id_hash=body.device_stable_id_hash,
        format=dict(body.format),
    ).identity_hash()


def test_router_declares_lifecycle_endpoints():
    declared = {(r.path, tuple(sorted(r.methods))) for r in recording_routes.router.routes}
    expected = {
        ("/recording/submit", ("POST",)),
        ("/recording/{identity_hash}/begin", ("POST",)),
        ("/recording/{identity_hash}/stop", ("POST",)),
        ("/recording/{identity_hash}/commit", ("POST",)),
        ("/recording/{identity_hash}/cancel", ("POST",)),
        ("/recording/{identity_hash}/handoff", ("POST",)),
        ("/recording/{identity_hash}", ("GET",)),
    }
    assert expected <= declared


def test_submit_is_idempotent_and_conflict_fail_closed():
    s = _session()
    body = _submit_body()
    assert recording_routes.submit_recording(body, s)["outcome"] == "created"
    assert recording_routes.submit_recording(body, s)["outcome"] == "existing"
    other = _submit_body(run_id=body.run_id)
    other.device_stable_id_hash = "c" * 64
    with pytest.raises(HTTPException) as exc:
        recording_routes.submit_recording(other, s)
    assert exc.value.status_code == 409
    assert exc.value.detail == "conflict"


def test_full_lifecycle_submit_begin_stop_commit_get():
    s = _session()
    body = _submit_body()
    recording_routes.submit_recording(body, s)
    ih = _identity_hash(body)
    begun = recording_routes.begin_recording(
        ih, recording_routes.RecordingBeginRequest(t_100ns=2_000_000), s
    )
    assert begun["status"] == "recording"
    stopped = recording_routes.stop_recording(
        ih, recording_routes.RecordingStopRequest(cause="toggle", t_100ns=5_000_000), s
    )
    assert stopped["outcome"] == "accepted"
    committed = recording_routes.commit_recording(ih, _commit_body(body.run_id), s)
    assert committed["outcome"] == "committed"
    summary = recording_routes.get_recording(ih, s)
    assert summary["status"] == "secured"
    assert summary["identity_hash"] == ih
    # Kein Doppellauf nach dem Commit (fail-closed).
    with pytest.raises(HTTPException) as exc:
        recording_routes.begin_recording(
            ih, recording_routes.RecordingBeginRequest(t_100ns=6_000_000), s
        )
    assert exc.value.status_code == 409


def test_begin_requires_all_start_confirmations():
    s = _session()
    body = _submit_body()
    recording_routes.submit_recording(body, s)
    ih = _identity_hash(body)
    with pytest.raises(HTTPException) as exc:
        recording_routes.begin_recording(
            ih,
            recording_routes.RecordingBeginRequest(
                stream_open=False, run_persisted=True, first_block_accepted=True, t_100ns=2_000_000
            ),
            s,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail == "start_unvollstaendig"


def test_second_stop_is_visible_no_second_completion():
    s = _session()
    body = _submit_body()
    recording_routes.submit_recording(body, s)
    ih = _identity_hash(body)
    recording_routes.begin_recording(
        ih, recording_routes.RecordingBeginRequest(t_100ns=2_000_000), s
    )
    stop = recording_routes.RecordingStopRequest(cause="toggle", t_100ns=5_000_000)
    assert recording_routes.stop_recording(ih, stop, s)["outcome"] == "accepted"
    repeat = recording_routes.RecordingStopRequest(cause="toggle", t_100ns=5_100_000)
    assert recording_routes.stop_recording(ih, repeat, s)["outcome"] == "already_stopped"


def test_cancel_after_commit_reports_too_late():
    s = _session()
    body = _submit_body()
    recording_routes.submit_recording(body, s)
    ih = _identity_hash(body)
    recording_routes.begin_recording(
        ih, recording_routes.RecordingBeginRequest(t_100ns=2_000_000), s
    )
    recording_routes.stop_recording(
        ih, recording_routes.RecordingStopRequest(cause="toggle", t_100ns=5_000_000), s
    )
    recording_routes.commit_recording(ih, _commit_body(body.run_id), s)
    out = recording_routes.cancel_recording(
        ih,
        recording_routes.RecordingCancelRequest(confirmed=True, deletion_contract_hash="d" * 64),
        s,
    )
    assert out["outcome"] == "too_late"


def test_handoff_is_idempotent():
    s = _session()
    body = _submit_body()
    recording_routes.submit_recording(body, s)
    ih = _identity_hash(body)
    recording_routes.begin_recording(
        ih, recording_routes.RecordingBeginRequest(t_100ns=2_000_000), s
    )
    recording_routes.stop_recording(
        ih, recording_routes.RecordingStopRequest(cause="toggle", t_100ns=5_000_000), s
    )
    recording_routes.commit_recording(ih, _commit_body(body.run_id), s)
    mh = canonical_hash(_manifest(body.run_id))
    handoff = recording_routes.RecordingHandoffRequest(audio_hash="a" * 64, manifest_hash=mh)
    assert recording_routes.deliver_run_handoff(ih, handoff, s)["outcome"] == "delivered"
    assert recording_routes.deliver_run_handoff(ih, handoff, s)["outcome"] == "existing"


def test_get_unknown_identity_404():
    s = _session()
    with pytest.raises(HTTPException) as exc:
        recording_routes.get_recording("0" * 64, s)
    assert exc.value.status_code == 404
    assert exc.value.detail == "unknown_identity"
