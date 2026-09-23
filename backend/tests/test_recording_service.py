"""JFW-6: Atomare Persistenz des Aufnahme-Runs — Vertragstests (TDD, RED zuerst).

Muster JFW-2/JFW-3/JFW-11: Idempotenz ueber ``payload_hash``, fail-closed
Provenienzkonflikt, Exactly-once Beginn und Stopp-Intent, atomarer terminaler
Commit vs. Verwerfen-/Fehler-Race, idempotenter JFW-7-Handoff, Crash-Recovery
(nie als vollstaendig ausgegeben). Geteilte Datei-DB (kein ``sqlite://``) —
siehe Pitfall pytest-Concurrency.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_service.py
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, RecordingRun
from backend.recording.manifest import build_run_manifest, canonical_hash
from backend.recording.provenance import RecordingRequest
from backend.recording.run_identity import new_run_id
from backend.services.recording_contract import (
    accept_stop,
    begin_recording,
    commit_result,
    deliver_handoff,
    discard_run,
    fail_attempt,
    recover_interrupted,
    submit_run,
)


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_rec_service_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _request(run_id=None, **overrides) -> RecordingRequest:
    base = dict(
        run_id=run_id or new_run_id(),
        device_stable_id_hash="b" * 64,
        format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        started_at_100ns=1_000_000,
    )
    base.update(overrides)
    return RecordingRequest(**base)


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
        gaps=[{"start_100ns": 2_000_000, "end_100ns": 2_100_000, "grund": "geraetefeher"}],
        sound_cue_marks=[{"kind": "start_ton", "start_100ns": 1_000_010, "end_100ns": 1_000_500}],
        recovery_procedure={"ablauf": "max(raw, journal)"},
    )


def _built(run_id: str, **overrides) -> dict:
    manifest = _manifest(run_id)
    built = {
        "stop_reason": "toggle",
        "audio_hash": "a" * 64,
        "manifest": manifest,
        "frames": {"accepted_frames": 10, "final_frames": 10, "gap_frames": 0},
        "gaps": [{"start_100ns": 2_000_000, "end_100ns": 2_100_000, "grund": "geraetefeher"}],
        "sound_cue_marks": manifest["sound_cue_marks"],
        "recovery_status": {"status": "vollstaendig", "loss_window_ms": 0.0},
    }
    built.update(overrides)
    return built


def test_submit_created_existing_conflict():
    s = _session()
    req = _request()
    assert submit_run(s, req)["outcome"] == "created"
    assert submit_run(s, req)["outcome"] == "existing"  # identisches Handoff = idempotent
    other = _request(run_id=req.run_id, device_stable_id_hash="c" * 64)
    assert submit_run(s, other)["outcome"] == "conflict"  # fail-closed


def test_submit_creates_exactly_one_run_per_run_id():
    s = _session()
    req = _request()
    submit_run(s, req)
    submit_run(s, req)  # Auto-Repeat/Mehrfachfeuer
    from backend.database.models import RecordingRun

    assert s.query(RecordingRun).filter_by(run_id=req.run_id).count() == 1


def test_begin_recording_exactly_once():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    assert begin_recording(s, out["identity_hash"], app_epoch="epoch-1") is not None
    assert begin_recording(s, out["identity_hash"], app_epoch="epoch-1") is None  # kein zweiter Start


def test_accept_stop_exactly_once():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    assert accept_stop(s, ih, cause="toggle", t_100ns=5_000_000) == "accepted"
    assert accept_stop(s, ih, cause="toggle", t_100ns=5_100_000) == "already_stopped"  # kein zweiter Abschluss


def test_accept_stop_rejects_non_monotonic_point():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    assert accept_stop(s, ih, cause="toggle", t_100ns=500) == "zeitpunkt_nicht_monoton"
    assert accept_stop(s, ih, cause="toggle", t_100ns=5_000_000) == "accepted"


def test_accept_stop_not_active_before_begin():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    assert accept_stop(s, out["identity_hash"], cause="toggle", t_100ns=5_000_000) == "not_active"


def test_commit_result_secures_run_and_is_idempotent():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    accept_stop(s, ih, cause="toggle", t_100ns=5_000_000)
    assert commit_result(s, ih, _built(req.run_id)) == "committed"
    assert commit_result(s, ih, _built(req.run_id)) == "already_terminal"


def test_commit_refuses_unbalanced_frames():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    built = _built(req.run_id, frames={"accepted_frames": 10, "final_frames": 8, "gap_frames": 1})
    assert commit_result(s, ih, built) == "frame_bilanz_unvollstaendig"
    from backend.database.models import RecordingRun

    row = s.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.result_hash is None  # nichts Autoritatives gespeichert


def test_commit_refuses_invalid_manifest():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    manifest = _manifest(req.run_id)
    del manifest["recovery_procedure"]
    assert commit_result(s, ih, _built(req.run_id, manifest=manifest)) == "manifest_ungueltig"


def test_discard_requires_confirmation_and_deletion_contract():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    assert discard_run(s, ih, confirmed=False, deletion_contract_hash="d" * 64) == "bestaetigung_erforderlich"
    assert discard_run(s, ih, confirmed=True, deletion_contract_hash=None) == "loeschvertrag_erforderlich"
    assert discard_run(s, ih, confirmed=True, deletion_contract_hash="d" * 64) == "canceled"
    from backend.database.models import RecordingRun

    row = s.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.status == "canceled"
    assert row.deletion_contract_hash == "d" * 64
    assert not row.transcription_authorized  # kein Transkriptions-Handoff


def test_commit_after_discard_is_visible_race_loss():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    accept_stop(s, ih, cause="bedienung", t_100ns=5_000_000)
    discard_run(s, ih, confirmed=True, deletion_contract_hash="d" * 64)
    assert commit_result(s, ih, _built(req.run_id)) == "canceled"
    assert discard_run(s, ih, confirmed=True, deletion_contract_hash="d" * 64) == "already_terminal"


def test_fail_attempt_without_authoritative_result():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    assert fail_attempt(s, ih, "geraetefeher") is True
    from backend.database.models import RecordingRun

    row = s.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.status == "failed"
    assert row.result_hash is None
    assert fail_attempt(s, ih, "geraetefeher") is False  # kein zweiter terminaler Ausgang


def test_commit_vs_discard_race_exactly_one_durable_winner():
    # Muster test_task_contract: Datei-DB, eigene Session je Thread.
    db_path = Path(tempfile.mktemp(suffix=".db"))
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    setup = factory()
    req = _request()
    out = submit_run(setup, req)
    ih = out["identity_hash"]
    begin_recording(setup, ih, app_epoch="epoch-1")
    accept_stop(setup, ih, cause="bedienung", t_100ns=5_000_000)
    setup.close()

    results: list[tuple[str, str]] = []
    barrier = threading.Barrier(2)

    def _commit():
        s = factory()
        try:
            barrier.wait()
            results.append(("commit", commit_result(s, ih, _built(req.run_id))))
        finally:
            s.close()

    def _discard():
        s = factory()
        try:
            barrier.wait()
            results.append(
                ("discard", discard_run(s, ih, confirmed=True, deletion_contract_hash="d" * 64))
            )
        finally:
            s.close()

    threads = [threading.Thread(target=_commit), threading.Thread(target=_discard)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [
        op
        for op, out in results
        if (op == "commit" and out == "committed") or (op == "discard" and out == "canceled")
    ]
    assert len(wins) == 1, f"genau ein dauerhafter Gewinner erwartet: {results}"
    check = factory()
    row = check.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.status in ("secured", "canceled")
    assert (row.status == "secured") == (row.result_hash is not None)
    check.close()
    engine.dispose()
    db_path.unlink(missing_ok=True)


def test_deliver_handoff_exactly_once_transcription_authorization():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="epoch-1")
    accept_stop(s, ih, cause="toggle", t_100ns=5_000_000)
    commit_result(s, ih, _built(req.run_id))
    manifest = _manifest(req.run_id)
    mh = canonical_hash(manifest)
    assert deliver_handoff(s, ih, audio_hash="a" * 64, manifest_hash=mh) == "delivered"
    # Dasselbe Handoff identisch erneut: derselbe Run idempotent fortgesetzt,
    # keine zweite Transkription autorisiert.
    assert deliver_handoff(s, ih, audio_hash="a" * 64, manifest_hash=mh) == "existing"
    from backend.database.models import RecordingRun

    row = s.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.handoff_count == 1
    assert row.transcription_authorized


def test_deliver_handoff_conflict_and_not_ready():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    # Vor dem Commit: kein Handoff moeglich.
    assert deliver_handoff(s, ih, audio_hash="a" * 64, manifest_hash="0" * 64) == "not_ready"
    begin_recording(s, ih, app_epoch="epoch-1")
    accept_stop(s, ih, cause="toggle", t_100ns=5_000_000)
    commit_result(s, ih, _built(req.run_id))
    manifest = _manifest(req.run_id)
    # Abweichender Payload bei gleicher Identitaet = fail-closed conflict.
    assert deliver_handoff(s, ih, audio_hash="a" * 64, manifest_hash="9" * 64) == "conflict"
    assert deliver_handoff(s, ih, audio_hash="e" * 64, manifest_hash=canonical_hash(manifest)) == "conflict"


def test_recover_interrupted_marks_incomplete_never_complete():
    s = _session()
    req = _request()
    out = submit_run(s, req)
    ih = out["identity_hash"]
    begin_recording(s, ih, app_epoch="alte-epoch")  # Absturz der Vorgaengergeneration
    assert recover_interrupted(s, "neue-epoch") == 1
    from backend.database.models import RecordingRun

    row = s.query(RecordingRun).filter_by(identity_hash=ih).one()
    assert row.status == "failed"
    assert row.reason_code == "unterbrochen"
    assert row.result_hash is None  # nie als vollstaendig ausgegeben


def test_recover_interrupted_keeps_own_epoch_and_secured_runs():
    s = _session()
    req_a, req_b = _request(), _request()
    out_a = submit_run(s, req_a)
    out_b = submit_run(s, req_b)
    begin_recording(s, out_a["identity_hash"], app_epoch="neue-epoch")
    begin_recording(s, out_b["identity_hash"], app_epoch="neue-epoch")
    accept_stop(s, out_b["identity_hash"], cause="toggle", t_100ns=5_000_000)
    commit_result(s, out_b["identity_hash"], _built(req_b.run_id))
    assert recover_interrupted(s, "neue-epoch") == 0
    from backend.database.models import RecordingRun

    secured = s.query(RecordingRun).filter_by(identity_hash=out_b["identity_hash"]).one()
    assert secured.status == "secured"
    assert secured.result_hash == canonical_hash(_manifest(req_b.run_id))
