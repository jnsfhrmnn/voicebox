"""JFW-3: Diarisierungs-Endpunkte (lokale API, Datei-/Meeting-Profil).

Ausdruecklich gestartete Sprechererkennungs-Laeufe fuer Jobs mit vorhandener
Transkription (und optional autoritativem JFW-2-Ergebnis). Der Diktatpfad ruft
nichts davon auf. JFW-3 ergaenzt ausschliesslich Sprechercluster/Turns/
Overlap-/Unsicherheitsstatus — nie Text oder Zeiten (heiliger
Unveränderlichkeits-Vertrag, fail-closed).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..database.models import DiarizationResult
from ..diarization.artifacts import verify_artifact
from ..diarization.engine import run_diarization
from ..diarization.provenance import DiarizationRequest, SpeakerSpec, SpeakerSpecError
from ..services import diarization_contract as store

router = APIRouter()

DEFAULT_PROFILE = "meeting_speakers_v1"
DEFAULT_MODEL_ID = "pyannote/speaker-diarization-community-1"


class DiarizationSubmitRequest(BaseModel):
    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int = Field(gt=0)
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    text: str
    words: list[dict] = Field(default_factory=list)
    jfw2_reference_status: str
    jfw2_result_hash: str | None = None
    speaker_mode: str = "auto"
    speaker_count: int | None = None
    speaker_minimum: int | None = None
    speaker_maximum: int | None = None
    diarization_profile: str = DEFAULT_PROFILE
    timebase: str = "audio_ms_v1"


class DiarizationStartRequest(DiarizationSubmitRequest):
    audio_path: str


def _to_request(body: DiarizationSubmitRequest) -> DiarizationRequest:
    try:
        spec = SpeakerSpec.parse(
            body.speaker_mode,
            count=body.speaker_count,
            minimum=body.speaker_minimum,
            maximum=body.speaker_maximum,
        )
    except SpeakerSpecError as exc:
        # Fail-closed: unplausible Vorgaben werden konkret abgelehnt.
        raise HTTPException(status_code=422, detail=exc.reason_code) from exc
    return DiarizationRequest(
        job_id=body.job_id,
        audio_asset_id=body.audio_asset_id,
        audio_hash=body.audio_hash,
        audio_duration_ms=body.audio_duration_ms,
        timebase=body.timebase,
        transcript_run_id=body.transcript_run_id,
        transcript_revision_id=body.transcript_revision_id,
        transcript_revision_hash=body.transcript_revision_hash,
        jfw2_reference_status=body.jfw2_reference_status,
        jfw2_result_hash=body.jfw2_result_hash,
        speaker_spec=spec,
        diarization_profile=body.diarization_profile,
    )


def _result_summary(row) -> dict:
    return {
        "identity_hash": row.identity_hash,
        "status": row.status,
        "result_hash": row.result_hash,
        "reason_code": row.reason_code,
        "speaker_mode": row.speaker_mode,
        "speaker_count_min": row.speaker_count_min,
        "speaker_count_max": row.speaker_count_max,
        "coverage_speech_ms": row.coverage_speech_ms,
        "coverage_usable_ms": row.coverage_usable_ms,
        "cluster_count": row.cluster_count,
        "turn_count": row.turn_count,
        "word_count": row.word_count,
        "word_assigned_count": row.word_assigned_count,
        "overlap_count": row.overlap_count,
        "uncertainty_count": row.uncertainty_count,
        "transcript_revision_id": row.transcript_revision_id,
        "contract_version": row.contract_version,
    }


@router.post("/diarization/submit")
def submit_diarization(body: DiarizationSubmitRequest, db: Session = Depends(get_db)):
    """Legt einen Diarisierungsauftrag an (idempotent; Provenienzkonflikt
    fail-closed). Sprecheranzahlvorgabe ist Teil der Identitaet."""
    request = _to_request(body)
    out = store.submit_diarization(db, request, body.text)
    if out["outcome"] == "revision_hash_mismatch":
        raise HTTPException(status_code=409, detail="revision_hash_mismatch")
    return out


@router.post("/diarization/start")
def start_diarization(body: DiarizationStartRequest, db: Session = Depends(get_db)):
    """Fuehrt den Lauf aus (nachgelagerter Schritt; Provider + Artefakt-Gate
    fail-closed, strikt lokal, kein automatischer Download)."""
    request = _to_request(body)
    model_id = DEFAULT_MODEL_ID

    def artifact_gate():
        # Installationsverzeichnis: <data_root>/models/diarization/<model>.
        from ..config import get_data_dir

        base = get_data_dir() / "models" / "diarization" / model_id.replace("/", "_")
        return verify_artifact(model_id, base)

    from ..diarization.providers.pyannote_pipeline import make_provider

    provider = make_provider(audio_path=body.audio_path, artifact_dir=None)
    out = run_diarization(
        db,
        request,
        body.text,
        body.words,
        provider=provider,
        artifact_gate=artifact_gate,
        app_epoch="api",
        provider_name=model_id,
    )
    return out


@router.post("/diarization/{identity_hash}/cancel")
def cancel_diarization(identity_hash: str, db: Session = Depends(get_db)):
    """Bestaetigter Abbruch: nur der Attempt endet ``canceled``; nach committetem
    Ergebnis sichtbar „zu spaet" (Race fail-closed)."""
    outcome = store.cancel_diarization(db, identity_hash)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.get("/diarization/{identity_hash}")
def get_diarization(identity_hash: str, db: Session = Depends(get_db)):
    """Status, Cluster, Turns, Wort-Overlay, Abdeckungs-/Unsicherheitszaehler
    und Modellprovenienz des Ergebnisses."""
    row = (
        db.query(DiarizationResult)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    summary = _result_summary(row)
    summary["clusters"] = row.clusters
    summary["turns"] = row.turns
    summary["words"] = row.words
    summary["model_id"] = row.model_id
    summary["model_revision"] = row.model_revision
    summary["model_license"] = row.model_license
    return summary
