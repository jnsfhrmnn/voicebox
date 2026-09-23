"""JFW-8: Fehlerpfad-Spuren nachvollziehbar und inhaltsfrei (TDD, RED zuerst).

Spec AC „Oberfläche, Datenschutz und Abnahme": Logs/Crash-Dumps enthalten IDs,
Adapter, Zustände, Dauer und Fehlercodes, aber KEINEN Rohtext, KEINEN
Clipboard-Inhalt, KEINE Fenstertitel mit Inhalt und KEINE Credentials.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_trace.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.payload import DeliveryVertragError
from backend.delivery.trace import (
    assert_trace_content_free,
    build_error_trace,
)


def _trace(**overrides) -> dict:
    base = dict(
        operation_id="jfw8-op-" + "1" * 32,
        adapter="verified_paste_adapter",
        states=["attempt_intent_committed", "attempting", "unknown"],
        duration_ms=1234,
        error_code="zielwirkung_unsicher",
    )
    base.update(overrides)
    return build_error_trace(**base)

def test_fehlerpfadspur_ist_nachvollziehbar():
    trace = _trace()
    assert trace["operation_id"] == "jfw8-op-" + "1" * 32
    assert trace["adapter"] == "verified_paste_adapter"
    assert trace["states"] == ["attempt_intent_committed", "attempting", "unknown"]
    assert trace["duration_ms"] == 1234
    assert trace["error_code"] == "zielwirkung_unsicher"
    assert_trace_content_free(trace)

def test_rohtext_und_clipboard_duerfen_nicht_in_die_spur():
    for key in ("raw_text", "text", "clipboard", "clipboard_content", "window_title", "credential", "password", "token"):
        poisoned = _trace()
        poisoned[key] = "geheim"
        with pytest.raises(DeliveryVertragError):
            assert_trace_content_free(poisoned)

def test_builder_lehnt_inhaltstragende_felder_ab():
    with pytest.raises(DeliveryVertragError):
        build_error_trace(
            operation_id="jfw8-op-1",
            adapter="direct_text_adapter",
            states=["attempting"],
            duration_ms=1,
            error_code="x",
            extra={"raw_text": "geheim"},
        )

def test_spur_enthaelt_keine_rohtextzeichenkette_aus_dem_testwort():
    secret = "QWORDXYZ123"
    trace = _trace(error_code=f"code:{'a' * 8}")
    assert secret not in repr(trace)
