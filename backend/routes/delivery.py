"""JFW-8: Delivery-Endpunkte (lokale API, Transkriptions-Profil).

Bindet den Zieluebergabe-Vertragskern an die HTTP-Ebene (Muster JFW-2/JFW-3/
JFW-11: ``routes/alignment.py``/``routes/meeting.py``). Annahme nur ueber ein
geprueftes JFW-7-Handoff-Payload (``dictation_handoff_v1``, Reuse); verbotene
Textmodi ``refined|formatted|rewritten|bypassed|raw_fallback`` sind fail-closed
abgelehnt (JFW-9-Riegel). Dieser Router endet am atomaren Vertrags-Commit
(``services/delivery_contract.py``); die Windows-Zieladapter (Win32/UIA/
Clipboard/Scroll-Lock) bleiben Zielsystem-Seams und sind hier nicht I/O-aktiv.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..delivery.contract import new_retry_operation
from ..delivery.payload import DeliveryVertragError, build_delivery_payload
from ..delivery.provenance import DeliveryRequest, new_operation_id
from ..delivery.recovery import manual_copy_intent
from ..services import delivery_contract as store

router = APIRouter()

class DeliverySubmitRequest(BaseModel):
    delivery_operation_id: str
    handoff: dict
    target_snapshot: dict = Field(default_factory=dict)
    parent_operation_id: str | None = None

class AttemptIntentRequest(BaseModel):
    attempt_intent: dict = Field(default_factory=dict)
    app_epoch: str = "api"

class AttemptingRequest(BaseModel):
    attempt_id: str

class OutcomeRequest(BaseModel):
    outcome: str
    reason_code: str | None = None
    error_trace: dict | None = None

class ManualRequest(BaseModel):
    reason_code: str = "manual_only"

class RetryRequest(BaseModel):
    handoff: dict
    target_snapshot: dict = Field(default_factory=dict)

class CopyRequest(BaseModel):
    confirmed: bool = False

def _to_request(body: DeliverySubmitRequest) -> DeliveryRequest:
    handoff = body.handoff or {}
    return DeliveryRequest(
        delivery_operation_id=body.delivery_operation_id,
        run_id=handoff.get("run_id") or "",
        audio_hash=handoff.get("audio_hash") or "",
        attempt_id=handoff.get("attempt_id") or "",
        revision_id=handoff.get("revision_id") or "",
        raw_text=handoff.get("raw_text") or "",
        text_hash=handoff.get("text_hash") or "",
        target_snapshot=dict(body.target_snapshot or {}),
        target_confirmed=bool(handoff.get("target_confirmed")),
        parent_operation_id=body.parent_operation_id,
    )

@router.post("/delivery/submit")
def submit_delivery(body: DeliverySubmitRequest, db: Session = Depends(get_db)):
    """Legt die Delivery-Operation an (idempotent ueber ``payload_hash``);
    abweichender Text/Hash/Ziel bei gleicher Operations-ID ist fail-closed
    ein 409-Konflikt — dann wird nichts geschrieben."""
    try:
        build_delivery_payload(
            delivery_operation_id=body.delivery_operation_id,
            handoff=body.handoff,
            target_snapshot=body.target_snapshot,
        )
    except DeliveryVertragError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    out = store.submit_delivery(db, _to_request(body))
    if out["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="conflict")
    return out

@router.post("/delivery/{identity_hash}/persist")
def persist_operation(identity_hash: str, db: Session = Depends(get_db)):
    """Bindet Operation und Recovery-Rohtext dauerhaft (``persisted``)."""
    return store.persist_operation(db, identity_hash)

@router.post("/delivery/{identity_hash}/attempt-intent")
def commit_attempt_intent(
    identity_hash: str, body: AttemptIntentRequest, db: Session = Depends(get_db)
):
    """Verbraucht das Einmalbudget genau einmal — VOR externer Eingabe."""
    outcome = store.commit_attempt_intent(db, identity_hash, body.attempt_intent, body.app_epoch)
    if outcome is None:
        raise HTTPException(status_code=409, detail="not_committable")
    return {"identity_hash": identity_hash, "status": outcome}

@router.post("/delivery/{identity_hash}/attempting")
def mark_attempting(
    identity_hash: str, body: AttemptingRequest, db: Session = Depends(get_db)
):
    """Zieladapter laeuft — nur aus dem autorisierten Intent-Zustand."""
    if not store.mark_attempting(db, identity_hash, body.attempt_id):
        raise HTTPException(status_code=409, detail="not_attemptable")
    return {"identity_hash": identity_hash, "status": "attempting"}

@router.post("/delivery/{identity_hash}/outcome")
def commit_outcome(
    identity_hash: str, body: OutcomeRequest, db: Session = Depends(get_db)
):
    """Atomarer terminaler Ausgang (``succeeded``/``failed``/``unknown``);
    ein spaeterer Ausgang ist sichtbar zu spaet (Race fail-closed)."""
    outcome = store.commit_outcome(
        db, identity_hash, body.outcome, body.reason_code, body.error_trace
    )
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    if outcome != "committed":
        raise HTTPException(status_code=409, detail=outcome)
    return {"identity_hash": identity_hash, "status": body.outcome}

@router.post("/delivery/{identity_hash}/manual")
def set_manual(identity_hash: str, body: ManualRequest, db: Session = Depends(get_db)):
    """Kein sicherer Auto-Adapter/Ziel: ``manual_only`` ohne automatischen Versuch."""
    if not store.set_manual_only(db, identity_hash, body.reason_code):
        raise HTTPException(status_code=409, detail="not_manual")
    return {"identity_hash": identity_hash, "status": "manual_only"}

@router.post("/delivery/{identity_hash}/retry")
def retry_operation(
    identity_hash: str, body: RetryRequest, db: Session = Depends(get_db)
):
    """``Erneut einfügen``: append-only Kindoperation mit neuem Ziel-Snapshot
    und eigenem Einmalbudget; die alte Operation wird nie zurueckgesetzt."""
    parent = store.get_operation(db, identity_hash)
    if parent is None:
        raise HTTPException(status_code=404, detail="not_found")
    if new_retry_operation(parent_state=parent["status"]) is None:
        raise HTTPException(status_code=409, detail="operation_active")
    try:
        build_delivery_payload(
            delivery_operation_id=new_operation_id(),
            handoff=body.handoff,
            target_snapshot=body.target_snapshot,
        )
    except DeliveryVertragError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    child = DeliverySubmitRequest(
        delivery_operation_id=new_operation_id(),
        handoff=body.handoff,
        target_snapshot=body.target_snapshot,
        parent_operation_id=parent["delivery_operation_id"],
    )
    out = store.submit_delivery(db, _to_request(child))
    return out

@router.post("/delivery/{identity_hash}/copy")
def copy_text(identity_hash: str, body: CopyRequest, db: Session = Depends(get_db)):
    """``Manuell kopieren``: explizite Aktion; die Zwischenablage wird als
    externe Trust Boundary angezeigt und nur ausdruecklich ausgefuehrt."""
    if store.get_operation(db, identity_hash) is None:
        raise HTTPException(status_code=404, detail="not_found")
    return manual_copy_intent(confirmed=body.confirmed)

@router.post("/delivery/{identity_hash}/delete")
def delete_recovery(identity_hash: str, db: Session = Depends(get_db)):
    """Loescht Recovery-Text und sensible Zwi­schenstaende gemeinsam; der
    verbleibende Tombstone ist inhaltsfrei und sichert die Idempotenz."""
    outcome = store.delete_recovery(db, identity_hash)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    if outcome != "deleted":
        raise HTTPException(status_code=409, detail=outcome)
    return {"identity_hash": identity_hash, "status": "deleted"}

@router.get("/delivery/{identity_hash}")
def get_operation(identity_hash: str, db: Session = Depends(get_db)):
    """Zustandsansicht: Status, Budget, Zielbindung, Fehlerpfad-Spur — nie der
    Recovery-Rohtext."""
    summary = store.get_operation(db, identity_hash)
    if summary is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    return summary
