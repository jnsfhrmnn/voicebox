"""JFW-13: atomare Persistenz des Protokollauftrags (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``export_contract.py``: genau ein
terminaler Ausgang pro Attempt ueber bedingte DB-Transaktionen.

* ``submit_minutes``: Idempotenz ueber ``minutes_key`` + ``payload_hash``,
  fail-closed ``conflict`` bei abweichendem Payload bei gleichem Schluessel.
* ``begin_generation`` / ``commit_result`` / ``cancel_generation`` /
  ``fail_attempt``: Commit und Cancel konkurrieren atomar — der zuerst
  dauerhaft gespeicherte terminale Ausgang gewinnt. Nach ``canceled`` entsteht
  kein Ergebnis; nach dem Commit ist Cancel sichtbar „zu spaet".
* Ergebnis-Commit existiert <=> ``result_hash`` ist gesetzt.
* ``recover_interrupted``: unterbrochene Laeufe fremder Epoche werden ``failed``
  — nie ``generated`` ohne Hash-Abgleich.
* ``invalidate_other_revisions``: Revisionswechsel an gebundenen Quellen
  markieren alte Resultate ``invalidiert`` (exportierte Dokumente bleiben).
* ``add_nondeterminism_revision``: nicht reproduzierbare Modellantworten werden
  als gesondert versionierte Ergebnisrevision gefuehrt — das autoritative
  Ergebnis bleibt unveraendert (Byte-Regel).
* Register: ``save_register``/``latest_register``/``export_gate`` — die
  Zuordnungsinformation liegt ausschliesslich hier (getrennt, loeschbar);
  ``export_gate`` haelt AC 74 (abgelehnt an die geloeschte Registerrevision).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import MinutesResult, PseudonymRegister
from ..minutes.contract import NON_TERMINAL_STATES
from ..minutes.provenance import (
    CONTRACT_VERSION,
    minutes_key as compute_key,
    payload_hash as compute_payload_hash,
)
from ..minutes.pseudonym import (
    REGISTER_CONTRACT_VERSION,
    STATE_GELOESCHT,
    check_export_binding,
)

CommitOutcome = Literal["committed", "already_terminal", "canceled", "not_found"]


def _now() -> datetime:
    return datetime.utcnow()


def submit_minutes(session: Session, request, readiness: dict, warnings,
                   register_id: str) -> dict:
    """Legt den Protokollauftrag an oder bestaetigt vorhandenes idempotent."""
    key = compute_key(request)
    request_payload_hash = compute_payload_hash(request)
    row = session.query(MinutesResult).filter_by(minutes_key=key).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request_payload_hash else "conflict"
        return {
            "outcome": outcome,
            "minutes_key": key,
            "status": row.status,
            "result_hash": row.result_hash,
        }

    row = MinutesResult(
        id=str(uuid.uuid4()),
        minutes_key=key,
        payload_hash=request_payload_hash,
        contract_version=CONTRACT_VERSION,
        minutes_profile=request.minutes_profile,
        register_id=register_id,
        register_revision=request.register_revision,
        job_id=request.job_id,
        audio_asset_id=request.audio_asset_id,
        audio_hash=request.audio_hash,
        audio_duration_ms=int(request.audio_duration_ms),
        timebase=request.timebase,
        transcript_run_id=request.transcript_run_id,
        transcript_revision_id=request.transcript_revision_id,
        transcript_revision_hash=request.transcript_revision_hash,
        transcript_text_hash=request.transcript_text_hash,
        jfw2_result_hash=request.jfw2_result_hash,
        jfw2_status=request.jfw2_status,
        jfw3_result_hash=request.jfw3_result_hash,
        jfw3_status=request.jfw3_status,
        jfw4_export_key=request.jfw4_export_key,
        jfw4_result_hash=request.jfw4_result_hash,
        jfw11_commit_hash=request.jfw11_commit_hash,
        jfw11_status=request.jfw11_status,
        status="queued",
        readiness=readiness,
        warnings=list(warnings or []),
    )
    session.add(row)
    session.commit()
    return {"outcome": "created", "minutes_key": key, "status": row.status,
            "result_hash": None}


def _can_start(row: MinutesResult) -> bool:
    if row.status in ("queued", "waiting_for_local_artifact", "preparing"):
        return True
    return row.status in ("failed", "canceled") and row.result_hash is None


def begin_generation(session: Session, minutes_key_value: str, app_epoch: str) -> str | None:
    """Bedingt startfaehig -> ``generating``. Liefert die ``attempt_id`` oder
    None (kein Doppellauf, nie erneuter Lauf nach Commit)."""
    row = session.query(MinutesResult).filter_by(
        minutes_key=minutes_key_value).one_or_none()
    if row is None or not _can_start(row):
        return None
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.minutes_key == minutes_key_value,
            or_(
                MinutesResult.status.in_(
                    ("queued", "waiting_for_local_artifact", "preparing")),
                MinutesResult.status.in_(("failed", "canceled")),
            ),
            MinutesResult.result_hash.is_(None),
        )
        .values(
            status="generating",
            attempt_id=attempt_id,
            app_epoch=app_epoch,
            reason_code=None,
            updated_at=_now(),
        )
    )
    session.commit()
    return attempt_id if (result.rowcount or 0) > 0 else None


def commit_result(session: Session, minutes_key_value: str, result_hash: str,
                  document: dict, warnings, model_provenance: dict) -> CommitOutcome:
    """Atomarer Ergebnis-Commit (gemeinsam autoritativ oder nie)."""
    status = "generated_with_warnings" if any(
        w.get("kind") in (
            "partially_aligned", "partially_diarized", "secured_partial",
            "sync_unsicher", "single_source", "dual_source_partial",
            "dedupe_unsicher", "pseudonym_unklar", "pseudonym_nicht_ersetzbar",
        ) for w in (warnings or [])) else "generated"
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.minutes_key == minutes_key_value,
            MinutesResult.status == "generating",
            MinutesResult.result_hash.is_(None),
        )
        .values(
            status=status,
            document=document,
            warnings=list(warnings or []),
            model_provenance=model_provenance,
            result_hash=result_hash,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(MinutesResult).filter_by(
        minutes_key=minutes_key_value).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def cancel_generation(session: Session, minutes_key_value: str) -> str:
    """Bestaetigter Abbruch: nur dieser Versuch endet ``canceled``; nach Commit
    sichtbar „zu spaet" (Race fail-closed)."""
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.minutes_key == minutes_key_value,
            MinutesResult.status.in_(NON_TERMINAL_STATES),
            MinutesResult.result_hash.is_(None),
        )
        .values(status="canceled", terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(MinutesResult).filter_by(
        minutes_key=minutes_key_value).one_or_none()
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def fail_attempt(session: Session, minutes_key_value: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Ergebnis-Commit (identischer Retry bleibt moeglich)."""
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.minutes_key == minutes_key_value,
            MinutesResult.status.in_(NON_TERMINAL_STATES),
            MinutesResult.result_hash.is_(None),
        )
        .values(status="failed", reason_code=reason_code,
                terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    return (result.rowcount or 0) > 0


def mark_waiting(session: Session, minutes_key_value: str, reason_code: str) -> bool:
    """Fehlendes lokales Modellartefakt blockiert ausschliesslich JFW-13."""
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.minutes_key == minutes_key_value,
            MinutesResult.result_hash.is_(None),
        )
        .values(status="waiting_for_local_artifact", reason_code=reason_code,
                updated_at=_now())
    )
    session.commit()
    return (result.rowcount or 0) > 0


def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: unterbrochene Laeufe anderer Epoche werden ``failed`` —
    nie ein unvollstaendiges Ergebnis als autoritativ."""
    result = session.execute(
        update(MinutesResult)
        .where(
            MinutesResult.status.in_(("generating", "preparing")),
            MinutesResult.app_epoch != current_epoch,
            MinutesResult.result_hash.is_(None),
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
    jfw3_result_hash: str,
    jfw4_result_hash: str,
    jfw11_commit_hash: str | None = None,
) -> int:
    """Gebundene Quellrevision geaendert: altes Resultat wird ``invalidiert`` —
    bereits exportierte Dokumente bleiben unveraendert."""
    conditions = [
        MinutesResult.job_id == job_id,
        MinutesResult.result_hash.is_not(None),
        MinutesResult.status.in_(("generated", "generated_with_warnings")),
        or_(
            # is_distinct_from ist NULL-sicher (jfw11_commit_hash ist optional)
            MinutesResult.transcript_revision_id.is_distinct_from(transcript_revision_id),
            MinutesResult.jfw2_result_hash.is_distinct_from(jfw2_result_hash),
            MinutesResult.jfw3_result_hash.is_distinct_from(jfw3_result_hash),
            MinutesResult.jfw4_result_hash.is_distinct_from(jfw4_result_hash),
            MinutesResult.jfw11_commit_hash.is_distinct_from(jfw11_commit_hash),
        ),
    ]
    result = session.execute(
        update(MinutesResult)
        .where(*conditions)
        .values(status="invalidated", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def add_nondeterminism_revision(session: Session, minutes_key_value: str,
                                revision: dict) -> bool:
    """Gesondert versionierte Ergebnisrevision — das autoritative Ergebnis bleibt."""
    row = session.query(MinutesResult).filter_by(
        minutes_key=minutes_key_value).one_or_none()
    if row is None or row.result_hash is None:
        return False
    entries = list(row.nondeterminism_revisions or [])
    entries.append(revision)
    row.nondeterminism_revisions = entries
    row.updated_at = _now()
    session.commit()
    return True


def get_minutes(session: Session, minutes_key_value: str) -> dict | None:
    row = session.query(MinutesResult).filter_by(
        minutes_key=minutes_key_value).one_or_none()
    if row is None:
        return None
    return {
        "minutes_key": row.minutes_key,
        "status": row.status,
        "reason_code": row.reason_code,
        "result_hash": row.result_hash,
        "document": row.document,
        "warnings": list(row.warnings or []),
        "nondeterminism_revisions": list(row.nondeterminism_revisions or []),
        "register_id": row.register_id,
        "register_revision": row.register_revision,
        "job_id": row.job_id,
        "transcript_revision_id": row.transcript_revision_id,
    }


def delete_minutes_for_job(session: Session, job_id: str) -> dict:
    """AC 31: gemeinsame Loeschung bei Job-Loeschung — Protokollrevisionen und das
    zugehoerige Pseudonymregister samt Zuordnungsinformation; exportierte
    Nutzerdateien bestehen (JFW-4-Vertrag) sichtbar gewarnt unveraendert fort."""
    rows = session.query(MinutesResult).filter_by(job_id=job_id).all()
    register_ids = {r.register_id for r in rows}
    registers_deleted = 0
    for register_id in register_ids:
        registers_deleted += session.query(PseudonymRegister).filter_by(
            register_id=register_id).delete()
    minutes_deleted = len(rows)
    for row in rows:
        session.delete(row)
    session.commit()
    return {"minutes_deleted": minutes_deleted,
            "registers_deleted": registers_deleted}


def save_register(session: Session, register: dict,
                  minutes_key_value: str | None = None) -> None:
    """Persistiert eine Registerrevision (idempotent je (register_id, revision))."""
    existing = session.query(PseudonymRegister).filter_by(
        register_id=register["register_id"], revision=register["revision"]).one_or_none()
    if existing is not None:
        return
    row = PseudonymRegister(
        id=str(uuid.uuid4()),
        register_id=register["register_id"],
        revision=int(register["revision"]),
        revision_id=register["revision_id"],
        parent_revision_id=register.get("parent_revision_id"),
        minutes_key=minutes_key_value,
        status=register["status"],
        entries=[dict(e) for e in register.get("entries") or []],
        created_at=_now(),
        deleted_at=_now() if register["status"] == STATE_GELOESCHT else None,
    )
    session.add(row)
    session.commit()


def latest_register(session: Session, register_id: str) -> dict | None:
    row = session.query(PseudonymRegister).filter_by(
        register_id=register_id).order_by(PseudonymRegister.revision.desc()).first()
    if row is None:
        return None
    return {
        "contract_version": REGISTER_CONTRACT_VERSION,
        "register_id": row.register_id,
        "revision": row.revision,
        "revision_id": row.revision_id,
        "parent_revision_id": row.parent_revision_id,
        "status": row.status,
        "entries": [dict(e) for e in row.entries or []],
    }


def export_gate(session: Session, register_id: str, bound_register_revision: str) -> str:
    """AC 74: offener Exportdialog an die geloeschte Registerrevision abgelehnt."""
    head = latest_register(session, register_id)
    if head is None:
        return "register_fremd"
    return check_export_binding(head, bound_register_revision)
