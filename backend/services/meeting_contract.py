"""JFW-11: Atomare Persistenz des Meeting-Ergebnisses (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``alignment_contract.py``/
``diarization_contract.py``: genau ein terminaler Ausgang pro Attempt ueber
bedingte DB-Transaktionen.

* ``submit_meeting``: Idempotenz ueber ``payload_hash`` (``existing``),
  fail-closed Provenienzkonflikt bei abweichendem Payload bei gleicher
  Identitaet (``conflict``).
* ``begin_attempt`` / ``commit_result`` / ``cancel_meeting`` / ``fail_attempt``:
  bedingte Mutationen; Commit und Cancel konkurrieren atomar — der zuerst
  dauerhaft gespeicherte terminale Ausgang gewinnt.
* Ergebnis-Commit existiert <=> ``result_hash`` gesetzt. ``failed``/``canceled``
  OHNE ``result_hash`` sind Fehler/Abbruche ohne autoritatives Ergebnis und
  duerfen identisch erneut gerechnet werden.
* ``recover_interrupted``: unterbrochene ``capturing``-Laeufe fremder Epoche
  werden zurueck auf ``queued`` gesetzt (fortsetzbar) — nie als autoritativ.
* ``invalidate_other_revisions``: Ergebnisse anderer Manifest-/Track-Revisionen
  desselben Jobs werden ``invalidated`` (revisionsgebunden erhalten).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import MeetingResult
from ..meeting.provenance import MeetingRequest

#: Nichtterminale Zustaende, in denen ein Lauf aktiv/planbar ist.
NON_TERMINAL_STATES = ("queued", "capturing")
#: Terminale Zustaende MIT autoritativem Ergebnis-Commit.
COMMITTED_STATES = ("secured_dual", "secured_partial")

CommitOutcome = Literal["committed", "already_terminal", "canceled", "not_found"]


def _now() -> datetime:
    return datetime.utcnow()


def submit_meeting(session: Session, request: MeetingRequest) -> dict:
    """Legt den Meeting-Job an oder liefert den vorhandenen Stand idempotent."""
    identity_hash = request.identity_hash()
    row = session.query(MeetingResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request.payload_hash() else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
            "result_hash": row.result_hash,
        }

    row = MeetingResult(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=request.payload_hash(),
        job_id=request.job_id,
        meeting_run_id=request.meeting_run_id,
        jfw6_run_reference=request.jfw6_run_reference,
        contract_version=request.contract_version,
        jfw2_result_hash=request.jfw2_result_hash,
        jfw3_result_hash=request.jfw3_result_hash,
        manifest_hash=request.manifest_hash,
        status="queued",
    )
    session.add(row)
    session.commit()
    return {
        "outcome": "created",
        "identity_hash": identity_hash,
        "status": "queued",
        "result_hash": None,
    }


def _can_start(row: MeetingResult) -> bool:
    if row.status in NON_TERMINAL_STATES:
        return True
    return row.status in ("failed", "canceled") and row.result_hash is None


def begin_attempt(session: Session, identity_hash: str, app_epoch: str) -> str | None:
    """Bedingt startfaehig -> ``capturing``. Liefert die ``attempt_id`` oder None
    (kein Doppellauf bei laufendem oder bereits committetem Ergebnis)."""
    row = session.query(MeetingResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None or not _can_start(row):
        return None
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.identity_hash == identity_hash,
            or_(
                MeetingResult.status.in_(NON_TERMINAL_STATES),
                MeetingResult.status.in_(("failed", "canceled")),
            ),
            MeetingResult.result_hash.is_(None),
        )
        .values(
            status="capturing",
            attempt_id=attempt_id,
            app_epoch=app_epoch,
            reason_code=None,
            updated_at=_now(),
        )
    )
    session.commit()
    return attempt_id if (result.rowcount or 0) > 0 else None


def commit_result(session: Session, identity_hash: str, built: dict) -> CommitOutcome:
    """Atomarer Ergebnis-Commit (genau ein terminaler Ausgang pro Attempt)."""
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.identity_hash == identity_hash,
            MeetingResult.status.in_(NON_TERMINAL_STATES),
            MeetingResult.result_hash.is_(None),
        )
        .values(
            status=built["status"],
            reason_code=built.get("reason_code"),
            stop_reason=built.get("stop_reason"),
            tracks=built["tracks"],
            gaps=built["gaps"],
            sync=built["sync"],
            sound_cue_marks=built["sound_cue_marks"],
            recovery_status=built["recovery_status"],
            dedupe=built["dedupe"] or [],
            name_mappings=built["name_mappings"],
            result_hash=built["result_hash"],
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(MeetingResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def cancel_meeting(session: Session, identity_hash: str) -> str:
    """Bestätigter Nutzerabbruch: nur der Attempt endet ``canceled``.

    Liefert ``canceled`` (gewonnen), ``too_late`` (Ergebnis bereits committet —
    sichtbar zu spaet) oder ``not_active``.
    """
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.identity_hash == identity_hash,
            MeetingResult.status.in_(NON_TERMINAL_STATES),
            MeetingResult.result_hash.is_(None),
        )
        .values(status="canceled", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(MeetingResult).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def fail_attempt(session: Session, identity_hash: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Ergebnis-Commit (identischer Retry bleibt moeglich)."""
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.identity_hash == identity_hash,
            MeetingResult.status.in_(NON_TERMINAL_STATES),
            MeetingResult.result_hash.is_(None),
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
    """Crash-Recovery: unterbrochene ``capturing``-Laeufe anderer Epoche werden
    wieder startfaehig (``queued``). Bereits committete Ergebnisse bleiben
    unangetastet; nichts wird als autoritativer Ausgang erfunden."""
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.status == "capturing",
            MeetingResult.app_epoch != current_epoch,
            MeetingResult.result_hash.is_(None),
        )
        .values(status="queued", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def invalidate_other_revisions(
    session: Session, job_id: str, current_identity_hash: str
) -> int:
    """Neue autoritative Manifest-/Track-Revision: bisherige Ergebnisse anderer
    Revisionen werden sichtbar ``invalidated`` — revisionsgebunden erhalten und
    niemals als aktuell fuer die neuen Quellen angezeigt."""
    result = session.execute(
        update(MeetingResult)
        .where(
            MeetingResult.job_id == job_id,
            MeetingResult.identity_hash != current_identity_hash,
            MeetingResult.result_hash.is_not(None),
            MeetingResult.status.in_(COMMITTED_STATES),
        )
        .values(status="invalidated", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0
