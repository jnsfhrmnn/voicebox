"""JFW-8: Atomare Persistenz der Delivery-Operation — Vertragstests (TDD, RED).

Muster JFW-12 ``task_contract.py`` / JFW-6 ``recording_contract.py`` /
JFW-11 ``meeting_contract.py``: Idempotenz ueber ``payload_hash``, fail-closed
Provenienzkonflikt, Exactly-once-``attempt_intent`` VOR externer Eingabe,
genau ein terminaler Ausgang pro Operation (erster dauerhafter gewinnt),
Crash-Recovery stellt ``unknown`` statt Retry, Recovery-Loeschung mit
inhaltsfreiem Tombstone. Geteilte Datei-DB (kein ``sqlite://``).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_service.py
"""
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.database.models import Base, DeliveryOperation
from backend.delivery.provenance import DeliveryRequest, new_operation_id
from backend.services.delivery_contract import (
    commit_attempt_intent,
    commit_outcome,
    delete_recovery,
    mark_attempting,
    persist_operation,
    recover_interrupted,
    set_manual_only,
    submit_delivery,
)
from backend.transcription.raw_transcript import text_hash

TEXT = "Rohtext fuer die Uebergabe."


def _session():
    db = Path(tempfile.mkdtemp(prefix="jfw_delivery_")) / "t.db"
    engine = create_engine(
        f"sqlite:///{db}",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


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


def test_submit_created_existing_conflict():
    s = _session()
    req = _request()
    assert submit_delivery(s, req)["outcome"] == "created"
    assert submit_delivery(s, req)["outcome"] == "existing"
    # Dieselbe Operations-ID, abweichender Text: fail-closed Konflikt.
    other = _request(
        delivery_operation_id=req.delivery_operation_id,
        raw_text="anderer Text",
        text_hash=text_hash("anderer Text"),
    )
    assert submit_delivery(s, other)["outcome"] == "conflict"


def test_identische_zustellung_startet_niemals_zweiten_versuch():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    assert commit_attempt_intent(s, req.identity_hash(), {"adapter": "direct_text_adapter"}, "epoch-1") == "attempt_intent_committed"
    # Identische erneute Zustellung: derselbe Zustand, kein zweiter Versuch.
    out = submit_delivery(s, req)
    assert out["outcome"] == "existing"
    assert out["status"] == "attempt_intent_committed"
    assert commit_attempt_intent(s, req.identity_hash(), {"adapter": "direct_text_adapter"}, "epoch-1") is None


def test_operation_rohtext_und_intent_sind_vor_externer_eingabe_gespeichert():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash(), recovery_expires_at=datetime(2026, 10, 1))
    assert commit_attempt_intent(s, req.identity_hash(), {"adapter": "verified_paste_adapter"}, "epoch-1") == "attempt_intent_committed"
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.status == "attempt_intent_committed"
    assert row.auto_attempt_consumed is True
    assert row.recovery_text == TEXT
    assert row.attempt_intent == {"adapter": "verified_paste_adapter"}


def test_genau_ein_terminaler_ausgang_pro_operation():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1")
    mark_attempting(s, req.identity_hash(), "try-1")
    assert commit_outcome(s, req.identity_hash(), "succeeded") == "committed"
    # Der spaetere Ausgang ist sichtbar zu spaet — nie ueberschrieben.
    assert commit_outcome(s, req.identity_hash(), "failed") == "already_terminal"
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.status == "succeeded"


def test_unknown_verbraucht_budget_ohne_auto_retry():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1")
    mark_attempting(s, req.identity_hash(), "try-1")
    assert commit_outcome(s, req.identity_hash(), "unknown") == "committed"
    # Budget bleibt verbraucht: kein zweiter automatischer Versuch.
    assert commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1") is None
    assert mark_attempting(s, req.identity_hash(), "try-2") is False


def test_manual_only_ohne_automatischen_versuch():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    assert set_manual_only(s, req.identity_hash(), "kein_sicherer_adapter") is True
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.status == "manual_only"
    assert row.auto_attempt_consumed is False
    assert commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1") is None


def test_retry_ist_append_only_mit_eigenem_budget():
    s = _session()
    parent = _request()
    submit_delivery(s, parent)
    persist_operation(s, parent.identity_hash())
    commit_attempt_intent(s, parent.identity_hash(), {}, "epoch-1")
    mark_attempting(s, parent.identity_hash(), "try-1")
    commit_outcome(s, parent.identity_hash(), "unknown")
    # Kindoperation: eigene ID, eigener Ziel-Snapshot, eigenes Budget.
    child = _request(
        parent_operation_id=parent.delivery_operation_id,
        target_snapshot={"target_snapshot_hash": "u" * 64},
    )
    assert submit_delivery(s, child)["outcome"] == "created"
    assert commit_attempt_intent(s, child.identity_hash(), {}, "epoch-1") == "attempt_intent_committed"
    # Die alte Operation bleibt unangetastet (Budget verbraucht, Status unknown).
    row = s.query(DeliveryOperation).filter_by(identity_hash=parent.identity_hash()).one()
    assert row.status == "unknown"
    assert row.auto_attempt_consumed is True


def test_recover_interrupted_stellt_unknown_statt_retry():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    commit_attempt_intent(s, req.identity_hash(), {}, "epoch-alt")
    mark_attempting(s, req.identity_hash(), "try-1")
    # Crash zwischen externer Wirkung und Ergebniscommit: Ausgang unbekannt.
    assert recover_interrupted(s, "epoch-neu") == 1
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.status == "unknown"
    assert row.auto_attempt_consumed is True
    assert commit_attempt_intent(s, req.identity_hash(), {}, "epoch-neu") is None


def test_delete_recovery_gemeinsame_loeschung_mit_tombstone():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1")
    mark_attempting(s, req.identity_hash(), "try-1")
    commit_outcome(s, req.identity_hash(), "failed")
    assert delete_recovery(s, req.identity_hash()) == "deleted"
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.status == "deleted"
    assert row.recovery_text is None
    # Inhaltsfreier Tombstone sichert weiterhin die Idempotenz.
    assert row.text_hash == text_hash(TEXT)
    assert delete_recovery(s, req.identity_hash()) == "deleted"


def test_loeschung_nicht_aktiver_operationen_blockiert():
    s = _session()
    req = _request()
    submit_delivery(s, req)
    persist_operation(s, req.identity_hash())
    commit_attempt_intent(s, req.identity_hash(), {}, "epoch-1")
    mark_attempting(s, req.identity_hash(), "try-1")
    # Aktive Operation darf nicht geloescht werden.
    assert delete_recovery(s, req.identity_hash()) == "not_deletable"
    row = s.query(DeliveryOperation).filter_by(identity_hash=req.identity_hash()).one()
    assert row.recovery_text == TEXT
