"""JFW-2: Persistenz- und Orchestrierungsvertrag — Vertragstests.

Atomare Ergebnisfreigabe (genau ein terminaler Ausgang), Idempotenz,
fail-closed Provenienzkonflikt, Cancel/Commit-Race, Interrupted-Recovery,
Invalidierung bei neuer Transkriptrevision und No-text-change-Gate.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_alignment_service.py
"""
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.alignment.artifacts import ArtifactGateError  # noqa: E402
from backend.alignment.provenance import (  # noqa: E402
    AlignmentRequest,
    transcript_text_hash,
)
from backend.alignment.engine import run_alignment  # noqa: E402
from backend.database.models import Base  # noqa: E402
from backend.services.alignment_contract import (  # noqa: E402
    begin_attempt,
    cancel_alignment,
    commit_result,
    fail_attempt,
    invalidate_other_revisions,
    recover_interrupted,
    submit_alignment,
)

TEXT = "hallo welt das ist ein test"
MODEL_PROV = {
    "model_id": "facebook/mms_fa",
    "model_revision": "0" * 40,
    "model_sha256": "a" * 64,
    "model_license": "CC-BY-NC-4.0",
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
        language_ranges=(("de", 0, len(TEXT)),),
        alignment_profile="precise_words_v1",
    )
    defaults.update(kw)
    return AlignmentRequest(**defaults)


def fake_gate():
    return dict(MODEL_PROV)


def fake_provider_ok(words, duration_ms, language_ranges):
    out = {}
    t = 0.0
    for w in words:
        if w["status"] == "not_applicable":
            continue
        out[w["word_id"]] = (t, t + 250.0, 0.9)
        t += 500.0
    return out


def engine_run(session, request=None, provider=fake_provider_ok, gate=fake_gate, text_=TEXT):
    return run_alignment(
        session,
        request or req(),
        text_,
        provider=provider,
        artifact_gate=gate,
        app_epoch="epoch-A",
    )


# --- Schema ---------------------------------------------------------------

def test_fresh_schema_has_alignment_table():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    assert "alignment_results" in set(inspect(engine).get_table_names())
    engine.dispose()


# --- Submit / Idempotenz / Provenienz --------------------------------------

def test_submit_creates_queued_row(session):
    out = submit_alignment(session, req(), TEXT)
    assert out["outcome"] == "created"
    assert out["status"] == "queued"


def test_resubmit_identical_is_idempotent(session):
    submit_alignment(session, req(), TEXT)
    out = submit_alignment(session, req(), TEXT)
    assert out["outcome"] == "existing"
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM alignment_results")).scalar()
    assert n == 1


def test_submit_conflict_same_identity_other_payload(session):
    submit_alignment(session, req(), TEXT)
    out = submit_alignment(session, req(audio_hash="sha256:geaendert"), TEXT)
    assert out["outcome"] == "conflict"  # fail-closed, kein Ueberschreiben
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM alignment_results")).scalar()
    assert n == 1


def test_submit_rejects_text_hash_mismatch(session):
    out = submit_alignment(session, req(), "anderer text")
    assert out["outcome"] == "revision_hash_mismatch"
    with session.get_bind().connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM alignment_results")).scalar()
    assert n == 0


# --- Attempt / Commit / Cancel ---------------------------------------------

def test_begin_and_commit_writes_result(session):
    submit_alignment(session, req(), TEXT)
    attempt_id = begin_attempt(session, req().identity_hash(), "epoch-A")
    assert attempt_id
    built = {"status": "aligned", "words": [{"word_id": "w-0000"}],
             "coverage_alignable": 1, "coverage_aligned": 1, "result_hash": "r1"}
    out = commit_result(session, req().identity_hash(), built, MODEL_PROV)
    assert out == "committed"
    assert begin_attempt(session, req().identity_hash(), "epoch-A") is None  # kein Doppellauf


def test_double_commit_rejected(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    built = {"status": "aligned", "words": [], "coverage_alignable": 1,
             "coverage_aligned": 1, "result_hash": "r1"}
    assert commit_result(session, req().identity_hash(), built, MODEL_PROV) == "committed"
    assert commit_result(session, req().identity_hash(), built, MODEL_PROV) == "already_terminal"


def test_cancel_wins_no_result_becomes_authoritative(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    assert cancel_alignment(session, req().identity_hash()) == "canceled"
    built = {"status": "aligned", "words": [], "coverage_alignable": 1,
             "coverage_aligned": 1, "result_hash": "r1"}
    assert commit_result(session, req().identity_hash(), built, MODEL_PROV) == "canceled"


def test_cancel_after_commit_is_too_late(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    built = {"status": "aligned", "words": [], "coverage_alignable": 1,
             "coverage_aligned": 1, "result_hash": "r1"}
    commit_result(session, req().identity_hash(), built, MODEL_PROV)
    assert cancel_alignment(session, req().identity_hash()) == "too_late"


def test_error_without_commit_allows_identical_retry(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    assert fail_attempt(session, req().identity_hash(), "provider_error") is True
    # Noch kein Ergebnis-Commit -> identischer Retry darf neu rechnen.
    assert begin_attempt(session, req().identity_hash(), "epoch-B") is not None


def test_interrupted_run_is_recovered_not_authoritative(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-OLD")
    n = recover_interrupted(session, current_epoch="epoch-A")
    assert n == 1
    row = submit_alignment(session, req(), TEXT)
    assert row["status"] == "queued"  # wieder fortsetzbar, kein autoritatives Ergebnis
    assert begin_attempt(session, req().identity_hash(), "epoch-A") is not None


def test_recovery_leaves_committed_results_alone(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-OLD")
    built = {"status": "aligned", "words": [], "coverage_alignable": 1,
             "coverage_aligned": 1, "result_hash": "r1"}
    commit_result(session, req().identity_hash(), built, MODEL_PROV)
    assert recover_interrupted(session, current_epoch="epoch-A") == 0


def test_new_revision_invalidates_old_result_but_keeps_binding(session):
    submit_alignment(session, req(), TEXT)
    begin_attempt(session, req().identity_hash(), "epoch-A")
    built = {"status": "aligned", "words": [], "coverage_alignable": 1,
             "coverage_aligned": 1, "result_hash": "r1"}
    commit_result(session, req().identity_hash(), built, MODEL_PROV)
    n = invalidate_other_revisions(session, job_id="job-1", current_transcript_revision_id="rev-2")
    assert n == 1
    row = submit_alignment(session, req(), TEXT)
    assert row["status"] == "invalidated"
    assert row["transcript_revision_id"] == "rev-1"  # revisionsgebunden erhalten
    assert row["result_hash"] == "r1"


# --- Engine ----------------------------------------------------------------

def test_engine_commits_full_alignment(session):
    out = engine_run(session)
    assert out["outcome"] == "committed"
    assert out["status"] == "aligned"
    assert out["result_hash"]
    assert out["model_id"] == "facebook/mms_fa"


def test_engine_is_idempotent_on_rerun(session):
    first = engine_run(session)
    second = engine_run(session)
    assert first["outcome"] == "committed"
    assert second["outcome"] == "existing"
    assert second["result_hash"] == first["result_hash"]


def test_engine_partial_alignment_visible(session):
    text_ = "wort " * 20  # 20 ausrichtbare Woerter, 1 nicht verortbar -> 95 %

    def provider(words, duration_ms, language_ranges):
        out = {}
        for i, w in enumerate(words):
            if w["status"] == "not_applicable":
                continue
            if w["text"] == "wort" and w["order"] == 0:
                out[w["word_id"]] = None  # nicht verortbar
            else:
                out[w["word_id"]] = (w["order"] * 100.0, w["order"] * 100.0 + 50.0, 0.8)
        return out

    out = engine_run(
        session,
        request=req(transcript_revision_hash=transcript_text_hash(text_)),
        provider=provider,
        text_=text_,
    )
    assert out["status"] == "partially_aligned"
    assert out["coverage_aligned"] == out["coverage_alignable"] - 1


def test_engine_missing_artifact_is_waiting_without_network(session):
    def gate():
        raise ArtifactGateError("waiting_for_local_artifact")

    out = engine_run(session, gate=gate)
    assert out["status"] == "waiting_for_local_artifact"
    assert out["result_hash"] is None


def test_engine_provider_error_fails_without_commit(session):
    def provider(words, duration_ms, language_ranges):
        raise RuntimeError("gpu oom")

    out = engine_run(session, provider=provider)
    assert out["outcome"] == "failed"
    assert out["result_hash"] is None  # kein autoritativer Commit


def test_engine_conflict_is_fail_closed(session):
    engine_run(session)
    out = engine_run(session, request=req(audio_hash="sha256:andere-audio"))
    assert out["outcome"] == "conflict"
    assert out["result_hash"]  # das vorhandene Ergebnis bleibt unangetastet


def test_engine_no_text_change_gate_blocks_mutation(session):
    # Der Text-Hash der Request bindet den Text; eine Mutation wird abgelehnt.
    out = run_alignment(
        session, req(), "hallo welt das ist ein ANDERER test",
        provider=fake_provider_ok, artifact_gate=fake_gate, app_epoch="epoch-A",
    )
    assert out["outcome"] == "revision_hash_mismatch"
    assert out["result_hash"] is None


def test_concurrent_commit_and_cancel_exactly_one_terminal():
    """Race: Commit und Cancel treffen fast gleichzeitig ein — genau ein Ausgang."""
    import tempfile

    db_path = Path(tempfile.mktemp(suffix=".db"))
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    setup = factory()
    request = req()
    submit_alignment(setup, request, TEXT)
    begin_attempt(setup, request.identity_hash(), "epoch-A")
    setup.close()

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def do_commit():
        s = factory()
        try:
            barrier.wait()
            built = {"status": "aligned", "words": [], "coverage_alignable": 1,
                     "coverage_aligned": 1, "result_hash": "r1"}
            outcomes.append("commit:" + commit_result(s, request.identity_hash(), built, MODEL_PROV))
        finally:
            s.close()

    def do_cancel():
        s = factory()
        try:
            barrier.wait()
            outcomes.append("cancel:" + cancel_alignment(s, request.identity_hash()))
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
        n_rows = conn.execute(text("SELECT COUNT(*) FROM alignment_results")).scalar()
    assert n_rows == 1
    engine.dispose()
    db_path.unlink(missing_ok=True)
