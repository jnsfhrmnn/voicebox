"""JFW-7: JFW-8-Handoff-Payload und Ableitungsbindung — Vertragstests (TDD, RED zuerst).

Spec AC „Übergabe und Datenschutz": genau ein versioniertes Payload mit Run-/Audio-/
Attempt-/Revision-ID, Rohtext und Hash, Sprache, Backend-/Modellprofil, Stopgrund und
Zielbestätigungsflag; `refined`/`formatted`/`rewritten` wird nie als JFW-7-Rohtranskript
akzeptiert; Ableitungen binden an Revision-ID + Hash und lassen den Rohtext unverändert.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_handoff.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.transcription.handoff import (
    HANDOFF_CONTRACT_VERSION,
    HandoffVertragError,
    assert_payload_kind,
    build_derivation_binding,
    build_handoff_payload,
    validate_handoff_payload,
    verify_derivation_text,
)
from backend.transcription.raw_transcript import text_hash

TEXT = "genau ein Rohtranskript."


def _payload(**overrides) -> dict:
    base = dict(
        run_id="jfw7-run-" + "0" * 32,
        audio_hash="a" * 64,
        attempt_id="att-1",
        revision_id="rev-1",
        raw_text=TEXT,
        text_hash=text_hash(TEXT),
        language={"detected": "de", "source": "model_output", "is_user_intent": False},
        backend_model_profile={
            "backend_variant": "cpu",
            "backend_generation": 4,
            "model_profile": "jfw7-dictate-v1",
            "stt_model": "openai/whisper-large-v3-turbo",
        },
        stop_reason="toggle",
    )
    base.update(overrides)
    return build_handoff_payload(**base)


def test_payload_traegt_alle_pflichtfelder():
    p = _payload()
    assert p["contract_version"] == HANDOFF_CONTRACT_VERSION
    assert p["payload_kind"] == "raw_transcript"
    assert p["run_id"] == "jfw7-run-" + "0" * 32
    assert p["audio_hash"] == "a" * 64
    assert p["attempt_id"] == "att-1"
    assert p["revision_id"] == "rev-1"
    assert p["raw_text"] == TEXT
    assert p["text_hash"] == text_hash(TEXT)
    assert p["language"]["detected"] == "de"
    assert p["backend_model_profile"]["model_profile"] == "jfw7-dictate-v1"
    assert p["stop_reason"] == "toggle"
    assert p["target_confirmed"] is False  # Zielbestätigungsflag


def test_validate_gueltiges_payload():
    assert validate_handoff_payload(_payload()) == []


def test_validate_faengt_textaenderung_und_fehlfelder():
    p = _payload()
    p["raw_text"] = "geschönt."
    assert "abweichender_rohtext" in validate_handoff_payload(p)
    p2 = _payload()
    del p2["stop_reason"]
    assert "pflichtfeld_fehlt:stop_reason" in validate_handoff_payload(p2)


def test_verfeinerte_payloads_werden_nie_als_rohtranskript_akzeptiert():
    for kind in ("refined", "formatted", "rewritten"):
        p = _payload()
        p["payload_kind"] = kind
        assert "payload_kind_verboten" in validate_handoff_payload(p)
        with pytest.raises(HandoffVertragError) as e:
            assert_payload_kind(p)
        assert "abweichendes_payload_abgelehnt" in str(e.value)
    p3 = _payload()
    p3["payload_kind"] = "halluziniert"
    assert "abweichendes_payload" in validate_handoff_payload(p3)


def test_ableitung_bindet_revision_id_und_hash():
    b = build_derivation_binding(revision_id="rev-1", text_hash=text_hash(TEXT), source="jfw2")
    assert b["revision_id"] == "rev-1"
    assert b["text_hash"] == text_hash(TEXT)
    assert b["source"] == "jfw2"
    assert verify_derivation_text(b, TEXT) is True
    assert verify_derivation_text(b, TEXT + "x") is False
