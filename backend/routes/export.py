"""JFW-4: Export-Endpunkte (lokale API, Datei-/Meeting-Profil).

Ausdruecklich gestartete Exporte fuer Jobs mit gueltigen JFW-2-/JFW-3-Bindungen
(und optionalen JFW-11-Revisionen). ``prepare`` erzeugt KEINE Dateien;
``run`` validiert die Bindungen erneut fail-closed und schreibt den Set
atomar. Der einfache Capture-Export (`.txt`, `.md`, Audio) bleibt unberuehrt.
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..database.models import ExportJob
from ..export.document import build_set
from ..export.provenance import (
    ExportRequest,
    expected_file_names,
    export_key,
    transcript_text_hash,
)
from ..export.set_writer import (
    LocalFs,
    SetWriteError,
    existing_targets,
    revert_set,
    write_set,
)
from ..export.snapshot import build_snapshot
from ..services import export_contract as store

router = APIRouter()


class ExportPrepareRequest(BaseModel):
    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int = Field(gt=0)
    timebase: str = "audio_ms_v1"
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    text: str
    jfw2_result_hash: str
    jfw2_status: str
    jfw3_result_hash: str
    jfw3_status: str
    jfw11_commit_hash: str | None = None
    jfw11_status: str | None = None
    jfw11_expected: bool = False
    sources: dict = Field(default_factory=dict)
    formats: list[str] = Field(default_factory=lambda: ["json"])
    export_profile: str = "lesbare_untertitel_v1"
    name_policy: str = "neutral"
    partial_mode: str | None = None
    partial_confirmed: bool = False
    target_dir: str | None = None
    replace_existing: bool = False


class ExportRunRequest(ExportPrepareRequest):
    pass


def _to_request(body: ExportPrepareRequest) -> ExportRequest:
    return ExportRequest(
        job_id=body.job_id,
        audio_asset_id=body.audio_asset_id,
        audio_hash=body.audio_hash,
        audio_duration_ms=body.audio_duration_ms,
        timebase=body.timebase,
        transcript_run_id=body.transcript_run_id,
        transcript_revision_id=body.transcript_revision_id,
        transcript_revision_hash=body.transcript_revision_hash,
        transcript_text_hash=transcript_text_hash(body.text),
        jfw2_result_hash=body.jfw2_result_hash,
        jfw2_status=body.jfw2_status,
        jfw3_result_hash=body.jfw3_result_hash,
        jfw3_status=body.jfw3_status,
        jfw11_commit_hash=body.jfw11_commit_hash,
        jfw11_status=body.jfw11_status,
        jfw11_expected=body.jfw11_expected,
        formats=tuple(body.formats),
        export_profile=body.export_profile,
        name_policy=body.name_policy,
        partial_mode=body.partial_mode,
        partial_confirmed=body.partial_confirmed,
        target_dir=body.target_dir,
    )


def _snapshot(body: ExportPrepareRequest):
    return build_snapshot(_to_request(body), body.sources)


@router.post("/export/prepare")
def prepare_export(body: ExportPrepareRequest, db: Session = Depends(get_db)):
    """Prueft Job-, Audio-, Transkript-, JFW-2-, JFW-3- und ggf. JFW-11-Revisionen
    und meldet den Exportstatus inklusive Warnungen, Dateinamen und
    Zielkonflikten. Es werden keine Dateien erzeugt; bei ``blocked`` entsteht
    kein Exportauftrag."""
    request = _to_request(body)
    snap = _snapshot(body)
    names = expected_file_names(request)
    existing = []
    if body.target_dir:
        try:
            existing = existing_targets(LocalFs(), body.target_dir, names)
        except OSError:
            existing = []
    if snap.readiness["state"] == "blocked":
        return {
            "outcome": "blocked",
            "export_key": export_key(request),
            "status": "blocked",
            "result_hash": None,
            "reason_code": snap.readiness.get("reason_code"),
            "readiness": snap.readiness,
            "expected_files": list(names),
            "existing_targets": existing,
        }
    out = store.submit_export(db, request, names, snap.readiness)
    out.update({
        "readiness": snap.readiness,
        "expected_files": list(names),
        "existing_targets": existing,
    })
    return out


@router.post("/export/run")
def run_export(body: ExportRunRequest, db: Session = Depends(get_db)):
    """Erzeugt und validiert den Set vollstaendig und schreibt ihn atomar
    (kein stilles Ueberschreiben; ``replace_existing`` ersetzt gemeinsam)."""
    request = _to_request(body)
    snap = _snapshot(body)
    names = expected_file_names(request)
    if snap.readiness["state"] == "blocked":
        return {
            "outcome": "blocked",
            "export_key": export_key(request),
            "status": "blocked",
            "result_hash": None,
            "reason_code": snap.readiness.get("reason_code"),
            "readiness": snap.readiness,
            "expected_files": list(names),
        }
    if not body.target_dir:
        return {
            "outcome": "failed",
            "export_key": export_key(request),
            "reason_code": "ziel_fehlt",
            "expected_files": list(names),
        }
    submitted = store.submit_export(db, request, names, snap.readiness)
    if submitted["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="export_conflict")
    if submitted["outcome"] == "existing" and submitted.get("result_hash"):
        return {
            "outcome": "already_exported",
            "export_key": submitted["export_key"],
            "status": submitted["status"],
            "result_hash": submitted["result_hash"],
            "expected_files": list(names),
        }
    key = submitted["export_key"]
    attempt = store.begin_export(db, key, app_epoch="api")
    if attempt is None:
        return {
            "outcome": "not_active",
            "export_key": key,
            "status": submitted.get("status"),
            "expected_files": list(names),
        }
    try:
        built = build_set(snap, request)
        expected_hashes = {
            name: hashlib.sha256(data).hexdigest() for name, data in built["files"].items()
        }
        written_out = write_set(
            LocalFs(),
            body.target_dir,
            built["files"],
            expected_hashes,
            attempt_id=attempt,
            replace=bool(body.replace_existing),
        )
        outcome = store.commit_set(
            db,
            key,
            manifest=built["document"]["presentation"],
            result_hash=built["document"]["result_hash"],
        )
        if outcome != "committed":
            # Abbruch/Endausgang gewann die DB-Race: kein Set, Vorsatz wiederher
            revert_set(LocalFs(), body.target_dir,
                       written_out["written"], written_out["backups"])
            return {
                "outcome": outcome,
                "export_key": key,
                "status": outcome,
                "result_hash": None,
                "expected_files": list(names),
                "manifest": built["document"]["presentation"],
                "format_status": built["format_status"],
            }
        return {
            "outcome": "exported" if outcome == "committed" else outcome,
            "export_key": key,
            "status": "exported" if outcome == "committed" else outcome,
            "result_hash": built["document"]["result_hash"],
            "expected_files": list(names),
            "manifest": built["document"]["presentation"],
            "format_status": built["format_status"],
        }
    except SetWriteError as exc:
        store.fail_attempt(db, key, exc.reason_code)
        return {
            "outcome": "failed",
            "export_key": key,
            "status": "failed",
            "reason_code": exc.reason_code,
            "details": exc.details,
            "expected_files": list(names),
        }


@router.post("/export/{export_key}/cancel")
def cancel_export(export_key: str, db: Session = Depends(get_db)):
    """Bestaetigter Abbruch vor Set-Commit; nach ``exported`` sichtbar „zu spaet"."""
    outcome = store.cancel_export(db, export_key)
    return {"export_key": export_key, "outcome": outcome}


@router.get("/export/{export_key}")
def get_export(export_key: str, db: Session = Depends(get_db)):
    """Status, Readiness, Manifest und erwartete Dateien des Exportauftrags."""
    row = db.query(ExportJob).filter_by(export_key=export_key).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_export_key")
    return {
        "export_key": row.export_key,
        "status": row.status,
        "reason_code": row.reason_code,
        "result_hash": row.result_hash,
        "readiness": row.readiness,
        "manifest": row.manifest,
        "expected_files": list(row.expected_files or []),
        "formats": list(row.formats or []),
        "export_profile": row.export_profile,
        "name_policy": row.name_policy,
        "partial_mode": row.partial_mode,
        "target_dir": row.target_dir,
        "contract_version": row.contract_version,
        "job_id": row.job_id,
        "transcript_revision_id": row.transcript_revision_id,
    }
