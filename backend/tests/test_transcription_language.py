"""JFW-7: Sprachwahl, Modelloutput und Transcribe-Riegel — Vertragstests (TDD, RED zuerst).

Spec AC „Deutsch, Englisch und Sprachwahl": erkannte Sprache ist Modelloutput mit
Provenienz, Auto-Erkennung ändert nie dauerhafte Einstellungen, `translate` ist im
Produkt nicht erreichbar.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_language.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.transcription.language import (
    LANGUAGE_SETTINGS,
    TranslatePfadVerbotenError,
    assert_task_transcribe,
    auto_detect_changes_settings,
    chosen_language,
    language_param,
    model_language,
    record_language,
)


def test_spracheinstellungen_sind_auto_de_en():
    assert LANGUAGE_SETTINGS == ("auto", "de", "en")


def test_language_param_auto_bedeutet_modellerkennung():
    assert language_param("auto") is None
    assert language_param("de") == "de"
    assert language_param("en") == "en"
    with pytest.raises(ValueError, match="spracheinstellung_unbekannt"):
        language_param("fr")


def test_translate_ist_unerreichbar():
    assert_task_transcribe({"task": "transcribe"})
    with pytest.raises(TranslatePfadVerbotenError) as e:
        assert_task_transcribe({"task": "translate"})
    assert "translate" in str(e.value)
    with pytest.raises(TranslatePfadVerbotenError):
        assert_task_transcribe({})


def test_modellsprache_ist_modelloutput_keine_nutzerabsicht():
    out = model_language("de", 0.87)
    assert out == {
        "detected": "de",
        "confidence": 0.87,
        "source": "model_output",
        "is_user_intent": False,
    }


def test_gewaehlte_sprache_ist_nutzerabsicht():
    out = chosen_language("de")
    assert out == {"setting": "de", "source": "user_setting", "is_user_intent": True}


def test_auto_erkennung_aendert_keine_dauerhafte_einstellung():
    assert auto_detect_changes_settings("de") is False
    assert auto_detect_changes_settings(None) is False


def test_record_language_faehrt_beide_quellen_getrennt():
    rec = record_language("auto", detected="de", confidence=0.7)
    assert rec["chosen"] is None
    assert rec["detected"]["source"] == "model_output"
    rec2 = record_language("de", detected="de", confidence=0.9)
    assert rec2["chosen"]["is_user_intent"] is True
    assert rec2["detected"]["is_user_intent"] is False
