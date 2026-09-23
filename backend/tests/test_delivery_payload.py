"""JFW-8: Delivery-Payload — Vertragstests (TDD, RED zuerst beobachtet).

Spec AC „Rohtext-Payload und Idempotenz": versioniertes Payload mit Run-,
Audio-, Attempt-, Rohtranskript- und Delivery-Operation-ID sowie Text-/
Payload-Hash und Zielbestätigungsflag; verbotene Textmodi
(`refined|formatted|rewritten|bypassed|raw_fallback`) werden abgelehnt;
identische erneute Zustellung = derselbe Zustand, abweichender Text/Hash/Ziel
bei derselben Operations-ID = fail-closed Konflikt.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_payload.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.payload import (
    DELIVERY_CONTRACT_VERSION,
    DeliveryVertragError,
    build_delivery_payload,
    classify_resubmission,
    validate_delivery_payload,
)
from backend.recording.manifest import canonical_hash  # noqa: F401 — Reuse-Bezug
from backend.transcription.handoff import build_handoff_payload
from backend.transcription.raw_transcript import text_hash

TEXT = "genau ein Rohtranskript für die Uebergabe."
RUN_ID = "jfw6-run-" + "0" * 32

def _handoff(**overrides) -> dict:
    base = dict(
        run_id=RUN_ID,
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

def _target() -> dict:
    return {"target_snapshot_hash": "t" * 64, "capability": "direct_text"}

def _payload(**overrides) -> dict:
    base = dict(
        delivery_operation_id="jfw8-op-" + "1" * 32,
        handoff=_handoff(),
        target_snapshot=_target(),
    )
    base.update(overrides)
    return build_delivery_payload(**base)

def test_gueltiges_payload_ist_vertragsgemaess():
    payload = _payload()
    assert payload["contract_version"] == DELIVERY_CONTRACT_VERSION
    assert payload["delivery_operation_id"].startswith("jfw8-op-")
    assert payload["text_hash"] == text_hash(TEXT)
    assert payload["target_confirmed"] is False
    assert validate_delivery_payload(payload) == []

def test_pflichtfelder_fehlen_sind_fail_closed():
    payload = _payload()
    del payload["payload_hash"]
    errors = validate_delivery_payload(payload)
    assert "pflichtfeld_fehlt:payload_hash" in errors

def test_verbotene_textmodi_werden_abgelehnt():
    for mode in ("refined", "formatted", "rewritten", "bypassed", "raw_fallback"):
        payload = _payload()
        payload["text_mode"] = mode
        errors = validate_delivery_payload(payload)
        assert "textmodus_verboten" in errors, mode

def test_handoff_mit_verfeinertem_text_wird_nicht_uebernommen():
    # JFW-9-Riegel: ein als refined behauptetes Payload ist kein Rohtranskript.
    handoff = _handoff()
    handoff["payload_kind"] = "refined"
    with pytest.raises(DeliveryVertragError):
        build_delivery_payload(
            delivery_operation_id="jfw8-op-" + "1" * 32,
            handoff=handoff,
            target_snapshot=_target(),
        )

def test_abweichender_rohtext_zum_hash_ist_fail_closed():
    payload = _payload()
    payload["handoff"] = dict(payload["handoff"])
    payload["handoff"]["raw_text"] = "verändert"
    errors = validate_delivery_payload(payload)
    assert "abweichender_rohtext" in errors

def test_idempotenz_und_provenienzkonflikt():
    payload = _payload()
    assert classify_resubmission(None, payload) == "created"
    assert classify_resubmission(payload, _payload()) == "existing"
    # Dieselbe Operations-ID mit abweichendem Text: Konflikt, nichts schreiben.
    other = _payload(handoff=_handoff(raw_text="anderer Text", text_hash=text_hash("anderer Text")))
    assert classify_resubmission(payload, other) == "conflict"
    # Abweichendes Zielbestätigungsflag: ebenfalls Konflikt.
    confirmed = _payload(handoff=_handoff(target_confirmed=True))
    assert classify_resubmission(payload, confirmed) == "conflict"

def test_payload_hash_bindet_text_und_ziel():
    a = _payload()
    b = _payload(target_snapshot={"target_snapshot_hash": "u" * 64, "capability": "direct_text"})
    assert a["payload_hash"] != b["payload_hash"]
