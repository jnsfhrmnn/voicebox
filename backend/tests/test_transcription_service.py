"""JFW-7: Atomare Persistenz des Transkriptions-Runs — Vertragstests (TDD, RED zuerst).

Muster JFW-2/JFW-3/JFW-6/JFW-11: Idempotenz ueber ``payload_hash``, fail-closed
Provenienzkonflikt, genau eine autoritative Rohrevision pro Attempt, Terminal-Race
Commit vs. Cancel (erster dauerhafter Ausgang gewinnt), Crash-Recovery zeigt nie ein
Teilergebnis als final. Geteilte Datei-DB (kein ``sqlite://``) — Pitfall Concurrency.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_service.py
"""
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, TranscriptionRun, TranscriptRevision
from backend.services.transcription_contract import (
    cancel_run,
    commit_raw,
    fail_run,
    invalidate_run,
    record_no_speech,
    recover_interrupted,
    retranscribe,
    save_user_edit,
    start_attempt,
    submit_run,
)
from backend.transcription.language import model_language
from backend.transcription.provenance import TranscriptionRequest
from backend.transcription.raw_transcript import RAW_KIND, USER_EDITED_KIND, text_hash
from backend.transcription.snapshot import build_snapshot


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_tr_service_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _snapshot(**overrides) -> dict:
    base = dict(
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        stt_model="openai/whisper-large-v3-turbo",
        model_revision="1" * 40,
        language_setting="auto",
        backend_variant="cpu",
        backend_generation=4,
    )
    base.update(overrides)
    return build_snapshot(**base)


def _request(run_id="jfw7-run-" + "0" * 32, **overrides) -> TranscriptionRequest:
    base = dict(
        run_id=run_id,
        source_kind="jfw6_handoff",
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        snapshot=_snapshot(),
    )
    base.update(overrides)
    return TranscriptionRequest(**base)


def _accepted(s) -> str:
    out = submit_run(s, _request())
    return out["identity_hash"]


def _transcribing(s) -> str:
    ih = _accepted(s)
    start_attempt(
        s,
        ih,
        backend_variant="cpu",
        backend_generation=4,
        model_ready=True,
        backend_stable=True,
    )
    return ih


LANG = model_language("de", 0.9)


def test_submit_erzeugt_run_und_ist_idempotent():
    s = _session()
    out1 = submit_run(s, _request())
    out2 = submit_run(s, _request())
    assert out1["outcome"] == "created"
    assert out2["outcome"] == "existing"
    assert out1["identity_hash"] == out2["identity_hash"]
    assert out2["status"] == "queued"
    # keine zweite autoritative Revision/Run-Zeile
    assert s.query(TranscriptionRun).count() == 1


def test_abweichender_payload_ist_fail_closed_conflict():
    s = _session()
    _accepted(s)
    out = submit_run(s, _request(manifest_hash="f" * 64))
    assert out["outcome"] == "conflict"


def test_upload_und_retranscription_haben_stabile_identitaet():
    s = _session()
    up = submit_run(s, _request(source_kind="upload"))
    up2 = submit_run(s, _request(source_kind="upload"))
    assert up["outcome"] == "created"
    assert up2["outcome"] == "existing"
    rt = submit_run(s, _request(source_kind="retranscribe"))
    assert rt["outcome"] == "created"
    assert rt["identity_hash"] != up["identity_hash"]


def test_start_attempt_bindet_backend_und_liefert_attempt():
    s = _session()
    ih = _accepted(s)
    out = start_attempt(
        s, ih, backend_variant="cuda", backend_generation=5, model_ready=True, backend_stable=True
    )
    assert out["outcome"] == "started"
    assert out["status"] == "transcribing"
    assert out["attempt_id"]


def test_unklares_backend_bleibt_waiting_kein_stiller_fallback():
    s = _session()
    ih = _accepted(s)
    out = start_attempt(
        s, ih, backend_variant="cpu", backend_generation=4, model_ready=True, backend_stable=False
    )
    assert out["outcome"] == "waiting_for_backend"
    assert out["attempt_id"] is None


def test_fehlendes_modell_bleibt_waiting_ohne_automatischen_download():
    s = _session()
    ih = _accepted(s)
    out = start_attempt(
        s, ih, backend_variant="cpu", backend_generation=4, model_ready=False, backend_stable=True
    )
    assert out["outcome"] == "waiting_for_model"
    assert out["attempt_id"] is None


def test_commit_raw_erzeugt_genau_eine_unveraenderte_revision():
    s = _session()
    ih = _transcribing(s)
    text = "Hallo Welt, Rohtranskript."
    out = commit_raw(
        s,
        ih,
        text=text,
        segments=[{"start_ms": 0, "end_ms": 800, "text": text}],
        language_output=LANG,
        model_provenance={"model_profile": "jfw7-dictate-v1"},
        duration_ms=800,
    )
    assert out["outcome"] == "committed"
    assert out["text_hash"] == text_hash(text)
    rows = s.query(TranscriptRevision).all()
    assert len(rows) == 1
    rev = rows[0]
    assert rev.revision_kind == RAW_KIND
    assert rev.transcript_raw == text  # unveraendert
    assert rev.text_hash == text_hash(text)
    assert rev.run_identity_hash == ih
    run = s.query(TranscriptionRun).one()
    assert run.status == "raw_ready"
    assert run.revision_id == rev.id


def test_zweiter_commit_ist_keine_zweite_revision():
    s = _session()
    ih = _transcribing(s)
    commit_raw(s, ih, text="erstes", segments=[], language_output=LANG, model_provenance={})
    out2 = commit_raw(s, ih, text="zweites", segments=[], language_output=LANG, model_provenance={})
    assert out2["outcome"] == "already_terminal"
    assert s.query(TranscriptRevision).count() == 1


def test_no_speech_ohne_leeren_erfolgstext():
    s = _session()
    ih = _transcribing(s)
    out = record_no_speech(s, ih, language_output=LANG, model_provenance={})
    assert out["outcome"] == "no_speech"
    assert s.query(TranscriptRevision).count() == 0
    run = s.query(TranscriptionRun).one()
    assert run.status == "no_speech"
    assert run.result_hash is None  # kein JFW-8-Payload


def test_commit_mit_leerem_text_wird_no_speech_kein_leerer_erfolgstext():
    s = _session()
    ih = _transcribing(s)
    out = commit_raw(s, ih, text="   ", segments=[], language_output=LANG, model_provenance={})
    assert out["outcome"] == "no_speech"
    assert s.query(TranscriptRevision).count() == 0


def test_terminal_race_commit_vs_cancel_genau_ein_dauerhafter_sieger():
    for winner in ("commit", "cancel"):
        s = _session()
        ih = _transcribing(s)
        results = {}

        def do_commit(results=results, s=s, ih=ih):
            results["commit"] = commit_raw(
                s, ih, text="Text", segments=[], language_output=LANG, model_provenance={}
            )

        def do_cancel(results=results, s=s, ih=ih):
            results["cancel"] = cancel_run(s, ih, reason_code="nutzerabbruch")

        t1 = threading.Thread(target=do_commit)
        t2 = threading.Thread(target=do_cancel)
        if winner == "commit":
            t1.start()
            t1.join()
            t2.start()
            t2.join()
        else:
            t2.start()
            t2.join()
            t1.start()
            t1.join()
        run = s.query(TranscriptionRun).one()
        if winner == "commit":
            assert results["commit"]["outcome"] == "committed"
            assert results["cancel"] in ("too_late", "already_terminal")
            assert run.status == "raw_ready"
        else:
            assert results["cancel"] == "canceled"
            assert results["commit"]["outcome"] in ("canceled", "already_terminal")
            assert run.status == "canceled"
            assert s.query(TranscriptRevision).count() == 0  # nie autoritativ


def test_user_edit_ist_kindrevision_raw_bleibt():
    s = _session()
    ih = _transcribing(s)
    commit_raw(s, ih, text="Original", segments=[], language_output=LANG, model_provenance={})
    out = save_user_edit(s, ih, "bearbeitet")
    assert out["outcome"] == "saved"
    rows = s.query(TranscriptRevision).order_by(TranscriptRevision.created_at).all()
    assert len(rows) == 2
    raw = next(r for r in rows if r.revision_kind == RAW_KIND)
    edit = next(r for r in rows if r.revision_kind == USER_EDITED_KIND)
    assert raw.transcript_raw == "Original"
    assert edit.transcript_raw == "bearbeitet"
    assert edit.parent_revision_id == raw.id


def test_retranskription_neue_revision_alte_bleibt():
    s = _session()
    ih = _transcribing(s)
    commit_raw(s, ih, text="erstes Ergebnis", segments=[], language_output=LANG, model_provenance={})
    out = retranscribe(s, ih)
    assert out["outcome"] == "requed"
    ih2 = out["identity_hash"]
    start_attempt(
        s, ih2, backend_variant="cpu", backend_generation=4, model_ready=True, backend_stable=True
    )
    commit_raw(s, ih2, text="zweites Ergebnis", segments=[], language_output=LANG, model_provenance={})
    rows = s.query(TranscriptRevision).order_by(TranscriptRevision.created_at).all()
    texts = [r.transcript_raw for r in rows]
    assert "erstes Ergebnis" in texts  # nicht still ueberschrieben
    assert "zweites Ergebnis" in texts


def test_beschaedigtes_audio_scheitert_konkret_ohne_rekonstruktion():
    s = _session()
    ih = _transcribing(s)
    out = fail_run(s, ih, "audio_beschaedigt")
    assert out == "failed"
    run = s.query(TranscriptionRun).one()
    assert run.status == "failed"
    assert run.reason_code == "audio_beschaedigt"
    assert s.query(TranscriptRevision).count() == 0  # keine Textbehauptung


def test_invalidate_verlangt_neue_revision_oder_attempt():
    s = _session()
    ih = _transcribing(s)
    commit_raw(s, ih, text="alt", segments=[], language_output=LANG, model_provenance={})
    out = invalidate_run(s, ih, "audio_geaendert")
    assert out == "invalidated"
    assert s.query(TranscriptionRun).one().status == "invalidated"


def test_recover_interrupted_kein_teilergebnis_als_final():
    s = _session()
    _transcribing(s)
    n = recover_interrupted(s, current_epoch="epoch-neu")
    assert n == 1
    run = s.query(TranscriptionRun).one()
    assert run.status in ("queued", "failed")
    assert run.result_hash is None
    assert s.query(TranscriptRevision).count() == 0
