"""JFW-3: Persistenz- und Orchestrierungsvertrag — Vertragstests.

Atomare Ergebnisfreigabe (genau ein terminaler Ausgang), Idempotenz,
fail-closed Provenienzkonflikt, Sprecheranzahländerung als neue Ergebnisrevision,
Cancel/Commit-Race, Interrupted-Recovery, Invalidierung bei neuer
Transkriptrevision und heiliger Unveränderlichkeits-Check.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_diarization_service.py
"""
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.database.models import Base
from backend.diarization.artifacts import ArtifactGateError
from backend.diarization.engine import run_diarization
from backend.diarization.provenance import (
    DiarizationRequest,
    SpeakerSpec,
    transcript_text_hash,
)
from backend.services.diarization_contract import (
    begin_attempt,
    cancel_diarization,
    commit_result,
    fail_attempt,
    invalidate_other_revisions,
    recover_interrupted,
    submit_diarization,
)

TEXT = "hallo welt das ist ein test"
MODEL_PROV = {
    "model_id": "pyannote/speaker-diarization-community-1",
    "model_revision": "1" * 40,
    "model_sha256": "a" * 64,
    "model_license": "CC-BY-4.0",
}

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

def base_words():
    return [
        {
            "word_id": "w-0000",
            "order": 0,
            "char_start": 0,
            "char_end": 5,
            "text": "hallo",
            "status": "alignable",
            "start_ms": 100.0,
            "end_ms": 400.0,
        },
        {
            "word_id": "w-0001",
            "order": 1,
            "char_start": 6,
            "char_end": 10,
            "text": "welt",
            "status": "alignable",
            "start_ms": 1100.0,
            "end_ms": 1400.0,
        },
    ]

def req(**kw):
    defaults = dict(
        job_id="job-1",
        audio_asset_id="audio-1",
        audio_hash="sha256:audio",
        audio_duration_ms=60000,
        timebase="audio_ms_v1",
        transcript_run_id="run-1",
        transcript_revision_id="rev-1",
        transcript_revision_hash=transcript_text_hash(TEXT),
        jfw2_reference_status="aligned",
        jfw2_result_hash="abc123",
        speaker_spec=SpeakerSpec.parse("auto"),
    )
    defaults.update(kw)
    return DiarizationRequest(**defaults)

def fake_gate():
    return dict(MODEL_PROV)

def fake_provider_ok(duration_ms, speaker_spec):
    return [
        {"start_ms": 0.0, "end_ms": 900.0, "speaker_key": "A", "score": 0.9,
         "overlap_hint": "unknown"},
        {"start_ms": 1000.0, "end_ms": 1900.0, "speaker_key": "B", "score": 0.9,
         "overlap_hint": "unknown"},
    ]

def built_ok():
    return {
        "status": "diarized",
        "reason_code": None,
        "clusters": [{"cluster_id": "speaker_01", "display_label": "Sprecher 1"}],
        "turns": [{"turn_id": "t-0000"}],
        "words": [{"word_id": "w-0000"}],
        "coverage": {"speech_ms": 1800.0, "usable_ms": 1800.0},
        "counters": {
            "cluster_count": 1,
            "turn_count": 1,
            "word_count": 1,
            "word_assigned_count": 1,
            "overlap_count": 0,
            "uncertainty_count": 0,
            "invalid_turn_count": 0,
        },
        "result_hash": "r1",
    }

def engine_run(session, request=None, provider=fake_provider_ok, gate=fake_gate,
               text_=TEXT, words=None):
    return run_diarization(
        session,
        request or req(),
        text_,
        base_words() if words is None else words,
        provider=provider,
        artifact_gate=gate,
        app_epoch="epoch-A",
    )

# --- Schema ---------------------------------------------------------------

def test_fresh_schema_has_diarization_table():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert "diarization_results" in set(inspect(engine).get_table_names())
    engine.dispose()

# --- Submit / Idempotenz / Provenienz --------------------------------------

def test_submit_creates_queued_row(session):
    out = submit_diarization(session, req(), TEXT)
    assert out["outcome"] == "created"
    assert out["status"] == "queued"

def test_resubmit_identical_is_idempotent(session):
    submit_diarization(session, req(), TEXT)
    out = submit_diarization(session, req(), TEXT)
    assert out["outcome"] == "existing"
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM diarization_results")).scalar()
    assert n == 1

def test_submit_conflict_same_identity_other_payload(session):
    submit_diarization(session, req(), TEXT)
    out = submit_diarization(session, req(audio_hash="sha256:geaendert"), TEXT)
    assert out["outcome"] == "conflict"  # fail-closed, kein Ueberschreiben
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM diarization_results")).scalar()
    assert n == 1

def test_submit_rejects_text_hash_mismatch(session):
    out = submit_diarization(session, req(), "anderer text")
    assert out["outcome"] == "revision_hash_mismatch"
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM diarization_results")).scalar()
    assert n == 0

def test_speaker_spec_change_creates_new_revision_keeps_old(session):
    submit_diarization(session, req(), TEXT)
    forced = req(speaker_spec=SpeakerSpec.parse("exact", count=2))
    out = submit_diarization(session, forced, TEXT)
    assert out["outcome"] == "created"  # neue Ergebnisrevision
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM diarization_results")).scalar()
    assert n == 2  # frueheres Ergebnis bleibt revisionsgebunden erhalten

# --- Attempt / Commit / Cancel ---------------------------------------------

def test_begin_and_commit_writes_result(session):
    submit_diarization(session, req(), TEXT)
    attempt_id = begin_attempt(session, req().identity_hash(), "epoch-A")
    assert attempt_id
    out = commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV)
    assert out == "committed"
    assert begin_attempt(session, req().identity_hash(), "epoch-A") is None

def test_commit_persists_full_result_head(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV)
    row = submit_diarization(session, req(), TEXT)
    assert row["status"] == "diarized"
    assert row["result_hash"] == "r1"
    assert row["model_id"] == "pyannote/speaker-diarization-community-1"

def test_double_commit_rejected(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    assert commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV) == "committed"
    assert commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV) == "already_terminal"

def test_cancel_wins_no_result_becomes_authoritative(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    assert cancel_diarization(session, req().identity_hash()) == "canceled"
    assert commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV) == "canceled"

def test_cancel_after_commit_is_too_late(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV)
    assert cancel_diarization(session, req().identity_hash()) == "too_late"

def test_error_without_commit_allows_identical_retry(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    assert fail_attempt(session, req().identity_hash(), "provider_error") is True
    assert begin_attempt(session, req().identity_hash(), "epoch-B") is not None

def test_interrupted_run_is_recovered_not_authoritative(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-OLD")
    assert recover_interrupted(session, current_epoch="epoch-A") == 1
    row = submit_diarization(session, req(), TEXT)
    assert row["status"] == "queued"
    assert begin_attempt(session, req().identity_hash(), "epoch-A") is not None

def test_recovery_leaves_committed_results_alone(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-OLD")
    commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV)
    assert recover_interrupted(session, current_epoch="epoch-A") == 0

def test_new_revision_invalidates_old_result_but_keeps_binding(session):
    submit_diarization(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    commit_result(session, req().identity_hash(), built_ok(), MODEL_PROV)
    n = invalidate_other_revisions(
        session,
        job_id="job-1",
        current_transcript_revision_id="rev-2",
        current_jfw2_result_hash="abc123",
    )
    assert n == 1
    row = submit_diarization(session, req(), TEXT)
    assert row["status"] == "invalidated"
    assert row["transcript_revision_id"] == "rev-1"  # revisionsgebunden erhalten
    assert row["result_hash"] == "r1"

# --- Engine ----------------------------------------------------------------

def test_engine_commits_full_diarization(session):
    out = engine_run(session)
    assert out["outcome"] == "committed"
    assert out["status"] == "diarized"
    assert out["result_hash"]
    assert out["model_id"] == "pyannote/speaker-diarization-community-1"

def test_engine_is_idempotent_on_rerun(session):
    first = engine_run(session)
    second = engine_run(session)
    assert first["outcome"] == "committed"
    assert second["outcome"] == "existing"
    assert second["result_hash"] == first["result_hash"]

def test_engine_partial_result_visible(session):
    def provider(duration_ms, speaker_spec):
        turns = []
        for i in range(20):
            turns.append(
                {"start_ms": i * 1000.0, "end_ms": i * 1000.0 + 900.0,
                 "speaker_key": "A", "score": 0.9,
                 "overlap_hint": "uncertain" if i == 0 else "unknown"}
            )
        return turns

    out = engine_run(session, provider=provider, words=[])
    assert out["status"] == "partially_diarized"
    assert out["coverage_usable_ms"] == out["coverage_speech_ms"] - 900.0

def test_engine_missing_artifact_is_waiting_without_network(session):
    def gate():
        raise ArtifactGateError("waiting_for_local_artifact")

    out = engine_run(session, gate=gate)
    assert out["status"] == "waiting_for_local_artifact"
    assert out["result_hash"] is None

def test_engine_provider_error_fails_without_commit(session):
    def provider(duration_ms, speaker_spec):
        raise RuntimeError("gpu oom")

    out = engine_run(session, provider=provider)
    assert out["outcome"] == "failed"
    assert out["result_hash"] is None  # kein autoritativer Commit

def test_engine_word_mutation_is_fail_closed(session):
    bad = [dict(base_words()[0], text="MUTATION")]
    out = engine_run(session, words=bad)
    assert out["outcome"] == "failed"
    assert out["result_hash"] is None

def test_engine_conflict_is_fail_closed(session):
    engine_run(session)
    out = engine_run(session, request=req(audio_hash="sha256:andere-audio"))
    assert out["outcome"] == "conflict"
    assert out["result_hash"]  # das vorhandene Ergebnis bleibt unangetastet

def test_engine_forced_speaker_spec_stays_visible(session):
    out = engine_run(session, request=req(speaker_spec=SpeakerSpec.parse("exact", count=2)))
    assert out["outcome"] == "committed"
    assert out["speaker_mode"] == "exact"

def test_concurrent_commit_and_cancel_exactly_one_terminal():
    """Race: Commit und Cancel treffen fast gleichzeitig ein — genau ein Ausgang."""
    import tempfile

    db_path = Path(tempfile.mktemp(suffix=".db"))
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    setup = factory()
    request = req()
    submit_diarization(setup, request, TEXT)
    begin_attempt(setup, request.identity_hash(), "epoch-A")
    setup.close()

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def do_commit():
        s = factory()
        try:
            barrier.wait()
            outcomes.append("commit:" + commit_result(s, request.identity_hash(), built_ok(), MODEL_PROV))
        finally:
            s.close()

    def do_cancel():
        s = factory()
        try:
            barrier.wait()
            outcomes.append("cancel:" + cancel_diarization(s, request.identity_hash()))
        finally:
            s.close()

    threads = [threading.Thread(target=do_commit), threading.Thread(target=do_cancel)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    committed = [o for o in outcomes if o == "commit:committed"]
    cancelled = [o for o in outcomes if o == "cancel:canceled"]
    assert len(committed) + len(cancelled) == 1, f"genau ein terminaler Ausgang: {outcomes}"

    with engine.connect() as conn:
        n_rows = conn.execute(text("SELECT COUNT(*) FROM diarization_results")).scalar()
    assert n_rows == 1
    engine.dispose()
    db_path.unlink(missing_ok=True)
