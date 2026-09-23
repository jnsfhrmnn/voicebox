"""JFW-11: Meeting-Endpunkte (lokale API, Datei-/Meeting-Profil).

Bindet den Dual-Source-Vertragskern an die HTTP-Ebene (Muster JFW-2/JFW-3:
``routes/alignment.py``/``routes/diarization.py``). Ausdruecklich gestartete
Meeting-Laeufe mit genau einem Mikrofon und genau einer Remote-Audioquelle.
Der Diktatpfad ruft nichts davon auf. JFW-11 ergaenzt ausschliesslich
Quellen-, Deduplizierungs- und Namenszuordnungen — nie Text, Wortgrenzen,
Cluster-IDs oder Overlap-/Unsicherheitsstatus (heiliger
Unveraenderlichkeits-Vertrag, fail-closed).

Capture-Helfer-I/O (``jf-whisper-capture``) bleibt Folge-Block; dieser Router
ist bewusst I/O-frei und endet am atomaren Vertrags-Commit
(``services/meeting_contract.py``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..database.models import MeetingResult
from ..meeting.contract import RESULT_CONTRACT_VERSION
from ..meeting.provenance import MeetingRequest
from ..services import meeting_contract as store

router = APIRouter()


class MeetingSubmitRequest(BaseModel):
    job_id: str
    meeting_run_id: str
    jfw6_run_reference: str
    track_hashes: list[str] = Field(default_factory=list)
    manifest_hash: str
    jfw2_result_hash: str | None = None
    jfw3_result_hash: str | None = None
    contract_version: str = RESULT_CONTRACT_VERSION


class MeetingBeginRequest(BaseModel):
    app_epoch: str = "api"


class MeetingCommitRequest(BaseModel):
    """Ergebnis-Commit der Annotationsebene (inhaltsfrei, revisionsgebunden)."""

    status: str
    reason_code: str | None = None
    stop_reason: str | None = None
    tracks: list[dict] = Field(default_factory=list)
    gaps: list[dict] = Field(default_factory=list)
    sync: dict = Field(default_factory=dict)
    sound_cue_marks: list[dict] = Field(default_factory=list)
    recovery_status: dict = Field(default_factory=dict)
    dedupe: list[dict] = Field(default_factory=list)
    name_mappings: list[dict] = Field(default_factory=list)
    result_hash: str


def _to_request(body: MeetingSubmitRequest) -> MeetingRequest:
    return MeetingRequest(
        job_id=body.job_id,
        meeting_run_id=body.meeting_run_id,
        jfw6_run_reference=body.jfw6_run_reference,
        track_hashes=tuple(body.track_hashes),
        manifest_hash=body.manifest_hash,
        jfw2_result_hash=body.jfw2_result_hash,
        jfw3_result_hash=body.jfw3_result_hash,
        contract_version=body.contract_version,
    )


def _result_summary(row) -> dict:
    return {
        "identity_hash": row.identity_hash,
        "payload_hash": row.payload_hash,
        "status": row.status,
        "result_hash": row.result_hash,
        "reason_code": row.reason_code,
        "stop_reason": row.stop_reason,
        "job_id": row.job_id,
        "meeting_run_id": row.meeting_run_id,
        "jfw6_run_reference": row.jfw6_run_reference,
        "manifest_hash": row.manifest_hash,
        "jfw2_result_hash": row.jfw2_result_hash,
        "jfw3_result_hash": row.jfw3_result_hash,
        "tracks": row.tracks,
        "gaps": row.gaps,
        "sync": row.sync,
        "sound_cue_marks": row.sound_cue_marks,
        "recovery_status": row.recovery_status,
        "dedupe": row.dedupe,
        "name_mappings": row.name_mappings,
        "contract_version": row.contract_version,
    }


@router.post("/meeting/submit")
def submit_meeting(body: MeetingSubmitRequest, db: Session = Depends(get_db)):
    """Legt den Meeting-Job an (idempotent ueber ``payload_hash``); abweichender
    Payload bei gleicher Manifest-Identitaet ist fail-closed ein Konflikt."""
    out = store.submit_meeting(db, _to_request(body))
    if out["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="conflict")
    return out


@router.post("/meeting/{identity_hash}/begin")
def begin_meeting(
    identity_hash: str, body: MeetingBeginRequest, db: Session = Depends(get_db)
):
    """Startet den Capture-Attempt (``capturing``) — kein Doppellauf, kein Start
    ueber bereits committetem Ergebnis; die Capture-Helfer-I/O bleibt Folge-Block."""
    attempt_id = store.begin_attempt(db, identity_hash, body.app_epoch)
    if attempt_id is None:
        raise HTTPException(status_code=409, detail="not_startable")
    return {"identity_hash": identity_hash, "attempt_id": attempt_id}


@router.post("/meeting/{identity_hash}/commit")
def commit_meeting(
    identity_hash: str, body: MeetingCommitRequest, db: Session = Depends(get_db)
):
    """Atomarer Ergebnis-Commit (``secured_dual``/``secured_partial``). Der
    Cancel/Commit-Race ist sichtbar: der Verlierer erhaelt 409 statt zu
    ueberschreiben."""
    built = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    outcome = store.commit_result(db, identity_hash, built)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    if outcome != "committed":
        raise HTTPException(status_code=409, detail=outcome)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.post("/meeting/{identity_hash}/cancel")
def cancel_meeting(identity_hash: str, db: Session = Depends(get_db)):
    """Bestaetigter Nutzerabbruch: nur der Attempt endet ``canceled``; nach
    committetem Ergebnis sichtbar „too_late" (Race fail-closed)."""
    outcome = store.cancel_meeting(db, identity_hash)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.get("/meeting/{identity_hash}")
def get_meeting(identity_hash: str, db: Session = Depends(get_db)):
    """Status, Tracks, Lücken, Sync-Ableitung, Recovery-Status sowie
    Deduplizierungs- und Namensannotationen des Meeting-Jobs."""
    row = (
        db.query(MeetingResult)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    return _result_summary(row)
