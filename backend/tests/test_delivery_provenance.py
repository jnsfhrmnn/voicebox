"""JFW-8: Identity-/Payload-Hash-Bindung (TDD, RED zuerst beobachtet).

Muster JFW-2/JFW-3/JFW-6/JFW-7/JFW-11: ``identity_hash`` ist die stabile
Identität der Delivery-Operation (Operations-ID + JFW-7-Revision +
Vertragsversion); ``payload_hash`` bindet Text-Hash, Ziel-Snapshot-Hash,
Run-/Audio-/Attempt-IDs und Zielbestätigungsflag. Identischer Payload =
idempotent; abweichender Payload = fail-closed Konflikt.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_provenance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.provenance import DeliveryRequest, new_operation_id
from backend.transcription.raw_transcript import text_hash

TEXT = "Rohtext."

def _request(**overrides) -> DeliveryRequest:
    base = dict(
        delivery_operation_id=new_operation_id(),
        run_id="jfw6-run-" + "0" * 32,
        audio_hash="a" * 64,
        attempt_id="att-1",
        revision_id="rev-1",
        raw_text=TEXT,
        text_hash=text_hash(TEXT),
        target_snapshot={"target_snapshot_hash": "t" * 64},
        target_confirmed=False,
    )
    base.update(overrides)
    return DeliveryRequest(**base)

def test_operations_id_ist_stabil_geformt():
    op_id = new_operation_id()
    assert op_id.startswith("jfw8-op-")
    assert len(op_id) == len("jfw8-op-") + 32

def test_identitaet_bindet_operation_und_revision():
    a = _request(delivery_operation_id="jfw8-op-" + "1" * 32)
    b = _request(
        delivery_operation_id="jfw8-op-" + "1" * 32,
        raw_text="anderer Text",
        text_hash=text_hash("anderer Text"),
    )
    assert a.identity_hash() == b.identity_hash()  # Identität = Operation + Revision
    assert a.payload_hash() != b.payload_hash()  # Inhalt ist gebunden

def test_payload_hash_bindet_text_ziel_und_flag():
    base = _request(delivery_operation_id="jfw8-op-" + "1" * 32)
    changed_text = _request(
        delivery_operation_id="jfw8-op-" + "1" * 32,
        raw_text="x",
        text_hash=text_hash("x"),
    )
    changed_target = _request(
        delivery_operation_id="jfw8-op-" + "1" * 32,
        target_snapshot={"target_snapshot_hash": "u" * 64},
    )
    changed_flag = _request(
        delivery_operation_id="jfw8-op-" + "1" * 32, target_confirmed=True
    )
    hashes = {
        base.payload_hash(),
        changed_text.payload_hash(),
        changed_target.payload_hash(),
        changed_flag.payload_hash(),
    }
    assert len(hashes) == 4

def test_hash_ist_kanonisch_und_reproduzierbar():
    a = _request(delivery_operation_id="jfw8-op-" + "1" * 32)
    b = _request(delivery_operation_id="jfw8-op-" + "1" * 32)
    assert a.payload_hash() == b.payload_hash()
    assert len(a.payload_hash()) == 64

def test_unzulaessige_konstruktion():
    with pytest.raises(ValueError, match="run_id_unzulaessig"):
        _request(run_id="rando")
    with pytest.raises(ValueError, match="abweichender_rohtext"):
        _request(text_hash="0" * 64)  # passt nicht zum Text
