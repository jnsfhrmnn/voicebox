"""JFW-3: Atomare Persistenz des Diarisierungsergebnisses (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``alignment_contract.py``: genau ein
terminaler Ausgang pro Attempt ueber bedingte DB-Transaktionen.

* ``submit_diarization``: Idempotenz ueber ``payload_hash``, fail-closed
  Provenienzkonflikt bei abweichendem Payload bei gleicher Identitaet,
  Text-Hash-Pruefung gegen die gebundene Transkriptrevision.
* ``begin_attempt`` / ``commit_result`` / ``cancel_diarization`` /
  ``fail_attempt``: bedingte Mutationen; Commit und Cancel konkurrieren
  atomar — der zuerst dauerhaft gespeicherte terminale Ausgang gewinnt.
  Nach ``canceled`` wird nie mehr ein Ergebnis autoritativ; nach bereits
  committetem Ergebnis wird Cancel sichtbar „zu spaet" behandelt.
* Ergebnis-Commit existiert <=> ``result_hash`` ist gesetzt. ``failed``/
  ``canceled`` OHNE ``result_hash`` sind Fehler/Abbrueche ohne autoritatives
  Ergebnis und duerfen identisch erneut gerechnet werden.
* ``recover_interrupted``: unterbrochene ``diarizing``-Laeufe fremder Epoche
  werden zurueck auf ``queued`` gesetzt (fortsetzbar) — nie als autoritativ.
* ``invalidate_other_revisions``: Ergebnisse anderer Transkript- oder
  JFW-2-Revisionen desselben Jobs werden ``invalidated`` (revisionsgebunden
  erhalten).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import DiarizationResult
from ..diarization.provenance import DiarizationRequest, transcript_text_hash

#: Nichtterminale Zustaende, in denen ein Lauf aktiv/planbar ist.
NON_TERMINAL_STATES = ("queued", "waiting_for_local_artifact", "diarizing")
#: Terminale Zustaende MIT autoritativem Ergebnis-Commit.
COMMITTED_STATES = ("diarized", "partially_diarized", "no_speech", "failed")

CommitOutcome = Literal["committed", "already_terminal", "canceled", "not_found"]


def _now() -> datetime:
    return datetime.utcnow()


def _speaker_bounds(spec) -> tuple[int | None, int | None]:
    if spec.mode == "exact":
        return spec.count, spec.count
    if spec.mode == "range":
        return spec.minimum, spec.maximum
    return None, None


def submit_diarization(session: Session, request: DiarizationRequest, text: str) -> dict:
    """Legt den Auftrag an oder liefert das vorhandene Ergebnis idempotent."""
    identity_hash = request.identity_hash()
    if transcript_text_hash(text) != request.transcript_revision_hash:
        return {
            "outcome": "revision_hash_mismatch",
            "identity_hash": identity_hash,
            "status": None,
            "result_hash": None,
        }

    row = session.query(DiarizationResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request.payload_hash() else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
            "result_hash": row.result_hash,
            "transcript_revision_id": row.transcript_revision_id,
            "model_id": row.model_id,
            "speaker_mode": row.speaker_mode,
        }

    speaker_min, speaker_max = _speaker_bounds(request.speaker_spec)
    row = DiarizationResult(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=request.payload_hash(),
        job_id=request.job_id,
        audio_asset_id=request.audio_asset_id,
        audio_hash=request.audio_hash,
        audio_duration_ms=int(request.audio_duration_ms),
        timebase=request.timebase,
        transcript_run_id=request.transcript_run_id,
        transcript_revision_id=request.transcript_revision_id,
        transcript_revision_hash=request.transcript_revision_hash,
        jfw2_reference_status=request.jfw2_reference_status,
        jfw2_result_hash=request.jfw2_result_hash,
        speaker_mode=request.speaker_spec.mode,
        speaker_count_min=speaker_min,
        speaker_count_max=speaker_max,
        diarization_profile=request.diarization_profile,
        contract_version=request.contract_version,
        status="queued",
    )
    session.add(row)
    session.commit()
    return {
        "outcome": "created",
        "identity_hash": identity_hash,
        "status": "queued",
        "result_hash": None,
        "transcript_revision_id": row.transcript_revision_id,
        "speaker_mode": row.speaker_mode,
    }


def _can_start(row: DiarizationResult) -> bool:
    if row.status in ("queued", "waiting_for_local_artifact"):
        return True
    return row.status in ("failed", "canceled") and row.result_hash is None


def begin_attempt(session: Session, identity_hash: str, app_epoch: str) -> str | None:
    """Bedingt startfaehig -> ``diarizing``. Liefert die ``attempt_id`` oder None
    (kein Doppellauf bei laufendem oder bereits committetem Ergebnis)."""
    row = (
        session.query(DiarizationResult)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None or not _can_start(row):
        return None
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.identity_hash == identity_hash,
            or_(
                DiarizationResult.status.in_(("queued", "waiting_for_local_artifact")),
                DiarizationResult.status.in_(("failed", "canceled")),
            ),
            DiarizationResult.result_hash.is_(None),
        )
        .values(
            status="diarizing",
            attempt_id=attempt_id,
            app_epoch=app_epoch,
            reason_code=None,
            updated_at=_now(),
        )
    )
    session.commit()
    return attempt_id if (result.rowcount or 0) > 0 else None


def mark_waiting_for_artifact(
    session: Session, identity_hash: str, reason_code: str
) -> bool:
    """Fehlendes/korruptes Artefakt sichtbar machen (kein automatischer Download)."""
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.identity_hash == identity_hash,
            DiarizationResult.status.in_(NON_TERMINAL_STATES),
            DiarizationResult.result_hash.is_(None),
        )
        .values(
            status="waiting_for_local_artifact",
            reason_code=reason_code,
            updated_at=_now(),
        )
    )
    session.commit()
    return (result.rowcount or 0) > 0


def commit_result(
    session: Session, identity_hash: str, built: dict, model_provenance: dict
) -> CommitOutcome:
    """Atomarer Ergebnis-Commit (genau ein terminaler Ausgang pro Attempt)."""
    counters = built.get("counters", {})
    cov = built.get("coverage", {})
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.identity_hash == identity_hash,
            DiarizationResult.status.in_(NON_TERMINAL_STATES),
            DiarizationResult.result_hash.is_(None),
        )
        .values(
            status=built["status"],
            reason_code=built.get("reason_code"),
            clusters=built["clusters"],
            turns=built["turns"],
            words=built["words"],
            coverage_speech_ms=int(cov.get("speech_ms", 0)),
            coverage_usable_ms=int(cov.get("usable_ms", 0)),
            cluster_count=int(counters.get("cluster_count", 0)),
            turn_count=int(counters.get("turn_count", 0)),
            word_count=int(counters.get("word_count", 0)),
            word_assigned_count=int(counters.get("word_assigned_count", 0)),
            overlap_count=int(counters.get("overlap_count", 0)),
            uncertainty_count=int(counters.get("uncertainty_count", 0)),
            result_hash=built["result_hash"],
            model_id=model_provenance.get("model_id"),
            model_revision=model_provenance.get("model_revision"),
            model_sha256=model_provenance.get("model_sha256"),
            model_license=model_provenance.get("model_license"),
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(DiarizationResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def cancel_diarization(session: Session, identity_hash: str) -> str:
    """Bestaetigter Nutzerabbruch: nur der Attempt endet ``canceled``.

    Liefert ``canceled`` (gewonnen), ``too_late`` (Ergebnis bereits committet —
    sichtbar zu spaet) oder ``not_active``.
    """
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.identity_hash == identity_hash,
            DiarizationResult.status.in_(NON_TERMINAL_STATES),
            DiarizationResult.result_hash.is_(None),
        )
        .values(status="canceled", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(DiarizationResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def fail_attempt(session: Session, identity_hash: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Ergebnis-Commit (identischer Retry bleibt moeglich)."""
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.identity_hash == identity_hash,
            DiarizationResult.status.in_(NON_TERMINAL_STATES),
            DiarizationResult.result_hash.is_(None),
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
    """Crash-Recovery: unterbrochene ``diarizing``-Laeufe anderer Epoche werden
    wieder startfaehig (``queued``). Bereits committete Ergebnisse bleiben
    unangetastet; nichts wird als autoritativer Ausgang erfunden."""
    result = session.execute(
        update(DiarizationResult)
        .where(
            DiarizationResult.status == "diarizing",
            DiarizationResult.app_epoch != current_epoch,
            DiarizationResult.result_hash.is_(None),
        )
        .values(status="queued", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def invalidate_other_revisions(
    session: Session,
    job_id: str,
    current_transcript_revision_id: str,
    current_jfw2_result_hash: str | None = None,
) -> int:
    """Neue inhaltliche Transkript- oder JFW-2-Revision: bisherige Ergebnisse
    anderer Revisionen werden sichtbar ``invalidated`` — revisionsgebunden
    erhalten und niemals als aktuell fuer die neuen Quellen angezeigt."""
    conditions = [
        DiarizationResult.job_id == job_id,
        DiarizationResult.result_hash.is_not(None),
        DiarizationResult.status.in_(COMMITTED_STATES),
        or_(
            DiarizationResult.transcript_revision_id != current_transcript_revision_id,
            DiarizationResult.jfw2_result_hash != current_jfw2_result_hash,
        ),
    ]
    result = session.execute(
        update(DiarizationResult)
        .where(*conditions)
        .values(status="invalidated", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0
