"""JFW-8: Delivery-Router — Vertragstests (TDD, RED zuerst beobachtet).

Bindet ``services/delivery_contract.py`` an die lokale API (Muster JFW-11
``routes/meeting.py``). Endpunktfunktionen werden direkt gegen eine geteilte
Datei-DB aufgerufen — ``starlette.testclient`` ist hier bewusst nicht im Spiel
(httpx-Altlast, siehe QA-Evidence JFW-11).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_router.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base
from backend.delivery.provenance import new_operation_id
from backend.routes import delivery as delivery_routes
from backend.transcription.handoff import build_handoff_payload
from backend.transcription.raw_transcript import text_hash

TEXT = "Rohtext fuer den Router."


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_delivery_api_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _handoff(**overrides) -> dict:
    base = dict(
        run_id="jfw6-run-" + "0" * 32,
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


def _body(operation_id=None, handoff=None, target_hash="t" * 64):
    return delivery_routes.DeliverySubmitRequest(
        delivery_operation_id=operation_id or new_operation_id(),
        handoff=handoff or _handoff(),
        target_snapshot={"target_snapshot_hash": target_hash},
    )


def test_submit_idempotent_und_konflikt_fail_closed():
    s = _session()
    op_id = new_operation_id()
    out = delivery_routes.submit_delivery(_body(operation_id=op_id), db=s)
    assert out["outcome"] == "created"
    out = delivery_routes.submit_delivery(_body(operation_id=op_id), db=s)
    assert out["outcome"] == "existing"
    changed = _body(
        operation_id=op_id,
        handoff=_handoff(raw_text="anderer Text", text_hash=text_hash("anderer Text")),
    )
    with pytest.raises(HTTPException) as exc:
        delivery_routes.submit_delivery(changed, db=s)
    assert exc.value.status_code == 409


def test_verfeinerte_payloads_sind_abgelehnt():
    s = _session()
    handoff = _handoff()
    handoff["payload_kind"] = "refined"
    with pytest.raises(HTTPException):
        delivery_routes.submit_delivery(_body(handoff=handoff), db=s)


def test_workflow_bis_outcome():
    s = _session()
    out = delivery_routes.submit_delivery(_body(), db=s)
    identity = out["identity_hash"]
    assert delivery_routes.persist_operation(identity, db=s)["status"] == "persisted"
    intent = delivery_routes.commit_attempt_intent(
        identity,
        delivery_routes.AttemptIntentRequest(
            attempt_intent={"adapter": "direct_text_adapter"}, app_epoch="epoch-1"
        ),
        db=s,
    )
    assert intent["status"] == "attempt_intent_committed"
    assert delivery_routes.mark_attempting(
        identity, delivery_routes.AttemptingRequest(attempt_id="try-1"), db=s
    )["status"] == "attempting"
    out = delivery_routes.commit_outcome(
        identity,
        delivery_routes.OutcomeRequest(outcome="succeeded"),
        db=s,
    )
    assert out["status"] == "succeeded"
    view = delivery_routes.get_operation(identity, db=s)
    assert view["status"] == "succeeded"
    assert view["auto_attempt_consumed"] is True


def test_outcome_abweichung_ist_sichtbar_zu_spaet():
    s = _session()
    out = delivery_routes.submit_delivery(_body(), db=s)
    identity = out["identity_hash"]
    delivery_routes.persist_operation(identity, db=s)
    delivery_routes.commit_attempt_intent(
        identity,
        delivery_routes.AttemptIntentRequest(attempt_intent={}, app_epoch="e"),
        db=s,
    )
    delivery_routes.mark_attempting(
        identity, delivery_routes.AttemptingRequest(attempt_id="try-1"), db=s
    )
    delivery_routes.commit_outcome(
        identity, delivery_routes.OutcomeRequest(outcome="unknown"), db=s
    )
    with pytest.raises(HTTPException) as exc:
        delivery_routes.commit_outcome(
            identity, delivery_routes.OutcomeRequest(outcome="succeeded"), db=s
        )
    assert exc.value.status_code == 409


def test_kopieren_zeigt_trust_boundary_und_ist_explizit():
    s = _session()
    out = delivery_routes.submit_delivery(_body(), db=s)
    identity = out["identity_hash"]
    res = delivery_routes.copy_text(
        identity, delivery_routes.CopyRequest(confirmed=False), db=s
    )
    assert res["executed"] is False
    assert res["trust_boundary"] == "clipboard_external"
    res = delivery_routes.copy_text(
        identity, delivery_routes.CopyRequest(confirmed=True), db=s
    )
    assert res["executed"] is True


def test_retry_erzeugt_kind_mit_neuem_ziel():
    s = _session()
    out = delivery_routes.submit_delivery(_body(), db=s)
    identity = out["identity_hash"]
    delivery_routes.persist_operation(identity, db=s)
    delivery_routes.commit_attempt_intent(
        identity,
        delivery_routes.AttemptIntentRequest(attempt_intent={}, app_epoch="e"),
        db=s,
    )
    delivery_routes.mark_attempting(
        identity, delivery_routes.AttemptingRequest(attempt_id="try-1"), db=s
    )
    delivery_routes.commit_outcome(
        identity, delivery_routes.OutcomeRequest(outcome="unknown"), db=s
    )
    retry = delivery_routes.retry_operation(
        identity,
        delivery_routes.RetryRequest(
            handoff=_handoff(), target_snapshot={"target_snapshot_hash": "u" * 64}
        ),
        db=s,
    )
    assert retry["outcome"] == "created"
    assert retry["identity_hash"] != identity
    parent = delivery_routes.get_operation(identity, db=s)
    assert parent["status"] == "unknown"


def test_delete_tombstone():
    s = _session()
    out = delivery_routes.submit_delivery(_body(), db=s)
    identity = out["identity_hash"]
    delivery_routes.set_manual(
        identity, delivery_routes.ManualRequest(reason_code="kein_adapter"), db=s
    )
    res = delivery_routes.delete_recovery(identity, db=s)
    assert res["status"] == "deleted"
    view = delivery_routes.get_operation(identity, db=s)
    assert view["status"] == "deleted"
