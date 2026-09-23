"""JFW-7: Dictation-Endpunkte (lokale API, Diktat-Profil).

Bindet den Transkriptions-Vertragskern an die HTTP-Ebene (Muster JFW-2/JFW-3/
JFW-6/JFW-11: ``routes/alignment.py``/``routes/recording.py``). Genau ein
unverändertes Rohtranskript — das JFW-9-Verbot ist Vertragsbestandteil: KEIN
Text-LLM, KEIN Refinement am Rohtranskript. Dieser Router endet am atomaren
Vertrags-Commit (``services/transcription_contract.py``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import transcription_contract as store
from ..transcription.handoff import (
    HandoffVertragError,
    assert_payload_kind,
    build_handoff_payload,
    validate_handoff_payload,
)
from ..transcription.provenance import SOURCE_KINDS, TranscriptionRequest
from ..transcription.snapshot import MODEL_PROFILE_ID, RESULT_CONTRACT_VERSION

router = APIRouter()


class DictationSubmitRequest(BaseModel):
    run_id: str
    source_kind: str = "jfw6_handoff"
    audio_hash: str
    manifest_hash: str | None = None
    capture_id: str | None = None
    snapshot: dict
    stop_reason: str | None = None
    contract_version: str = RESULT_CONTRACT_VERSION


class AttemptRequest(BaseModel):
    backend_variant: str = "cpu"
    backend_generation: int = 1
    model_ready: bool = True
    backend_stable: bool = True
    app_epoch: str = "api"


class DictationCommitRequest(BaseModel):
    text: str
    segments: list[dict] = Field(default_factory=list)
    language_output: dict
    model_provenance: dict = Field(default_factory=dict)
    duration_ms: int | None = None


class NoSpeechRequest(BaseModel):
    language_output: dict = Field(default_factory=dict)
    model_provenance: dict = Field(default_factory=dict)


class UserEditRequest(BaseModel):
    text: str


class DictationCancelRequest(BaseModel):
    reason_code: str = "nutzerabbruch"


@router.post("/dictation/submit")
def submit_dictation(body: DictationSubmitRequest, db: Session = Depends(get_db)):
    """Annahme (JFW-6-Handoff/Upload/Retranskription) mit eingefrorenem Snapshot.

    Idempotent ueber ``payload_hash``; abweichender Payload bei gleicher Identität
    ist fail-closed ``conflict`` — keine zweite autoritative Revision.
    """
    if body.source_kind not in SOURCE_KINDS:
        raise HTTPException(status_code=422, detail="quelle_unbekannt")
    try:
        request = TranscriptionRequest(
            run_id=body.run_id,
            source_kind=body.source_kind,
            audio_hash=body.audio_hash,
            manifest_hash=body.manifest_hash,
            capture_id=body.capture_id,
            snapshot=dict(body.snapshot),
            contract_version=body.contract_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    out = store.submit_run(db, request)
    if out["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="conflict")
    if out["outcome"] == "snapshot_ungueltig":
        raise HTTPException(status_code=422, detail=out.get("errors"))
    if body.stop_reason:
        row = store.get_state(db, out["identity_hash"])
        if row is not None and not row.get("stop_reason"):
            from ..database.models import TranscriptionRun

            db.query(TranscriptionRun).filter_by(
                identity_hash=out["identity_hash"]
            ).update({"stop_reason": body.stop_reason})
            db.commit()
    return out


@router.post("/dictation/{identity_hash}/attempt")
def start_dictation_attempt(
    identity_hash: str, body: AttemptRequest, db: Session = Depends(get_db)
):
    """Bindet einen Attempt an Backendvariante + JFW-12-Generation.

    Unklare Health-/Generation-Identität → ``waiting_for_backend`` (kein stiller
    Fallback); fehlendes Modell → ``waiting_for_model`` (kein Netzwerkzugriff);
    notwendiger Backendwechsel → sichtbarer neuer Attempt.
    """
    out = store.start_attempt(
        db,
        identity_hash,
        backend_variant=body.backend_variant,
        backend_generation=body.backend_generation,
        model_ready=body.model_ready,
        backend_stable=body.backend_stable,
        app_epoch=body.app_epoch,
    )
    if out["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="unknown_identity")
    return out


@router.post("/dictation/{identity_hash}/commit")
def commit_dictation(
    identity_hash: str, body: DictationCommitRequest, db: Session = Depends(get_db)
):
    """Atomarer Rohtranskript-Commit: genau eine unveraenderte Revision.

    Kein lokales LLM, kein Refinement — der uebergebene Text wird unveraendert
    als ``raw_transcript`` gebunden. Leerer Text ohne Segmente wird ``no_speech``.
    """
    out = store.commit_raw(
        db,
        identity_hash,
        text=body.text,
        segments=body.segments,
        language_output=body.language_output,
        model_provenance=body.model_provenance,
        duration_ms=body.duration_ms,
    )
    if out["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="unknown_identity")
    return out


@router.post("/dictation/{identity_hash}/no-speech")
def no_speech_dictation(
    identity_hash: str, body: NoSpeechRequest, db: Session = Depends(get_db)
):
    """Terminal ``no_speech`` — kein leerer/halluzinierter Text, kein JFW-8-Payload."""
    out = store.record_no_speech(
        db,
        identity_hash,
        language_output=body.language_output,
        model_provenance=body.model_provenance,
    )
    if out["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="unknown_identity")
    return out


@router.post("/dictation/{identity_hash}/cancel")
def cancel_dictation(
    identity_hash: str, body: DictationCancelRequest, db: Session = Depends(get_db)
):
    """Terminaler Abbruch; nach Commitempfaengt kein spaeteres Ergebnis Autorität."""
    outcome = store.cancel_run(db, identity_hash, reason_code=body.reason_code)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.post("/dictation/{identity_hash}/edit")
def edit_dictation(
    identity_hash: str, body: UserEditRequest, db: Session = Depends(get_db)
):
    """Manuelle Änderung als getrennte ``user_edited``-Kindrevision."""
    out = store.save_user_edit(db, identity_hash, body.text)
    if out["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="unknown_identity")
    if out["outcome"] == "keine_rawrevision":
        raise HTTPException(status_code=409, detail="keine_rawrevision")
    return out


@router.post("/dictation/{identity_hash}/retranscribe")
def retranscribe_dictation(identity_hash: str, db: Session = Depends(get_db)):
    """Erneutes Transkribieren: neue Revision eigener Provenienz, alte bleibt."""
    out = store.retranscribe(db, identity_hash)
    if out["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail="unknown_identity")
    return out


@router.post("/dictation/{identity_hash}/handoff")
def build_dictation_handoff(identity_hash: str, db: Session = Depends(get_db)):
    """Versioniertes JFW-8-Payload der autoritativen ``raw_transcript``-Revision.

    Enthält Run-/Audio-/Attempt-/Revision-ID, Rohtext und Hash, Sprache,
    Backend-/Modellprofil, Stopgrund und Zielbestätigungsflag. Verfeinerte
    Payloads sind nie ein JFW-7-Rohtranskript (``abweichendes_payload_abgelehnt``).
    """
    state = store.get_state(db, identity_hash)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    if state["status"] != "raw_ready" or not state.get("revision_id"):
        raise HTTPException(status_code=409, detail="kein_rawtranskript")
    from ..database.models import TranscriptRevision

    raw = (
        db.query(TranscriptRevision)
        .filter_by(id=state["revision_id"])
        .one_or_none()
    )
    if raw is None or raw.revision_kind not in (None, "raw_transcript"):
        raise HTTPException(status_code=409, detail="kein_rawtranskript")
    prov = raw.provenance or {}
    payload = build_handoff_payload(
        run_id=state["run_id"],
        audio_hash=state["audio_hash"],
        attempt_id=raw.source_attempt_id,
        revision_id=raw.id,
        raw_text=raw.transcript_raw,
        text_hash=raw.text_hash,
        language=prov.get("language") or {},
        backend_model_profile={
            "backend_variant": prov.get("model", {}).get("backend_variant"),
            "backend_generation": prov.get("model", {}).get("backend_generation"),
            "model_profile": MODEL_PROFILE_ID,
            "stt_model": prov.get("model", {}).get("stt_model"),
            "model_revision": prov.get("model", {}).get("model_revision"),
        },
        stop_reason=state.get("stop_reason") or "unbekannt",
        target_confirmed=False,
    )
    errors = validate_handoff_payload(payload)
    if errors:
        raise HTTPException(status_code=409, detail=errors)
    try:
        assert_payload_kind(payload)
    except HandoffVertragError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"identity_hash": identity_hash, "payload": payload}


@router.get("/dictation/{identity_hash}")
def get_dictation(identity_hash: str, db: Session = Depends(get_db)):
    """Status und autoritativer Rohtext (dieselbe Quelle wie API/Export/JFW-8)."""
    state = store.get_state(db, identity_hash)
    if state is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    return state
