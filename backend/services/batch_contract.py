"""JFW-5: atomare Persistenz des Batch-Auftragsmodells (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``export_contract.py``: genau ein
terminaler Ausgang pro Elementversuch ueber bedingte DB-Transaktionen.

* ``submit_batch``: Idempotenz ueber ``identity_hash`` + ``payload_hash``,
  fail-closed ``conflict`` bei abweichendem Payload bei gleicher Identitaet.
* ``begin_item_attempt``: hoechstens ein aktiver Versuch je Element.
* ``finalize_item_attempt`` / ``cancel_attempt`` konkurrieren atomar — der
  zuerst dauerhaft gespeicherte terminale Ausgang gewinnt; bereits gesicherte
  Ergebnisreferenzen (``result_refs``) bleiben unangetastet.
* ``recover_interrupted``: aktive Versuche fremder Epoche werden
  ``interrupted`` (fortsetzbar, KEIN fachlicher Erfolg), der Batch geht
  ``paused`` (Grund ``unterbrochen``) — nie ``succeeded`` ohne Commit.
* ``resume_batch`` erfordert ausdrueckliche Bestaetigung des urspruenglichen
  Vertrags (fail-closed, keine Rekonstruktion aus globalen Defaults).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..batch.identity import new_attempt_id
from ..batch.provenance import canonical_hash
from ..batch.snapshot import snapshot_hash as compute_snapshot_hash
from ..batch.state import aggregate_batch, can_transition
from ..database.models import Batch, BatchAttempt, BatchItem

CommitOutcome = Literal["finalized", "already_terminal", "not_found"]

ACTIVE_ITEM_STATES = ("waiting", "active", "interrupted")
ACTIVE_ATTEMPT_STATES = ("active", "pending")


def _now() -> datetime:
    return datetime.utcnow()


def _batch_row(session: Session, identity_hash: str):
    return session.query(Batch).filter_by(identity_hash=identity_hash).one_or_none()


def _item_row(session: Session, identity_hash: str, item_id: str):
    return (
        session.query(BatchItem)
        .filter_by(identity_hash=_item_identity(identity_hash, item_id))
        .one_or_none()
    )


def _item_identity(batch_identity_hash: str, item_id: str) -> str:
    return canonical_hash({"batch": batch_identity_hash, "item": item_id})


def submit_batch(session: Session, snapshot: dict, *, app_epoch: str = "api") -> dict:
    """Legt Snapshot-Revision samt Elementen an oder bestaetigt vorhandenes."""
    snap_hash = compute_snapshot_hash(snapshot)
    identity_hash = canonical_hash(
        {"batch_id": snapshot["batch_id"], "snapshot_hash": snap_hash}
    )
    payload_hash = canonical_hash({"identity": identity_hash, "snapshot": snapshot})
    row = _batch_row(session, identity_hash)
    item_ids = [i["item_id"] for i in snapshot["items"]]
    if row is not None:
        outcome = "existing" if row.payload_hash == payload_hash else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "snapshot_hash": snap_hash,
            "status": row.status,
            "item_count": len(item_ids),
            "item_ids": item_ids,
        }

    row = Batch(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=payload_hash,
        batch_id=snapshot["batch_id"],
        contract_version=snapshot["contract_version"],
        revision_no=int(snapshot["revision_no"]),
        parent_snapshot_hash=snapshot["parent_snapshot_hash"],
        snapshot_hash=snap_hash,
        snapshot=dict(snapshot),
        profile=dict(snapshot["profile"]),
        profile_hash=snapshot["profile_hash"],
        phases=list(snapshot["phases"]),
        partial_failure_policy=snapshot["partial_failure_policy"],
        resource_policy=dict(snapshot["resource_policy"]),
        output_policy=dict(snapshot["output_policy"]),
        frozen_order=list(snapshot["order"]),
        status="ready",
        app_epoch=app_epoch,
    )
    session.add(row)
    for item in snapshot["items"]:
        session.add(
            BatchItem(
                id=str(uuid.uuid4()),
                identity_hash=_item_identity(identity_hash, item["item_id"]),
                payload_hash=canonical_hash(item),
                batch_identity_hash=identity_hash,
                batch_id=snapshot["batch_id"],
                snapshot_hash=snap_hash,
                item_id=item["item_id"],
                order_index=int(item["order_index"]),
                source=dict(item["source"]),
                relative_path=item["relative_path"],
                selection_refs=list(item["selection_refs"]),
                profile_hash=snapshot["profile_hash"],
                status="waiting",
                result_refs={},
                warnings=[],
                output=dict(item["output"]),
                attempt_count=0,
            )
        )
    session.commit()
    return {
        "outcome": "created",
        "identity_hash": identity_hash,
        "snapshot_hash": snap_hash,
        "status": "ready",
        "item_count": len(item_ids),
        "item_ids": item_ids,
    }


def _transition_batch(session: Session, identity_hash: str, target: str,
                      reason_code: str | None = None) -> bool:
    row = _batch_row(session, identity_hash)
    if row is None or not can_transition("batch", row.status, target):
        return False
    row.status = target
    row.reason_code = reason_code
    if target in ("completed", "completed_with_issues", "canceled", "blocked"):
        row.terminal_at = _now()
    row.updated_at = _now()
    session.commit()
    return True


def start_batch(session: Session, identity_hash: str) -> str:
    if _transition_batch(session, identity_hash, "running", None):
        return "running"
    return "not_startable"


def pause_batch(session: Session, identity_hash: str) -> str:
    """Pausier-Anforderung: sofort sichtbar; ``paused`` erst am sicheren Checkpoint.

    Solange ein Element aktiv arbeitet, meldet der Batch ``pausing``; der
    Wechsel nach ``paused`` erfolgt erst, wenn kein aktives Element mehr
    verbleibt (Finalisierung/Abbruch am naechsten sicheren Punkt).
    """
    row = _batch_row(session, identity_hash)
    if row is None:
        return "not_found"
    if row.status not in ("running", "pausing"):
        return "not_active"
    session.flush()  # zaehlt gegen den frischen Stand, auch ohne Autoflush
    active = (
        session.query(BatchItem)
        .filter_by(batch_identity_hash=identity_hash, status="active")
        .count()
    )
    row.status = "pausing" if active else "paused"
    row.reason_code = "pausiert"
    row.updated_at = _now()
    session.commit()
    return row.status


def _settle_pause(session: Session, batch_identity_hash: str) -> None:
    """``pausing`` laeuft zu ``paused`` aus, sobald kein Element mehr aktiv ist."""
    row = _batch_row(session, batch_identity_hash)
    if row is None or row.status != "pausing":
        return
    session.flush()  # Elementwechsel sind sofort sichtbar, auch ohne Autoflush
    still_active = (
        session.query(BatchItem)
        .filter_by(batch_identity_hash=batch_identity_hash, status="active")
        .count()
    )
    if still_active == 0:
        row.status = "paused"
        row.updated_at = _now()


def resume_batch(session: Session, identity_hash: str, *,
                 confirmed_original: bool) -> str:
    """Wiederaufnahme nur nach ausdruecklicher Bestaetigung des Originalvertrags."""
    if not confirmed_original:
        return "nicht_bestaetigt"
    if _transition_batch(session, identity_hash, "running", None):
        return "running"
    return "not_startable"


def cancel_batch(session: Session, identity_hash: str) -> str:
    if _transition_batch(session, identity_hash, "canceled", "nutzerabbruch"):
        return "canceled"
    return "not_active"


def begin_item_attempt(session: Session, identity_hash: str, item_id: str, *,
                       app_epoch: str) -> dict:
    """Hoechstens ein aktiver Versuch je Element; wartende/interrupted starten."""
    item = _item_row(session, identity_hash, item_id)
    if item is None or item.status not in ACTIVE_ITEM_STATES:
        return {"outcome": "not_startable", "attempt_id": None}
    active = (
        session.query(BatchAttempt)
        .filter_by(batch_identity_hash=identity_hash, item_id=item_id)
        .filter(BatchAttempt.status.in_(ACTIVE_ATTEMPT_STATES))
        .one_or_none()
    )
    if active is not None:
        if active.status == "pending":
            result = session.execute(
                update(BatchAttempt)
                .where(BatchAttempt.id == active.id,
                       BatchAttempt.status == "pending")
                .values(status="active", app_epoch=app_epoch, updated_at=_now())
            )
            item.status = "active"
            item.attempt_count = int(item.attempt_count or 0)
            item.updated_at = _now()
            session.commit()
            if (result.rowcount or 0) > 0:
                return {"outcome": "started", "attempt_id": active.attempt_id}
        return {"outcome": "not_startable", "attempt_id": None}
    attempt_no = (
        session.query(BatchAttempt)
        .filter_by(batch_identity_hash=identity_hash, item_id=item_id)
        .count()
        + 1
    )
    attempt_id = new_attempt_id()
    session.add(
        BatchAttempt(
            id=str(uuid.uuid4()),
            attempt_id=attempt_id,
            batch_identity_hash=identity_hash,
            batch_id=item.batch_id,
            item_id=item_id,
            attempt_no=attempt_no,
            status="active",
            input_revisions={
                "snapshot_hash": item.snapshot_hash,
                "profile_hash": item.profile_hash,
                "source": dict(item.source),
            },
            reused_commits={},
            app_epoch=app_epoch,
        )
    )
    item.status = "active"
    item.attempt_count = attempt_no
    item.updated_at = _now()
    session.commit()
    return {"outcome": "started", "attempt_id": attempt_id}


def record_phase_commit(session: Session, attempt_id: str, phase: str,
                        commit_ref: dict) -> bool:
    """Haelt die autoritative Ergebnisreferenz einer Phase fest (bleibt erhalten)."""
    att = session.query(BatchAttempt).filter_by(attempt_id=attempt_id).one_or_none()
    if att is None:
        return False
    item = _item_row(session, att.batch_identity_hash, att.item_id)
    if item is None:
        return False
    refs = dict(item.result_refs or {})
    refs[phase] = dict(commit_ref)
    item.result_refs = refs
    item.current_phase = phase
    item.updated_at = _now()
    session.commit()
    return True


def finalize_item_attempt(session: Session, attempt_id: str, end_state: str, *,
                          reason_code: str | None = None,
                          phase_states: dict | None = None) -> CommitOutcome:
    """Genau ein terminaler Ausgang pro Versuch (bedingt, Exactly-once)."""
    att = session.query(BatchAttempt).filter_by(attempt_id=attempt_id).one_or_none()
    if att is None:
        return "not_found"
    result = session.execute(
        update(BatchAttempt)
        .where(
            BatchAttempt.attempt_id == attempt_id,
            BatchAttempt.status.in_(ACTIVE_ATTEMPT_STATES),
        )
        .values(
            status=end_state,
            phase_states=dict(phase_states or {}),
            reason_code=reason_code,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    if (result.rowcount or 0) > 0:
        item = _item_row(session, att.batch_identity_hash, att.item_id)
        if item is not None and item.status in ("active", "waiting"):
            item.status = end_state
            item.phases = dict(phase_states or {})
            item.reason_code = reason_code
            item.terminal_at = _now()
            item.updated_at = _now()
        _settle_pause(session, att.batch_identity_hash)
        session.commit()
        return "finalized"
    session.commit()
    return "already_terminal"


def cancel_attempt(session: Session, attempt_id: str) -> str:
    """Bestaetigter Abbruch; nach terminalem Commit sichtbar „zu spaet" (Race)."""
    att = session.query(BatchAttempt).filter_by(attempt_id=attempt_id).one_or_none()
    if att is None:
        return "not_found"
    result = session.execute(
        update(BatchAttempt)
        .where(
            BatchAttempt.attempt_id == attempt_id,
            BatchAttempt.status.in_(ACTIVE_ATTEMPT_STATES),
        )
        .values(status="canceled", reason_code="nutzerabbruch",
                terminal_at=_now(), updated_at=_now())
    )
    if (result.rowcount or 0) > 0:
        item = _item_row(session, att.batch_identity_hash, att.item_id)
        if item is not None and item.status in ("active", "waiting"):
            item.status = "canceled"
            item.reason_code = "nutzerabbruch"
            item.terminal_at = _now()
            item.updated_at = _now()
        _settle_pause(session, att.batch_identity_hash)
        session.commit()
        return "canceled"
    session.commit()
    return "too_late"


def cancel_item(session: Session, identity_hash: str, item_id: str) -> str:
    """Wartendes Element abbrechen: startet nicht und beeinflusst kein anderes."""
    item = _item_row(session, identity_hash, item_id)
    if item is None:
        return "not_found"
    result = session.execute(
        update(BatchItem)
        .where(
            BatchItem.identity_hash == _item_identity(identity_hash, item_id),
            BatchItem.status == "waiting",
        )
        .values(status="canceled", reason_code="nutzerabbruch",
                terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    return "canceled" if (result.rowcount or 0) > 0 else "not_active"


def retry_item(session: Session, identity_hash: str, item_id: str, *,
               reused_phases: tuple = (), rerun_phases: tuple = (),
               reason_code: str | None = None) -> dict:
    """Neuer Versuch mit wiederverwendeten Commits; gesicherte Ergebnisse bleiben."""
    item = _item_row(session, identity_hash, item_id)
    if item is None:
        return {"outcome": "not_found"}
    if item.status not in ("failed", "blocked", "canceled", "interrupted",
                           "invalidated", "succeeded_with_warnings"):
        return {"outcome": "not_retryable"}
    refs = dict(item.result_refs or {})
    reused = {p: refs[p] for p in reused_phases if p in refs}
    attempt_no = (
        session.query(BatchAttempt)
        .filter_by(batch_identity_hash=identity_hash, item_id=item_id)
        .count()
        + 1
    )
    session.add(
        BatchAttempt(
            id=str(uuid.uuid4()),
            attempt_id=new_attempt_id(),
            batch_identity_hash=identity_hash,
            batch_id=item.batch_id,
            item_id=item_id,
            attempt_no=attempt_no,
            status="pending",
            input_revisions={
                "snapshot_hash": item.snapshot_hash,
                "profile_hash": item.profile_hash,
                "source": dict(item.source),
            },
            reused_commits=reused,
            phase_states={"wiederholen": list(rerun_phases)},
            reason_code=reason_code,
        )
    )
    item.status = "waiting"
    item.reason_code = None
    item.result_refs = refs
    item.attempt_count = attempt_no
    item.terminal_at = None
    item.updated_at = _now()
    session.commit()
    return {"outcome": "retry_scheduled", "reused_commits": reused,
            "rerun_phases": list(rerun_phases)}


def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: aktive Versuche fremder Epoche werden ``interrupted``."""
    stale = (
        session.query(BatchAttempt)
        .filter(
            BatchAttempt.status.in_(ACTIVE_ATTEMPT_STATES),
            or_(BatchAttempt.app_epoch != current_epoch, BatchAttempt.app_epoch.is_(None)),
        )
        .all()
    )
    for att in stale:
        att.status = "interrupted"
        att.reason_code = "unterbrochen"
        att.terminal_at = _now()
        att.updated_at = _now()
        item = _item_row(session, att.batch_identity_hash, att.item_id)
        if item is not None and item.status in ("active", "waiting"):
            item.status = "interrupted"
            item.reason_code = "unterbrochen"
            item.updated_at = _now()
        row = _batch_row(session, att.batch_identity_hash)
        if row is not None and row.status in ("running", "pausing"):
            row.status = "paused"
            row.reason_code = "unterbrochen"
            row.updated_at = _now()
    session.commit()
    return len(stale)


def finalize_batch(session: Session, identity_hash: str) -> str:
    """Ableitung des Gesamtzustands ausschliesslich aus den Einzelelementen."""
    row = _batch_row(session, identity_hash)
    if row is None:
        return "not_found"
    items = (
        session.query(BatchItem)
        .filter_by(batch_identity_hash=identity_hash)
        .order_by(BatchItem.order_index)
        .all()
    )
    agg = aggregate_batch([i.status for i in items])
    row.aggregates = agg
    target = agg["status"]
    if can_transition("batch", row.status, target):
        row.status = target
        row.terminal_at = _now()
    row.updated_at = _now()
    session.commit()
    return row.status


def get_batch(session: Session, identity_hash: str) -> dict | None:
    row = _batch_row(session, identity_hash)
    if row is None:
        return None
    items = (
        session.query(BatchItem)
        .filter_by(batch_identity_hash=identity_hash)
        .order_by(BatchItem.order_index)
        .all()
    )
    attempts = (
        session.query(BatchAttempt)
        .filter_by(batch_identity_hash=identity_hash)
        .order_by(BatchAttempt.attempt_no, BatchAttempt.created_at)
        .all()
    )
    return {
        "identity_hash": row.identity_hash,
        "batch_id": row.batch_id,
        "snapshot_hash": row.snapshot_hash,
        "revision_no": row.revision_no,
        "status": row.status,
        "reason_code": row.reason_code,
        "phases": list(row.phases or []),
        "partial_failure_policy": row.partial_failure_policy,
        "aggregates": aggregate_batch([i.status for i in items]),
        "items": [
            {
                "item_id": i.item_id,
                "order_index": i.order_index,
                "status": i.status,
                "current_phase": i.current_phase,
                "phases": dict(i.phases or {}),
                "result_refs": dict(i.result_refs or {}),
                "warnings": list(i.warnings or []),
                "reason_code": i.reason_code,
                "source_path": (i.source or {}).get("path"),
                "output": dict(i.output or {}),
            }
            for i in items
        ],
        "attempts": [
            {
                "attempt_id": a.attempt_id,
                "item_id": a.item_id,
                "attempt_no": a.attempt_no,
                "status": a.status,
                "reused_commits": dict(a.reused_commits or {}),
                "phase_states": dict(a.phase_states or {}),
                "reason_code": a.reason_code,
            }
            for a in attempts
        ],
    }
