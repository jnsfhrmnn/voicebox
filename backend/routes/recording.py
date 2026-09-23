"""JFW-6: Recording-Endpunkte (lokale API, Diktat-Profil).

Bindet den Aufnahme-Vertragskern an die HTTP-Ebene (Muster JFW-2/JFW-3/JFW-11:
``routes/alignment.py``/``routes/diarization.py``/``routes/meeting.py``).
Genau ein autoritativer Diktat-Run je Benutzerinstanz; die browserbasierte
``MediaRecorder``-Aufnahme bleibt die einzige Aufnahmeautorität, dieser Router
haertet Run-Identität, Exactly-once Start/Stopp, Formatbindung, Sound-Cues,
Recovery-Grenzen und den idempotenten JFW-7-Handoff.

Dieser Router ist bewusst I/O-frei und endet am atomaren Vertrags-Commit
(``services/recording_contract.py``). Die Timeslice-Journal-Verdrahtung an die
MediaRecorder-Bloecke ist der Folge-Block.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..database.models import RecordingRun
from ..recording.manifest import RUN_CONTRACT_VERSION
from ..recording.provenance import RecordingRequest
from ..services import recording_contract as store

router = APIRouter()


class RecordingSubmitRequest(BaseModel):
    run_id: str
    device_stable_id_hash: str
    format: dict
    started_at_100ns: int = 0
    contract_version: str = RUN_CONTRACT_VERSION


class RecordingBeginRequest(BaseModel):
    stream_open: bool = True
    run_persisted: bool = True
    first_block_accepted: bool = True
    t_100ns: int = 0
    app_epoch: str = "api"


class RecordingStopRequest(BaseModel):
    cause: str
    t_100ns: int


class RecordingCommitRequest(BaseModel):
    stop_reason: str
    audio_hash: str
    manifest: dict
    frames: dict = Field(default_factory=dict)
    gaps: list[dict] = Field(default_factory=list)
    sound_cue_marks: list[dict] = Field(default_factory=list)
    recovery_status: dict = Field(default_factory=dict)


class RecordingCancelRequest(BaseModel):
    confirmed: bool = False
    deletion_contract_hash: str | None = None


class RecordingHandoffRequest(BaseModel):
    audio_hash: str
    manifest_hash: str


def _to_request(body: RecordingSubmitRequest) -> RecordingRequest:
    return RecordingRequest(
        run_id=body.run_id,
        device_stable_id_hash=body.device_stable_id_hash,
        format=dict(body.format),
        started_at_100ns=body.started_at_100ns,
        contract_version=body.contract_version,
    )


def _result_summary(row) -> dict:
    return {
        "identity_hash": row.identity_hash,
        "payload_hash": row.payload_hash,
        "run_id": row.run_id,
        "status": row.status,
        "result_hash": row.result_hash,
        "reason_code": row.reason_code,
        "stop_cause": row.stop_cause,
        "stop_reason": row.stop_reason,
        "stop_at_100ns": row.stop_at_100ns,
        "audio_hash": row.audio_hash,
        "manifest_hash": row.manifest_hash,
        "format": row.format,
        "frames": row.frames,
        "gaps": row.gaps,
        "sound_cue_marks": row.sound_cue_marks,
        "recovery_status": row.recovery_status,
        "transcription_authorized": row.transcription_authorized,
        "handoff_count": row.handoff_count,
        "deletion_contract_hash": row.deletion_contract_hash,
        "contract_version": row.contract_version,
    }


@router.post("/recording/submit")
def submit_recording(body: RecordingSubmitRequest, db: Session = Depends(get_db)):
    """Legt den Run an (idempotent ueber ``payload_hash``); abweichender Payload
    bei gleicher Run-Identität ist fail-closed ein Konflikt — Auto-Repeat oder
    nahezu gleichzeitiges Mehrfachfeuer erzeugt nie einen zweiten Run."""
    out = store.submit_run(db, _to_request(body))
    if out["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="conflict")
    return out


@router.post("/recording/{identity_hash}/begin")
def begin_recording(
    identity_hash: str, body: RecordingBeginRequest, db: Session = Depends(get_db)
):
    """``starting → recording`` erst wenn Stream offen, Run persistierbar und
    erster angenommener Audioblock bestätigt sind — sonst bleibt die Pill
    „Startet" und es wird keine aktive Aufnahme behauptet."""
    if not (body.stream_open and body.run_persisted and body.first_block_accepted):
        raise HTTPException(status_code=409, detail="start_unvollstaendig")
    attempt_id = store.begin_recording(db, identity_hash, body.app_epoch)
    if attempt_id is None:
        raise HTTPException(status_code=409, detail="not_startable")
    return {
        "identity_hash": identity_hash,
        "attempt_id": attempt_id,
        "status": "recording",
    }


@router.post("/recording/{identity_hash}/stop")
def stop_recording(
    identity_hash: str, body: RecordingStopRequest, db: Session = Depends(get_db)
):
    """Genau ein angenommener Stopp-Intent mit Ursache und monotonem Punkt.
    Auto-Repeat meldet sichtbar ``already_stopped`` und erzeugt keinen zweiten
    Abschluss."""
    outcome = store.accept_stop(db, identity_hash, body.cause, body.t_100ns)
    if outcome in ("accepted", "already_stopped"):
        return {"identity_hash": identity_hash, "outcome": outcome}
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    raise HTTPException(status_code=409, detail=outcome)


@router.post("/recording/{identity_hash}/commit")
def commit_recording(
    identity_hash: str, body: RecordingCommitRequest, db: Session = Depends(get_db)
):
    """Atomarer Ergebnis-Commit (``secured``). Frame-Bilanz und Manifest sind
    fail-closed; der Verwerfen-/Fehler-Race ist sichtbar (409) statt
    still zu ueberschreiben."""
    built = body.model_dump() if hasattr(body, "model_dump") else body.dict()
    outcome = store.commit_result(db, identity_hash, built)
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    if outcome != "committed":
        raise HTTPException(status_code=409, detail=outcome)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.post("/recording/{identity_hash}/cancel")
def cancel_recording(
    identity_hash: str, body: RecordingCancelRequest, db: Session = Depends(get_db)
):
    """Ausdrueckliches „Verwerfen": nur mit Bestätigung und gebundenem
    Löschvertrag; kein Transkriptions-Handoff. Nach committetem Ergebnis
    sichtbar ``too_late`` (Race fail-closed)."""
    outcome = store.discard_run(
        db,
        identity_hash,
        confirmed=body.confirmed,
        deletion_contract_hash=body.deletion_contract_hash,
    )
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.post("/recording/{identity_hash}/handoff")
def deliver_run_handoff(
    identity_hash: str, body: RecordingHandoffRequest, db: Session = Depends(get_db)
):
    """Idempotenter JFW-7-Handoff: identische erneute Zustellung setzt den-
    selben Run fort (``existing``) und autorisiert keine zweite Transkription;
    abweichender Payload ist fail-closed ``conflict``."""
    outcome = store.deliver_handoff(
        db,
        identity_hash,
        audio_hash=body.audio_hash,
        manifest_hash=body.manifest_hash,
    )
    if outcome in ("delivered", "existing"):
        return {"identity_hash": identity_hash, "outcome": outcome}
    if outcome == "not_found":
        raise HTTPException(status_code=404, detail="not_found")
    raise HTTPException(status_code=409, detail=outcome)


@router.get("/recording/{identity_hash}")
def get_recording(identity_hash: str, db: Session = Depends(get_db)):
    """Status, Frame-Bilanz, Luecken, Sound-Cue-Marken, Recovery-Status und
    Handoff-Zustand des Aufnahme-Runs (inhaltsfrei)."""
    row = db.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    return _result_summary(row)
