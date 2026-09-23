"""JFW-2: Atomare Persistenz des Alignment-Ergebnisses (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``: genau ein terminaler Ausgang pro
Attempt ueber bedingte DB-Transaktionen.

* ``submit_alignment``: Idempotenz ueber ``payload_hash``, fail-closed
  Provenienzkonflikt bei abweichendem Payload bei gleicher Identitaet,
  Text-Hash-Pruefung gegen die gebundene Transkriptrevision.
* ``begin_attempt`` / ``commit_result`` / ``cancel_alignment`` /
  ``fail_attempt``: bedingte Mutationen; Commit und Cancel konkurrieren
  atomar — der zuerst dauerhaft gespeicherte terminale Ausgang gewinnt.
  Nach ``canceled`` wird nie mehr ein Ergebnis autoritativ; nach bereits
  committetem Ergebnis wird Cancel sichtbar „zu spaet" behandelt.
* Ergebnis-Commit existiert <=> ``result_hash`` ist gesetzt. ``failed``/``canceled``
  OHNE ``result_hash`` sind Fehler/Abbrueche ohne autoritatives Ergebnis und
  duerfen identisch erneut gerechnet werden (Spec: identischer Retry).
* ``recover_interrupted``: unterbrochene ``aligning``-Laeufe fremder Epoche
  werden zurueck auf ``queued`` gesetzt (fortsetzbar) — nie als autoritativ.
* ``invalidate_other_revisions``: Ergebnisse anderer Transkriptrevisionen
  desselben Jobs werden ``invalidated`` (revisionsgebunden erhalten).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..alignment.provenance import AlignmentRequest, transcript_text_hash
from ..database.models import AlignmentResult

#: Nichtterminale Zustaende, in denen ein Lauf aktiv/planbar ist.
NON_TERMINAL_STATES = ("queued", "waiting_for_local_artifact", "aligning")
#: Terminale Zustaende MIT autoritativem Ergebnis-Commit.
COMMITTED_STATES = ("aligned", "partially_aligned", "no_alignable_speech", "failed")

CommitOutcome = Literal["committed", "already_terminal", "canceled", "not_found"]


def _now() -> datetime:
    return datetime.utcnow()


def submit_alignment(session: Session, request: AlignmentRequest, text: str) -> dict:
    """Legt den Auftrag an oder liefert das vorhandene Ergebnis idempotent."""
    identity_hash = request.identity_hash()
    if transcript_text_hash(text) != request.transcript_revision_hash:
        return {
            "outcome": "revision_hash_mismatch",
            "identity_hash": identity_hash,
            "status": None,
            "result_hash": None,
        }

    row = session.query(AlignmentResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request.payload_hash() else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
            "result_hash": row.result_hash,
            "transcript_revision_id": row.transcript_revision_id,
        }

    row = AlignmentResult(
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
        language_ranges=[list(r) for r in request.language_ranges],
        alignment_profile=request.alignment_profile,
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
    }


def _can_start(row: AlignmentResult) -> bool:
    if row.status in ("queued", "waiting_for_local_artifact"):
        return True
    return row.status in ("failed", "canceled") and row.result_hash is None


def begin_attempt(session: Session, identity_hash: str, app_epoch: str) -> str | None:
    """Bedingt startfaehig -> ``aligning``. Liefert die ``attempt_id`` oder None
    (kein Doppellauf bei laufendem oder bereits committetem Ergebnis)."""
    row = (
        session.query(AlignmentResult)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )
    if row is None or not _can_start(row):
        return None
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.identity_hash == identity_hash,
            or_(
                AlignmentResult.status.in_(("queued", "waiting_for_local_artifact")),
                AlignmentResult.status.in_(("failed", "canceled")),
            ),
            AlignmentResult.result_hash.is_(None),
        )
        .values(
            status="aligning",
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
        update(AlignmentResult)
        .where(
            AlignmentResult.identity_hash == identity_hash,
            AlignmentResult.status.in_(NON_TERMINAL_STATES),
            AlignmentResult.result_hash.is_(None),
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
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.identity_hash == identity_hash,
            AlignmentResult.status.in_(NON_TERMINAL_STATES),
            AlignmentResult.result_hash.is_(None),
        )
        .values(
            status=built["status"],
            words=built["words"],
            coverage_alignable=built["coverage_alignable"],
            coverage_aligned=built["coverage_aligned"],
            result_hash=built["result_hash"],
            model_id=model_provenance.get("model_id"),
            model_revision=model_provenance.get("model_revision"),
            model_sha256=model_provenance.get("model_sha256"),
            model_license=model_provenance.get("model_license"),
            reason_code=None,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(AlignmentResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def cancel_alignment(session: Session, identity_hash: str) -> str:
    """Bestaetigter Nutzerabbruch: nur der Attempt endet ``canceled``.

    Liefert ``canceled`` (gewonnen), ``too_late`` (Ergebnis bereits committet —
    sichtbar zu spaet) oder ``not_active``.
    """
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.identity_hash == identity_hash,
            AlignmentResult.status.in_(NON_TERMINAL_STATES),
            AlignmentResult.result_hash.is_(None),
        )
        .values(status="canceled", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(AlignmentResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def fail_attempt(session: Session, identity_hash: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Ergebnis-Commit (identischer Retry bleibt moeglich)."""
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.identity_hash == identity_hash,
            AlignmentResult.status.in_(NON_TERMINAL_STATES),
            AlignmentResult.result_hash.is_(None),
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
    """Crash-Recovery: unterbrochene ``aligning``-Laeufe anderer Epoche werden
    wieder startfaehig (``queued``). Bereits committete Ergebnisse bleiben
    unangetastet; nichts wird als autoritativer Ausgang erfunden."""
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.status == "aligning",
            AlignmentResult.app_epoch != current_epoch,
            AlignmentResult.result_hash.is_(None),
        )
        .values(status="queued", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def invalidate_other_revisions(
    session: Session, job_id: str, current_transcript_revision_id: str
) -> int:
    """Neue inhaltliche Transkriptrevision: bisherige Ergebnisse anderer
    Revisionen werden sichtbar ``invalidated`` — revisionsgebunden erhalten und
    niemals als aktuell fuer den neuen Text angezeigt."""
    result = session.execute(
        update(AlignmentResult)
        .where(
            AlignmentResult.job_id == job_id,
            AlignmentResult.transcript_revision_id != current_transcript_revision_id,
            AlignmentResult.result_hash.is_not(None),
            AlignmentResult.status.in_(COMMITTED_STATES),
        )
        .values(status="invalidated", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0
