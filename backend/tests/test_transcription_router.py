"""JFW-7: Dictation-Router — Vertragstests (TDD, RED zuerst).

Muster JFW-2/JFW-3/JFW-6/JFW-11 (Router gegen FastAPI-Handler-Funktionen).
Bindet den Vertragskern an die HTTP-Ebene inkl. JFW-8-Handoff-Payload.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_router.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base
from backend.routes import dictation as dictation_routes
from backend.transcription.raw_transcript import text_hash
from backend.transcription.snapshot import build_snapshot


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_tr_router_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _snapshot() -> dict:
    return build_snapshot(
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        stt_model="openai/whisper-large-v3-turbo",
        model_revision="1" * 40,
        language_setting="auto",
        backend_variant="cpu",
        backend_generation=4,
    )


def _submit_body():
    return dictation_routes.DictationSubmitRequest(
        run_id="jfw7-run-" + "0" * 32,
        source_kind="jfw6_handoff",
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        snapshot=_snapshot(),
        stop_reason="toggle",
    )


def _accepted(s) -> str:
    return dictation_routes.submit_dictation(_submit_body(), s)["identity_hash"]


def _commit_body(text="Hallo Rohwelt."):
    return dictation_routes.DictationCommitRequest(
        text=text,
        segments=[{"start_ms": 0, "end_ms": 500, "text": text}],
        language_output={"detected": "de", "source": "model_output", "is_user_intent": False},
        model_provenance={"model_profile": "jfw7-dictate-v1"},
    )


def test_submit_und_idempotente_wiederholung():
    s = _session()
    out1 = dictation_routes.submit_dictation(_submit_body(), s)
    out2 = dictation_routes.submit_dictation(_submit_body(), s)
    assert out1["outcome"] == "created"
    assert out2["outcome"] == "existing"


def test_submit_conflict_409():
    import fastapi
    import pytest

    s = _session()
    _accepted(s)
    body = _submit_body()
    body.manifest_hash = "f" * 64
    with pytest.raises(fastapi.HTTPException) as e:
        dictation_routes.submit_dictation(body, s)
    assert e.value.status_code == 409


def test_attempt_commit_und_get_liefern_denselben_rohtext():
    s = _session()
    ih = _accepted(s)
    att = dictation_routes.start_dictation_attempt(
        ih,
        dictation_routes.AttemptRequest(backend_variant="cpu", backend_generation=4),
        s,
    )
    assert att["status"] == "transcribing"
    out = dictation_routes.commit_dictation(ih, _commit_body(), s)
    assert out["outcome"] == "committed"
    got = dictation_routes.get_dictation(ih, s)
    assert got["status"] == "raw_ready"
    assert got["raw_text"] == "Hallo Rohwelt."
    assert got["text_hash"] == text_hash("Hallo Rohwelt.")


def test_edit_und_handoff_payload():
    s = _session()
    ih = _accepted(s)
    dictation_routes.start_dictation_attempt(
        ih, dictation_routes.AttemptRequest(backend_variant="cpu", backend_generation=4), s
    )
    dictation_routes.commit_dictation(ih, _commit_body(), s)
    edit = dictation_routes.edit_dictation(
        ih, dictation_routes.UserEditRequest(text="Korrigiert."), s
    )
    assert edit["outcome"] == "saved"
    handoff = dictation_routes.build_dictation_handoff(ih, s)
    p = handoff["payload"]
    assert p["payload_kind"] == "raw_transcript"
    assert p["raw_text"] == "Hallo Rohwelt."  # Handoff traegt den RAW-Text
    assert p["text_hash"] == text_hash("Hallo Rohwelt.")
    assert p["target_confirmed"] is False
    assert p["stop_reason"] == "toggle"


def test_cancel_nach_commit_sichtbar_too_late():

    s = _session()
    ih = _accepted(s)
    dictation_routes.start_dictation_attempt(
        ih, dictation_routes.AttemptRequest(backend_variant="cpu", backend_generation=4), s
    )
    dictation_routes.commit_dictation(ih, _commit_body(), s)
    out = dictation_routes.cancel_dictation(
        ih, dictation_routes.DictationCancelRequest(reason_code="nutzerabbruch"), s
    )
    assert out["outcome"] in ("too_late", "already_terminal")


def test_unknown_identity_404():
    import fastapi
    import pytest

    s = _session()
    with pytest.raises(fastapi.HTTPException) as e:
        dictation_routes.get_dictation("f" * 64, s)
    assert e.value.status_code == 404
