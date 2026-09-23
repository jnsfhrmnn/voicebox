"""JFW-4: atomare Persistenz des Exportauftrags (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``diarization_contract.py``: genau ein
terminaler Ausgang pro Attempt ueber bedingte DB-Transaktionen.

* ``submit_export``: Idempotenz ueber ``export_key`` + ``payload_hash``,
  fail-closed ``conflict`` bei abweichendem Payload bei gleichem Schluessel.
* ``begin_export`` / ``commit_set`` / ``cancel_export`` / ``fail_attempt``:
  Set-Commit und Cancel konkurrieren atomar — der zuerst dauerhaft gespeicherte
  terminale Ausgang gewinnt. Nach ``canceled`` entsteht kein Set; nach
  ``exported`` ist Cancel sichtbar „zu spaet".
* Ergebnis-Commit existiert <=> ``result_hash`` ist gesetzt.
* ``recover_interrupted``: unterbrochene ``exporting``-Laeufe fremder Epoche
  werden ``failed`` (Reste bereinigbar) — nie ``exported`` ohne Hash-Abgleich.
* ``invalidate_other_revisions``: Revisionswechsel an gebundenen Quellen
  markieren alte Nachweise ``invalidiert``; die bewusst exportierten
  Nutzerdateien bleiben unveraendert.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import ExportJob
from ..export.contract import NON_TERMINAL_STATES
from ..export.provenance import (
    CONTRACT_VERSION,
    export_key,
    normalize_formats,
    payload_hash,
)

CommitOutcome = Literal["committed", "already_terminal", "canceled", "not_found"]


def _now() -> datetime:
    return datetime.utcnow()


def submit_export(session: Session, request, expected_files, readiness: dict) -> dict:
    """Legt den Exportauftrag an oder bestaetigt vorhandenes idempotent."""
    key = export_key(request)
    request_payload_hash = payload_hash(request)
    row = session.query(ExportJob).filter_by(export_key=key).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request_payload_hash else "conflict"
        return {
            "outcome": outcome,
            "export_key": key,
            "status": row.status,
            "result_hash": row.result_hash,
            "expected_files": list(row.expected_files or []),
        }

    row = ExportJob(
        id=str(uuid.uuid4()),
        export_key=key,
        payload_hash=request_payload_hash,
        contract_version=CONTRACT_VERSION,
        job_id=request.job_id,
        audio_asset_id=request.audio_asset_id,
        audio_hash=request.audio_hash,
        audio_duration_ms=int(request.audio_duration_ms),
        timebase=request.timebase,
        transcript_run_id=request.transcript_run_id,
        transcript_revision_id=request.transcript_revision_id,
        transcript_revision_hash=request.transcript_revision_hash,
        jfw2_result_hash=request.jfw2_result_hash,
        jfw2_status=request.jfw2_status,
        jfw3_result_hash=request.jfw3_result_hash,
        jfw3_status=request.jfw3_status,
        jfw11_commit_hash=request.jfw11_commit_hash,
        jfw11_status=request.jfw11_status,
        formats=list(normalize_formats(request.formats)),
        export_profile=request.export_profile,
        name_policy=request.name_policy,
        partial_mode=request.partial_mode,
        partial_confirmed=bool(request.partial_confirmed),
        target_dir=request.target_dir,
        expected_files=list(expected_files),
        status=readiness.get("state", "preparing"),
        readiness=readiness,
        reason_code=readiness.get("reason_code"),
    )
    session.add(row)
    session.commit()
    return {
        "outcome": "created",
        "export_key": key,
        "status": row.status,
        "result_hash": None,
        "expected_files": list(expected_files),
    }


def _can_start(row: ExportJob) -> bool:
    if row.status in ("preparing", "ready", "ready_with_warnings"):
        return True
    return row.status in ("failed", "canceled") and row.result_hash is None


def begin_export(session: Session, export_key_value: str, app_epoch: str) -> str | None:
    """Bedingt startfaehig -> ``exporting``. Liefert die ``attempt_id`` oder
    None (kein Doppellauf, nie erneuter Lauf nach ``exported``)."""
    row = session.query(ExportJob).filter_by(export_key=export_key_value).one_or_none()
    if row is None or not _can_start(row):
        return None
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(ExportJob)
        .where(
            ExportJob.export_key == export_key_value,
            or_(
                ExportJob.status.in_(("preparing", "ready", "ready_with_warnings")),
                ExportJob.status.in_(("failed", "canceled")),
            ),
            ExportJob.result_hash.is_(None),
        )
        .values(
            status="exporting",
            attempt_id=attempt_id,
            app_epoch=app_epoch,
            reason_code=None,
            updated_at=_now(),
        )
    )
    session.commit()
    return attempt_id if (result.rowcount or 0) > 0 else None


def commit_set(
    session: Session, export_key_value: str, manifest: list, result_hash: str
) -> CommitOutcome:
    """Atomarer Set-Commit (gemeinsam autoritativ oder nie)."""
    result = session.execute(
        update(ExportJob)
        .where(
            ExportJob.export_key == export_key_value,
            ExportJob.status == "exporting",
            ExportJob.result_hash.is_(None),
        )
        .values(
            status="exported",
            manifest=manifest,
            result_hash=result_hash,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(ExportJob).filter_by(export_key=export_key_value).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def cancel_export(session: Session, export_key_value: str) -> str:
    """Bestaetigter Abbruch: nur der Attempt endet ``canceled``; nach committetem
    Set sichtbar „zu spaet" (Race fail-closed)."""
    result = session.execute(
        update(ExportJob)
        .where(
            ExportJob.export_key == export_key_value,
            ExportJob.status.in_(NON_TERMINAL_STATES),
            ExportJob.result_hash.is_(None),
        )
        .values(status="canceled", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(ExportJob).filter_by(export_key=export_key_value).one_or_none()
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def fail_attempt(session: Session, export_key_value: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Set-Commit (identischer Retry bleibt moeglich)."""
    result = session.execute(
        update(ExportJob)
        .where(
            ExportJob.export_key == export_key_value,
            ExportJob.status.in_(NON_TERMINAL_STATES),
            ExportJob.result_hash.is_(None),
        )
        .values(
            status="failed",
            reason_code=reason_code,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    return (result.rowcount or 0) > 0


def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: unterbrochene ``exporting``-Laeufe anderer Epoche werden
    ``failed`` (Attempt-Reste bereinigbar) — niemals ``exported``."""
    result = session.execute(
        update(ExportJob)
        .where(
            ExportJob.status == "exporting",
            ExportJob.app_epoch != current_epoch,
            ExportJob.result_hash.is_(None),
        )
        .values(status="failed", reason_code="unterbrochen", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def invalidate_other_revisions(
    session: Session,
    job_id: str,
    transcript_revision_id: str,
    jfw2_result_hash: str,
    jfw3_result_hash: str | None = None,
    jfw11_commit_hash: str | None = None,
) -> int:
    """Gebundene Quellrevision geaendert: alter Exportnachweis wird
    ``invalidiert`` — die bewusst exportierten Nutzerdateien bleiben bestehen."""
    conditions = [
        ExportJob.job_id == job_id,
        ExportJob.result_hash.is_not(None),
        ExportJob.status == "exported",
        or_(
            ExportJob.transcript_revision_id != transcript_revision_id,
            ExportJob.jfw2_result_hash != jfw2_result_hash,
            ExportJob.jfw3_result_hash != jfw3_result_hash,
            ExportJob.jfw11_commit_hash != jfw11_commit_hash,
        ),
    ]
    result = session.execute(
        update(ExportJob)
        .where(*conditions)
        .values(status="invalidated", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0
