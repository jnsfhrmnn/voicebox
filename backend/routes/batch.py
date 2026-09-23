"""JFW-5: Batch-Router `/batch/*` — Auswahl, Snapshot, Lebenszyklus, Elemente.

Vertragspartner: ``backend/batch/*`` (I/O-freier Kern) und
``services/batch_contract.py`` (einzige Schreibstelle). Kein Dateiimport, kein
Verarbeitungsstart und keine Endausgabe ohne ausdrueckliche Bestaetigung eines
unveraenderlichen Snapshots. Start/Pause/Resume/Abbruch sind explizite
Nutzeraktionen; Resume ist fail-closed ohne Rekonstruktion aus globalen
Defaults (``confirmed_original``).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..batch.discovery import discover
from ..batch.identity import new_batch_id
from ..batch.output import resolve_conflicts
from ..batch.pipeline import run_item as run_element_pipeline
from ..batch.profile import phases_for, validate_profile
from ..batch.provenance import canonical_hash
from ..batch.providers.local_discovery import LocalDiscoveryFs
from ..batch.providers.phase_executors import default_executors
from ..batch.retry import plan_retry
from ..batch.snapshot import (
    build_snapshot,
    verify_snapshot_completeness,
    verify_source_binding,
)
from ..database import get_db
from ..database.models import Batch, BatchItem
from ..services import batch_contract as store

router = APIRouter()

TERMINAL_ITEM_STATES = (
    "succeeded", "succeeded_with_warnings", "failed", "canceled",
    "blocked", "interrupted", "invalidated",
)


def app_epoch() -> str:
    return "jfw5-api"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SelectionRef(BaseModel):
    kind: Literal["file", "folder"] = "file"
    path: str


class DiscoverRequest(BaseModel):
    selection: list[SelectionRef]


class ConfirmRequest(BaseModel):
    selection: list[SelectionRef]
    profile: dict
    created_at: str | None = None


class ResumeRequest(BaseModel):
    confirmed_original: bool = False


class ReorderRequest(BaseModel):
    order: list[str]


def _selection(body) -> list[dict]:
    return [{"kind": s.kind, "path": s.path} for s in body.selection]


def _current_row(db: Session, batch_id: str) -> Batch:
    row = (
        db.query(Batch)
        .filter_by(batch_id=batch_id)
        .order_by(Batch.revision_no.desc())
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404,
                            detail={"reason_code": "unbekannter_batch"})
    return row


def _item_row(db: Session, row: Batch, item_id: str) -> BatchItem | None:
    return (
        db.query(BatchItem)
        .filter_by(batch_identity_hash=row.identity_hash, item_id=item_id)
        .one_or_none()
    )


def _actions(status: str) -> list[str]:
    return {
        "ready": ["start", "cancel"],
        "running": ["pause", "cancel"],
        "pausing": ["cancel"],
        "paused": ["resume", "cancel"],
    }.get(status, [])


@router.post("/batch/discover")
def discover_batch(body: DiscoverRequest, db: Session = Depends(get_db)):
    """Fundmenge als Vorschau — schreibt nichts und startet nichts."""
    return discover(_selection(body), LocalDiscoveryFs())


@router.post("/batch/confirm")
def confirm_batch(body: ConfirmRequest, db: Session = Depends(get_db)):
    """Bestaetigte Vorschau wird zu GENAU EINER unveraenderlichen Snapshot-Revision."""
    errors = validate_profile(body.profile)
    if errors:
        raise HTTPException(status_code=400,
                            detail={"reason_code": "profil_ungueltig", "errors": errors})
    disc = discover(_selection(body), LocalDiscoveryFs())
    if not disc["elements"]:
        raise HTTPException(status_code=400,
                            detail={"reason_code": "keine_elemente"})
    batch_id = new_batch_id()
    snap = build_snapshot(
        batch_id=batch_id,
        discovery=disc,
        profile=dict(body.profile),
        revision_no=1,
        parent_snapshot_hash=None,
        created_at=body.created_at or _now_iso(),
    )
    incomplete = verify_snapshot_completeness(snap)
    if incomplete:
        raise HTTPException(status_code=400,
                            detail={"reason_code": "snapshot_unvollstaendig",
                                    "errors": incomplete})
    # Kollisionen vor Start sichtbar machen und ueber die bestaetigte Regel
    # loesen — nie ueberschreiben (belegte Ziele prueft der Pre-Write-Riegel).
    resolved = resolve_conflicts(snap["items"], dict(body.profile["output_policy"]),
                                 existing_targets=())
    if not resolved["ok"]:
        raise HTTPException(status_code=409,
                            detail={"reason_code": resolved["reason_code"]})
    for item, assignment in zip(snap["items"], resolved["assignments"], strict=False):
        item["output"] = dict(assignment["output"])
    out = store.submit_batch(db, snap, app_epoch=app_epoch())
    return {
        "batch_id": batch_id,
        "revision_no": snap["revision_no"],
        "created_at": snap["created_at"],
        "order": list(snap["order"]),
        "phases": list(snap["phases"]),
        "trust_boundary_paths": list(snap["trust_boundary_paths"]),
        "excluded": list(snap["excluded"]),
        "duplicate_groups": list(snap["duplicate_groups"]),
        **out,
    }


@router.post("/batch/{batch_id}/start")
def start_batch(batch_id: str, db: Session = Depends(get_db)):
    row = _current_row(db, batch_id)
    result = store.start_batch(db, row.identity_hash)
    if result == "running":
        return {"batch_id": batch_id, "status": "running"}
    return {"batch_id": batch_id, "outcome": "not_startable",
            "status": store.get_batch(db, row.identity_hash)["status"]}


@router.post("/batch/{batch_id}/pause")
def pause_batch(batch_id: str, db: Session = Depends(get_db)):
    row = _current_row(db, batch_id)
    return {"batch_id": batch_id, "status": store.pause_batch(db, row.identity_hash)}


@router.post("/batch/{batch_id}/resume")
def resume_batch(batch_id: str, body: ResumeRequest, db: Session = Depends(get_db)):
    """Wiederaufnahme nur mit ausdruecklicher Bestaetigung des Originalvertrags."""
    row = _current_row(db, batch_id)
    result = store.resume_batch(db, row.identity_hash,
                                confirmed_original=body.confirmed_original)
    return {"batch_id": batch_id, "outcome": result}


@router.post("/batch/{batch_id}/cancel")
def cancel_batch(batch_id: str, db: Session = Depends(get_db)):
    row = _current_row(db, batch_id)
    return {"batch_id": batch_id, "outcome": store.cancel_batch(db, row.identity_hash)}


@router.post("/batch/{batch_id}/reorder")
def reorder_items(batch_id: str, body: ReorderRequest, db: Session = Depends(get_db)):
    """Neue nachvollziehbare Reihenfolgerevision; laufende/gesicherte Zuordnungen bleiben."""
    row = _current_row(db, batch_id)
    detail = store.get_batch(db, row.identity_hash)
    current_order = [i["item_id"] for i in detail["items"]]
    requested = list(body.order)
    if sorted(requested) != sorted(current_order):
        raise HTTPException(status_code=400,
                            detail={"reason_code": "reihenfolge_ungueltig"})
    fixed = {i["item_id"]: i["order_index"] for i in detail["items"]
             if i["status"] != "waiting"}
    free_slots = sorted(set(range(len(current_order))) - set(fixed.values()))
    remaining = [iid for iid in requested if iid not in fixed]
    final_order: list = [None] * len(current_order)
    for iid, idx in fixed.items():
        final_order[idx] = iid
    for slot, iid in zip(free_slots, remaining, strict=False):
        final_order[slot] = iid

    snap = dict(row.snapshot)
    items_by_id = {i["item_id"]: dict(i) for i in snap["items"]}
    new_items = []
    for idx, iid in enumerate(final_order):
        item = dict(items_by_id[iid])
        item["order_index"] = idx
        new_items.append(item)
    snap["items"] = new_items
    snap["order"] = list(final_order)
    snap["order_revision"] = canonical_hash({"order": final_order})
    snap["revision_no"] = int(row.revision_no) + 1
    snap["parent_snapshot_hash"] = row.snapshot_hash
    snap["created_at"] = _now_iso()
    out = store.submit_batch(db, snap, app_epoch=app_epoch())
    return {
        "batch_id": batch_id,
        "outcome": "new_revision" if out["outcome"] == "created" else out["outcome"],
        "revision_no": snap["revision_no"],
        "snapshot_hash": out["snapshot_hash"],
        "order": list(final_order),
    }


@router.post("/batch/{batch_id}/items/{item_id}/run")
def run_item(batch_id: str, item_id: str, db: Session = Depends(get_db)):
    """Laedt ein Element durch die bestaetigte Phasenreihenfolge (Serie).

    Jeder autoritative Phasen-Commit wird sofort persistiert — Fehler, Abbruch
    oder ein spaeteres Element lassen gesicherte Ergebnisse unangetastet.
    """
    row = _current_row(db, batch_id)
    item_row = _item_row(db, row, item_id)
    if item_row is None:
        raise HTTPException(status_code=404,
                            detail={"reason_code": "element_unbekannt"})
    source = dict(item_row.source or {})
    binding = verify_source_binding({"source": source}, LocalDiscoveryFs())
    if not binding["ok"]:
        begun = store.begin_item_attempt(db, row.identity_hash, item_id,
                                         app_epoch=app_epoch())
        if begun.get("attempt_id"):
            store.finalize_item_attempt(db, begun["attempt_id"], binding["state"],
                                        reason_code=binding["reason_code"],
                                        phase_states={})
        return {"item_id": item_id, "profile_hash": row.profile_hash,
                "end_state": binding["state"], "reason_code": binding["reason_code"],
                "phases": {}, "commits": {}, "warnings": []}
    begun = store.begin_item_attempt(db, row.identity_hash, item_id,
                                     app_epoch=app_epoch())
    if begun["outcome"] != "started":
        return {"item_id": item_id, "profile_hash": row.profile_hash,
                "end_state": "blocked", "reason_code": "nicht_startbereit",
                "phases": {}, "commits": {}, "warnings": []}
    attempt_id = begun["attempt_id"]
    execs = default_executors(session=db, app_epoch=app_epoch())

    def guarded(phase, ctx):
        outcome = execs[phase](phase, ctx)
        if getattr(outcome, "commit_ref", None):
            store.record_phase_commit(db, attempt_id, phase, outcome.commit_ref)
        return outcome

    result = run_element_pipeline(
        item={
            "item_id": item_id,
            "source": source,
            "relative_path": item_row.relative_path,
            "output": dict(item_row.output or {}),
        },
        profile=dict(row.profile),
        executors={phase: guarded for phase in execs},
    )
    store.finalize_item_attempt(
        db, attempt_id, result["end_state"],
        reason_code=None,
        phase_states={p: info["status"] for p, info in result["phases"].items()},
    )
    # Sichtbarer Fail-fast-Modus (Profiloption, nie still Standard): nach dem
    # ersten Fehlerhaften Element wird der Batch angehalten statt weiterzulaufen.
    if dict(row.profile or {}).get("fail_fast") and result["end_state"] in (
        "failed", "invalidated",
    ):
        result["fail_fast_paused"] = store.pause_batch(db, row.identity_hash) in (
            "paused", "pausing",
        )
    states = [i["status"] for i in store.get_batch(db, row.identity_hash)["items"]]
    if states and all(s in TERMINAL_ITEM_STATES for s in states):
        store.finalize_batch(db, row.identity_hash)
    return result


@router.post("/batch/{batch_id}/items/{item_id}/cancel")
def cancel_item(batch_id: str, item_id: str, db: Session = Depends(get_db)):
    """Wartendes Element abbricht, ohne zu starten; andere Elemente bleiben unberuehrt."""
    row = _current_row(db, batch_id)
    return {"batch_id": batch_id, "item_id": item_id,
            "outcome": store.cancel_item(db, row.identity_hash, item_id)}


@router.post("/batch/{batch_id}/items/{item_id}/retry")
def retry_item(batch_id: str, item_id: str, db: Session = Depends(get_db)):
    """Zeigt wiederverwendbare Commits und wiederholte Phasen vor dem Neustart."""
    row = _current_row(db, batch_id)
    item_row = _item_row(db, row, item_id)
    if item_row is None:
        raise HTTPException(status_code=404,
                            detail={"reason_code": "element_unbekannt"})
    binding = verify_source_binding({"source": dict(item_row.source or {})},
                                    LocalDiscoveryFs())
    plan = plan_retry(
        item_state={
            "phases": dict(item_row.phases or {}),
            "commits": dict(item_row.result_refs or {}),
        },
        profile=dict(row.profile),
        snapshot_profile_hash=row.profile_hash,
        source_ok=binding["ok"],
    )
    if not plan.allowed:
        return {"batch_id": batch_id, "item_id": item_id,
                "outcome": "neue_revision_notwendig",
                "reason_code": plan.reason_code}
    rerun = tuple(plan.rerun_phases) or tuple(phases_for(dict(row.profile)))
    result = store.retry_item(db, row.identity_hash, item_id,
                              reused_phases=tuple(plan.reused_phases),
                              rerun_phases=rerun,
                              reason_code=None)
    return {"batch_id": batch_id, "item_id": item_id,
            "outcome": result["outcome"],
            "reused_phases": list(plan.reused_phases),
            "rerun_phases": list(rerun)}


@router.get("/batch/{batch_id}")
def get_batch(batch_id: str, db: Session = Depends(get_db)):
    row = _current_row(db, batch_id)
    detail = store.get_batch(db, row.identity_hash)
    snap = dict(row.snapshot or {})
    detail.update(
        {
            "profile_hash": row.profile_hash,
            "created_at": snap.get("created_at"),
            "order_revision": snap.get("order_revision"),
            "trust_boundary_paths": list(snap.get("trust_boundary_paths") or []),
            "resource_policy": dict(row.resource_policy or {}),
            "output_policy": dict(row.output_policy or {}),
            "available_actions": _actions(row.status),
        }
    )
    return detail


@router.get("/batch/{batch_id}/items/{item_id}")
def get_item(batch_id: str, item_id: str, db: Session = Depends(get_db)):
    row = _current_row(db, batch_id)
    for item in store.get_batch(db, row.identity_hash)["items"]:
        if item["item_id"] == item_id:
            return item
    raise HTTPException(status_code=404,
                        detail={"reason_code": "element_unbekannt"})
