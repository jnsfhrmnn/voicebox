"""JFW-7: Unverändertes Rohtranskript (heiliger Vertrag, JFW-9-Riegel) — Vertragstests (TDD, RED zuerst).

Spec AC „Rohtranskript und Inhaltstreue": genau eine unveränderte `raw_transcript`-
Revision, keine Nachbereinigung, `user_edited` getrennt mit Elternbezug, Retranskription
neu statt Überschreiben, `no_speech` ohne leeren Erfolgstext, beschädigtes Audio ohne
Rekonstruktion. JFW-9-Verbot: KEIN Text-LLM, KEIN Refinement — verbotene Kinds werden
fail-closed abgelehnt.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_raw_transcript.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.transcription.language import model_language
from backend.transcription.raw_transcript import (
    FORBIDDEN_KINDS,
    RAW_KIND,
    USER_EDITED_KIND,
    RawVertragError,
    assert_raw_kind,
    build_raw_revision,
    has_speech,
    text_hash,
    user_edited_revision,
    verify_audio_binding,
    verify_unmodified,
)

LANG = model_language("de", 0.92)


def _raw(text="hallo welt, das ist ein Test mit API-Key und so.", **overrides) -> dict:
    base = dict(
        text=text,
        segments=[{"start_ms": 0, "end_ms": 900, "text": text}],
        language_output=LANG,
        audio_hash="a" * 64,
        attempt_id="att-1",
        model_provenance={
            "model_profile": "jfw7-dictate-v1",
            "stt_model": "openai/whisper-large-v3-turbo",
            "model_revision": "1" * 40,
            "backend_variant": "cpu",
            "backend_generation": 4,
        },
    )
    base.update(overrides)
    return build_raw_revision(**base)


def test_raw_revision_traegt_text_hash_sprache_segmente_provenienz_und_audio():
    rev = _raw()
    assert rev["revision_kind"] == RAW_KIND
    assert rev["parent_revision_id"] is None
    assert rev["text_hash"] == text_hash(rev["text"])
    assert rev["segments"][0]["end_ms"] == 900
    assert rev["language"] == LANG
    assert rev["provenance"]["audio_hash"] == "a" * 64
    assert rev["provenance"]["attempt_id"] == "att-1"
    assert rev["provenance"]["model"]["backend_generation"] == 4


def test_text_hash_ist_byteexakt():
    assert text_hash("hallo") == text_hash("hallo")
    assert text_hash("hallo") != text_hash("Hallo")
    assert text_hash("hallo") != text_hash("hallo ")


def test_keine_nachbereinigung_verify_unmodified():
    raw = _raw()
    assert verify_unmodified(raw["text"], raw["text"]) is True
    assert verify_unmodified(raw["text"], raw["text"].replace("Test", "test")) is False
    assert verify_unmodified(raw["text"], raw["text"].strip() + ".") is False


def test_fuellwoerter_und_satzzeichen_bleiben_stehen():
    rev = _raw(text="Ähm, wir — also ich ändere das, äh, später. OK?")
    assert rev["text"] == "Ähm, wir — also ich ändere das, äh, später. OK?"
    assert rev["text_hash"] == text_hash("Ähm, wir — also ich ändere das, äh, später. OK?")


def test_user_edited_ist_getrennte_kindrevision_mit_elternbezug():
    parent = _raw()
    parent["revision_id"] = "rev-raw-1"
    kind = user_edited_revision(parent, "korrigierter Text")
    assert kind["revision_kind"] == USER_EDITED_KIND
    assert kind["parent_revision_id"] == "rev-raw-1"
    assert kind["text"] == "korrigierter Text"
    assert kind["text_hash"] == text_hash("korrigierter Text")
    # raw_transcript bleibt unverändert
    assert parent["text"] == "hallo welt, das ist ein Test mit API-Key und so."
    assert parent["text_hash"] == text_hash(parent["text"])


def test_user_edited_verlangt_raw_als_elter():
    kind_only = {"revision_kind": USER_EDITED_KIND, "revision_id": "x", "text": "a"}
    with pytest.raises(RawVertragError):
        user_edited_revision(kind_only, "b")


def test_retranskription_erzeugt_neue_revision_statt_ueberschreiben():
    rev1 = _raw(text="erstes Ergebnis")
    rev2 = _raw(text="zweites Ergebnis", attempt_id="att-2")
    assert rev1["text"] == "erstes Ergebnis"  # unangetastet
    assert rev2["text_hash"] != rev1["text_hash"]
    assert rev2["provenance"]["attempt_id"] == "att-2"


def test_no_speech_leer_oder_nur_stille():
    assert has_speech("", []) is False
    assert has_speech("   \n ", None) is False
    assert has_speech("", [{"start_ms": 0, "end_ms": 1}]) is True
    assert has_speech("ton", []) is True


def test_jfw9_verbotene_kinds_abgelehnt():
    for kind in FORBIDDEN_KINDS:
        with pytest.raises(RawVertragError) as e:
            assert_raw_kind(kind)
        assert "jfw9_verboten" in str(e.value)
    assert_raw_kind(RAW_KIND)
    assert_raw_kind(None)  # Altzeilen gelten als raw_transcript


def test_unbekannter_kind_ist_fail_closed():
    with pytest.raises(RawVertragError):
        assert_raw_kind("halluziniert")


def test_sprachquelle_muss_als_modelloutput_oder_nutzerwahl_deklariert_sein():
    with pytest.raises(RawVertragError):
        _raw(language_output={"detected": "de", "source": "sicher_behauptet"})


def test_audio_binding_beschaedigtes_audio_scheitert_konkret():
    assert verify_audio_binding("a" * 64, "a" * 64) is True
    assert verify_audio_binding("a" * 64, "b" * 64) is False
