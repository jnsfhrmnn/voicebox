"""JFW-2: Alignment-Endpunkte (lokale API, Datei-/Meeting-Profil).

Ausdruecklich gestartete Forced-Alignment-Laeufe fuer Jobs mit abgeschlossener
Transkription. Der Diktatpfad ruft nichts davon auf. Zeitvarianten bleiben
getrennt: Backend-Wortzeiten sind ``backend_precise``, JFW-2 schreibt
ausschliesslich ``forced_alignment`` (Alignment-Profil ``precise_words_v1``).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..alignment.artifacts import verify_artifact
from ..alignment.engine import run_alignment
from ..alignment.provenance import AlignmentRequest
from ..database import get_db
from ..database.models import AlignmentResult
from ..services import alignment_contract as store

router = APIRouter()

DEFAULT_PROFILE = "precise_words_v1"
DEFAULT_MODEL_ID = "facebook/mms_fa"


class LanguageRange(BaseModel):
    lang: str
    char_start: int
    char_end: int


class AlignmentSubmitRequest(BaseModel):
    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int = Field(gt=0)
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    text: str
    language_ranges: list[LanguageRange] = Field(default_factory=list)
    alignment_profile: str = DEFAULT_PROFILE
    timebase: str = "audio_ms_v1"


class AlignmentStartRequest(AlignmentSubmitRequest):
    audio_path: str


def _to_request(body: AlignmentSubmitRequest) -> AlignmentRequest:
    return AlignmentRequest(
        job_id=body.job_id,
        audio_asset_id=body.audio_asset_id,
        audio_hash=body.audio_hash,
        audio_duration_ms=body.audio_duration_ms,
        timebase=body.timebase,
        transcript_run_id=body.transcript_run_id,
        transcript_revision_id=body.transcript_revision_id,
        transcript_revision_hash=body.transcript_revision_hash,
        language_ranges=tuple(
            (r.lang, r.char_start, r.char_end) for r in body.language_ranges
        ),
        alignment_profile=body.alignment_profile,
    )


def _result_summary(row) -> dict:
    return {
        "identity_hash": row.identity_hash,
        "status": row.status,
        "result_hash": row.result_hash,
        "coverage_alignable": row.coverage_alignable,
        "coverage_aligned": row.coverage_aligned,
        "reason_code": row.reason_code,
        "transcript_revision_id": row.transcript_revision_id,
        "contract_version": row.contract_version,
    }


@router.post("/alignment/submit")
def submit_alignment(body: AlignmentSubmitRequest, db: Session = Depends(get_db)):
    """Legt einen Alignment-Auftrag an (idempotent; Provenienzkonflikt fail-closed)."""
    request = _to_request(body)
    out = store.submit_alignment(db, request, body.text)
    if out["outcome"] == "revision_hash_mismatch":
        raise HTTPException(status_code=409, detail="revision_hash_mismatch")
    return out


@router.post("/alignment/start")
def start_alignment(body: AlignmentStartRequest, db: Session = Depends(get_db)):
    """Fuehrt den Lauf aus (nachgelagerter Schritt; Provider + Artefakt-Gate fail-closed)."""
    request = _to_request(body)
    model_id = DEFAULT_MODEL_ID

    def artifact_gate():
        # Installationsverzeichnis: <data_root>/models/alignment/<model>.
        from ..config import get_data_dir

        base = get_data_dir() / "models" / "alignment" / model_id.replace("/", "_")
        return verify_artifact(model_id, base)

    from ..alignment.providers.mms_fa import make_provider

    provider = make_provider(
        audio_path=body.audio_path,
        artifact_dir=None,
    )
    out = run_alignment(
        db,
        request,
        body.text,
        provider=provider,
        artifact_gate=artifact_gate,
        app_epoch="api",
        provider_name=model_id,
    )
    return out


@router.post("/alignment/{identity_hash}/cancel")
def cancel_alignment(identity_hash: str, db: Session = Depends(get_db)):
    """Bestaetigter Abbruch: nur der Attempt endet ``canceled`` (Race fail-closed)."""
    outcome = store.cancel_alignment(db, identity_hash)
    return {"identity_hash": identity_hash, "outcome": outcome}


@router.get("/alignment/{identity_hash}")
def get_alignment(identity_hash: str, db: Session = Depends(get_db)):
    """Status, Abdeckung, Alignment-Version und Wortliste des Ergebnisses."""
    row = (
        db.query(AlignmentResult)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_identity")
    summary = _result_summary(row)
    summary["words"] = row.words
    summary["model_id"] = row.model_id
    summary["model_revision"] = row.model_revision
    summary["model_license"] = row.model_license
    return summary
