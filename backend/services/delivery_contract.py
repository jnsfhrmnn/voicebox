"""JFW-8: Atomare Persistenz der Delivery-Operation (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``recording_contract.py``/
``meeting_contract.py``: genau ein terminaler Ausgang pro Operation ueber
bedingte DB-Transaktionen.

* ``submit_delivery``: Idempotenz ueber ``payload_hash`` (``existing``),
  fail-closed Provenienzkonflikt bei abweichendem Payload bei gleicher
  Identitaet (``conflict``) — dann wird nichts geschrieben.
* ``persist_operation``: bindet Operation und Recovery dauerhaft (``persisted``).
* ``commit_attempt_intent``: verbraucht das Einmalbudget GENAU EINMAL und zwar
  VOR der externen Eingabe (``attempt_intent_committed``).
* ``commit_outcome``: genau ein terminaler Ausgang pro Operation; der zuerst
  dauerhaft gespeicherte gewinnt (Commit/Cancel-Race sichtbar).
* ``recover_interrupted``: nach Crash zwischen externer Wirkung und
  Ergebniscommit wird ``unknown`` gesetzt — NIE Retry.
* ``delete_recovery``: Text und sensible Zwi­schenstaende werden gemeinsam
  geloescht; der Tombstone bleibt inhaltsfrei und idempotent.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..database.models import DeliveryOperation
from ..delivery.provenance import DeliveryRequest

#: Nichtterminale Zustaende des Delivery State Contract.
NON_TERMINAL_STATES = ("received", "persisted", "attempt_intent_committed", "attempting")
#: Zustaende, in denen ein Versuch laeuft (extern moeglicherweise wirkend).
ATTEMPT_STATES = ("attempt_intent_committed", "attempting")
#: Zulaessige terminale Ergebnis-Ausgaenge eines Versuchs.
COMMITTABLE_OUTCOMES = ("succeeded", "failed", "unknown")
#: Ruhezustaende, aus denen die Recovery-Loeschung zulaessig ist.
DELETABLE_STATES = ("succeeded", "failed", "unknown", "manual_only")

CommitOutcome = Literal["committed", "already_terminal", "not_found"]

def _now() -> datetime:
    return datetime.utcnow()

def submit_delivery(session: Session, request: DeliveryRequest) -> dict:
    """Legt die Delivery-Operation an oder liefert den Stand idempotent."""
    identity_hash = request.identity_hash()
    row = (
        session.query(DeliveryOperation)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is not None:
        same = row.payload_hash == request.payload_hash() and row.text_hash == request.text_hash
        outcome = "existing" if same else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
        }

    row = DeliveryOperation(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=request.payload_hash(),
        delivery_operation_id=request.delivery_operation_id,
        parent_operation_id=request.parent_operation_id,
        contract_version=request.contract_version,
        run_id=request.run_id,
        audio_hash=request.audio_hash,
        jfw7_attempt_id=request.attempt_id,
        revision_id=request.revision_id,
        text_hash=request.text_hash,
        target_snapshot=request.target_snapshot,
        target_confirmed=bool(request.target_confirmed),
        status="received",
        recovery_text=request.raw_text,
    )
    session.add(row)
    session.commit()
    return {"outcome": "created", "identity_hash": identity_hash, "status": "received"}

def persist_operation(
    session: Session, identity_hash: str, recovery_expires_at: datetime | None = None
) -> dict:
    """Bindet Operation und Recovery dauerhaft (``persisted``)."""
    session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status == "received",
        )
        .values(
            status="persisted",
            recovery_expires_at=recovery_expires_at,
            updated_at=_now(),
        )
    )
    session.commit()
    return get_operation(session, identity_hash) or {"identity_hash": identity_hash, "status": "not_found"}

def commit_attempt_intent(
    session: Session, identity_hash: str, attempt_intent: dict, app_epoch: str
) -> str | None:
    """Verbraucht das Einmalbudget genau einmal — VOR externer Eingabe."""
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status.in_(("received", "persisted")),
            DeliveryOperation.auto_attempt_consumed.is_(False),
        )
        .values(
            status="attempt_intent_committed",
            auto_attempt_consumed=True,
            attempt_intent=dict(attempt_intent or {}),
            app_epoch=app_epoch,
            updated_at=_now(),
        )
    )
    session.commit()
    return "attempt_intent_committed" if (result.rowcount or 0) > 0 else None

def mark_attempting(session: Session, identity_hash: str, attempt_id: str) -> bool:
    """Zieladapter laeuft — nur aus dem autorisierten Intent-Zustand."""
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status == "attempt_intent_committed",
        )
        .values(status="attempting", attempt_id=attempt_id, updated_at=_now())
    )
    session.commit()
    return (result.rowcount or 0) > 0

def commit_outcome(
    session: Session,
    identity_hash: str,
    outcome: str,
    reason_code: str | None = None,
    error_trace: dict | None = None,
) -> CommitOutcome:
    """Atomarer terminaler Ausgang — genau einer pro Operation."""
    if outcome not in COMMITTABLE_OUTCOMES:
        raise ValueError(f"ausgang_unzulaessig:{outcome!r}")
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status.in_(ATTEMPT_STATES),
        )
        .values(
            status=outcome,
            reason_code=reason_code,
            error_trace=dict(error_trace) if error_trace else None,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = (
        session.query(DeliveryOperation)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    return "not_found" if row is None else "already_terminal"

def set_manual_only(session: Session, identity_hash: str, reason_code: str) -> bool:
    """Kein sicherer Auto-Adapter/Ziel: ohne automatischen Versuch manuell."""
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status.in_(("received", "persisted")),
            DeliveryOperation.auto_attempt_consumed.is_(False),
        )
        .values(
            status="manual_only",
            reason_code=reason_code,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    return (result.rowcount or 0) > 0

def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: moeglicherweise wirkende Versuche enden ``unknown`` —
    nie Retry, nie ein erfundener Ausgang."""
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.status.in_(ATTEMPT_STATES),
            DeliveryOperation.app_epoch != current_epoch,
        )
        .values(status="unknown", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0

def delete_recovery(session: Session, identity_hash: str) -> str:
    """Gemeinsame Loeschung von Text und Zwi­schenstaenden; der Tombstone
    bleibt inhaltsfrei und sichert die Idempotenz. Aktive Operationen sind
    geschuetzt (``not_deletable``)."""
    result = session.execute(
        update(DeliveryOperation)
        .where(
            DeliveryOperation.identity_hash == identity_hash,
            DeliveryOperation.status.in_(DELETABLE_STATES),
        )
        .values(status="deleted", recovery_text=None, updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "deleted"
    row = (
        session.query(DeliveryOperation)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None:
        return "not_found"
    return "deleted" if row.status == "deleted" else "not_deletable"

def get_operation(session: Session, identity_hash: str) -> dict | None:
    """Inhaltsfreie Zustandsansicht der Operation (nie der Recovery-Rohtext)."""
    row = (
        session.query(DeliveryOperation)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None:
        return None
    return {
        "identity_hash": row.identity_hash,
        "payload_hash": row.payload_hash,
        "delivery_operation_id": row.delivery_operation_id,
        "parent_operation_id": row.parent_operation_id,
        "run_id": row.run_id,
        "revision_id": row.revision_id,
        "text_hash": row.text_hash,
        "target_confirmed": bool(row.target_confirmed),
        "capability": row.capability,
        "status": row.status,
        "auto_attempt_consumed": bool(row.auto_attempt_consumed),
        "attempt_id": row.attempt_id,
        "reason_code": row.reason_code,
        "error_trace": row.error_trace,
        "contract_version": row.contract_version,
    }
