"""JFW-13: Protokoll-Endpunkte (lokale API, Datei-/Meeting-Profil).

Ausdruecklich gestartete Protokollauftraege fuer Jobs mit gueltigem
``jfw4_export_v1``-Snapshot. ``prepare`` bindet fail-closed und erzeugt den
Pseudonym-Registerentwurf (Pruefansicht); erst die bestaetigte Registerfassung
laesst ``run`` die Endausgabe bilden. ``register/delete`` entfernt die
Zuordnungsinformation; offene Exportdialoge an die geloeschte Registerrevision
werden fuer jede weitere Ausgabe abgelehnt (AC 74).
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..minutes.artifacts import ArtifactGateError
from ..minutes.contract import outcome_state
from ..minutes.document import build_document, nondeterminism_revision
from ..minutes.engine import generate_sections
from ..minutes.input import bind_input
from ..minutes.provenance import DEFAULT_PROFILE, MinutesRequest, minutes_key
from ..minutes.providers.local_llm import (
    DECODE_PROFILE,
    LocalLLMProvider,
    MinutesProviderError,
)
from ..minutes.pseudonym import apply_action, confirm_register, propose_register
from ..services import minutes_contract as store

router = APIRouter()

#: Explizit uebergebene Modellvorschlaege tragen diese Provenienz (keine
#: Modell-Identitaetsbehauptung, wenn kein Modell gelaufen ist).
EXPLICIT_PROVENANCE = {
    "model_id": "vorschlaege_explizit_uebergeben",
    "decode_profile": DECODE_PROFILE,
}


class MinutesPrepareRequest(BaseModel):
    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int = Field(gt=0)
    timebase: str = "audio_ms_v1"
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    transcript_text_hash: str
    jfw2_result_hash: str
    jfw2_status: str
    jfw3_result_hash: str
    jfw3_status: str
    jfw4_export_key: str
    jfw4_result_hash: str
    register_revision: str = "vorbereitung"
    jfw11_commit_hash: str | None = None
    jfw11_status: str | None = None
    jfw11_expected: bool = False
    minutes_profile: str = DEFAULT_PROFILE
    target_dir: str | None = None
    document: dict | None = None
    candidates: list[dict] | None = None
    register_id: str | None = None


class MinutesRunRequest(MinutesPrepareRequest):
    proposals: dict | None = None
    rerun: bool = False


class RegisterActionRequest(BaseModel):
    actions: list[dict] = Field(default_factory=list)


class ExportCheckRequest(BaseModel):
    register_revision: str


def _to_request(body: MinutesPrepareRequest, register_revision: str | None) -> MinutesRequest:
    return MinutesRequest(
        job_id=body.job_id,
        audio_asset_id=body.audio_asset_id,
        audio_hash=body.audio_hash,
        audio_duration_ms=body.audio_duration_ms,
        timebase=body.timebase,
        transcript_run_id=body.transcript_run_id,
        transcript_revision_id=body.transcript_revision_id,
        transcript_revision_hash=body.transcript_revision_hash,
        transcript_text_hash=body.transcript_text_hash,
        jfw2_result_hash=body.jfw2_result_hash,
        jfw2_status=body.jfw2_status,
        jfw3_result_hash=body.jfw3_result_hash,
        jfw3_status=body.jfw3_status,
        jfw4_export_key=body.jfw4_export_key,
        jfw4_result_hash=body.jfw4_result_hash,
        register_revision=register_revision or body.register_revision,
        jfw11_commit_hash=body.jfw11_commit_hash,
        jfw11_status=body.jfw11_status,
        jfw11_expected=body.jfw11_expected,
        minutes_profile=body.minutes_profile,
        target_dir=body.target_dir,
    )


def _register_id(body: MinutesPrepareRequest) -> str:
    return body.register_id or f"jfw13-reg-{body.job_id}"


class _ProposalProviders:
    """Explizit uebergebene Modellvorschlaege als injizierbare Provider."""

    def __init__(self, proposals: dict):
        self.proposals = proposals or {}

    def propose_tasks(self, text, turns):
        return self.proposals.get("tasks") or []

    def propose_summary(self, text, turns):
        return self.proposals.get("summary") or []

    def propose_datum(self, text):
        return self.proposals.get("datum")


def _artifact_dir() -> Path:
    root = os.environ.get("JFWHISPER_DATA_ROOT") or tempfile.gettempdir()
    return Path(root) / "models" / "minutes"


@router.post("/minutes/prepare")
def prepare_minutes(body: MinutesPrepareRequest, db: Session = Depends(get_db)):
    """Bindet den JFW-4-Snapshot fail-closed und erzeugt den Pseudonym-Entwurf.
    Bei ``blocked`` entsteht weder Register noch Auftrag."""
    request = _to_request(body, body.register_revision)
    inp = bind_input(request, body.document)
    if inp.readiness["state"] == "blocked":
        return {
            "outcome": "blocked",
            "reason_code": inp.reason_code,
            "readiness": inp.readiness,
        }
    register_id = _register_id(body)
    turn_order = {
        str(t.get("turn_id")): i
        for i, t in enumerate((body.document or {}).get("turns") or [])
    }
    register = propose_register(body.candidates or [], turn_order, register_id)
    store.save_register(db, register, minutes_key_value=None)
    return {
        "outcome": inp.readiness["state"],
        "register_id": register_id,
        "register": register,  # Pruefansicht (Zuordnung sichtbar, nie im Ergebnis)
        "warnings": [dict(w) for w in inp.warnings],
        "readiness": inp.readiness,
    }


@router.post("/minutes/register/{register_id}/confirm")
def confirm_register_view(register_id: str, body: RegisterActionRequest,
                          db: Session = Depends(get_db)):
    """Nutzeraktionen anwenden und die Fassung bestaetigen (neue Revision)."""
    head = store.latest_register(db, register_id)
    if head is None:
        raise HTTPException(status_code=404, detail="unknown_register")
    actions = body.get("actions", []) if isinstance(body, dict) else body.actions
    for action in actions:
        head = apply_action(head, action)
    head = confirm_register(head)
    store.save_register(db, head, minutes_key_value=None)
    return {"register_id": register_id, "register": head}


@router.post("/minutes/register/{register_id}/delete")
def delete_register_view(register_id: str, db: Session = Depends(get_db)):
    """Loescht die Zuordnungsinformation vollstaendig (Pseudonyme bleiben)."""
    head = store.latest_register(db, register_id)
    if head is None:
        raise HTTPException(status_code=404, detail="unknown_register")
    from ..minutes.pseudonym import delete_register
    deleted = delete_register(head)
    store.save_register(db, deleted, minutes_key_value=None)
    return {"register_id": register_id, "outcome": "geloescht",
            "register_revision": deleted["revision_id"]}


@router.get("/minutes/register/{register_id}")
def get_register_view(register_id: str, db: Session = Depends(get_db)):
    """Pruefansicht des Registers (nach Loeschung ohne Zuordnung)."""
    head = store.latest_register(db, register_id)
    if head is None:
        raise HTTPException(status_code=404, detail="unknown_register")
    return {"register_id": register_id, "register": head}


@router.post("/minutes/register/{register_id}/export-check")
def export_check(register_id: str, body: ExportCheckRequest,
                 db: Session = Depends(get_db)):
    """AC 74: eine weitere Ausgabe an die geloeschte Registerrevision ist
    fail-closed abgelehnt; die Antwort enthaelt nie eine Zuordnung."""
    bound = body["register_revision"] if isinstance(body, dict) else body.register_revision
    outcome = store.export_gate(db, register_id, bound)
    if outcome == "ok":
        return {"outcome": "ok"}
    return {"outcome": "abgelehnt", "reason_code": outcome}


@router.post("/minutes/run")
def run_minutes(body: MinutesRunRequest, db: Session = Depends(get_db)):
    """Erzeugt das Protokoll atomar aus dem gebundenen Snapshot. Ohne
    bestaetigte Registerfassung ``blocked``; ohne Modellartefakt
    ``waiting_for_local_artifact`` (kein Auto-Download). Identische erneute
    Einreichung liefert idempotent dasselbe autoritative Ergebnis; eine nicht
    reproduzierbare Modellantwort wird als gesondert versionierte
    Ergebnisrevision gefuehrt (nie als zweite Fassung)."""
    register_id = _register_id(body)
    head = store.latest_register(db, register_id)
    if head is None or head.get("status") != "bestaetigt":
        return {"outcome": "blocked", "reason_code": "register_unbestaetigt"}

    request = _to_request(body, head["revision_id"])
    inp = bind_input(request, body.document)
    if inp.readiness["state"] == "blocked":
        return {"outcome": "blocked", "reason_code": inp.reason_code,
                "readiness": inp.readiness}

    key = minutes_key(request)
    submitted = store.submit_minutes(db, request, inp.readiness,
                                     [dict(w) for w in inp.warnings], register_id)
    if submitted["outcome"] == "conflict":
        raise HTTPException(status_code=409, detail="minutes_conflict")
    if submitted["outcome"] == "existing" and not body.rerun:
        return {
            "outcome": "existing",
            "minutes_key": key,
            "status": submitted["status"],
            "result_hash": submitted["result_hash"],
        }

    provenance = dict(EXPLICIT_PROVENANCE)
    if body.proposals is None:
        provider = LocalLLMProvider(_artifact_dir())
        try:
            provenance = provider.load()
        except (ArtifactGateError, MinutesProviderError) as exc:
            store.mark_waiting(db, key, exc.reason_code)
            return {
                "outcome": "waiting_for_local_artifact",
                "minutes_key": key,
                "reason_code": exc.reason_code,
            }
        providers = provider
    else:
        providers = _ProposalProviders(body.proposals)

    attempt = store.begin_generation(db, key, app_epoch="api")
    if attempt is None and not body.rerun:
        return {"outcome": "not_active", "minutes_key": key,
                "status": submitted.get("status")}

    sections = generate_sections(inp, head, providers)
    built = build_document(request, dict(inp.document), head, sections,
                           provenance, attempt_id=attempt or "rerun")

    authoritative = store.get_minutes(db, key)
    if body.rerun and authoritative and authoritative.get("result_hash"):
        rev = nondeterminism_revision(
            authoritative.get("document") or {}, built["document"],
            revision_no=len(authoritative.get("nondeterminism_revisions") or []) + 1)
        if rev is None:
            return {"outcome": "reproduziert", "minutes_key": key,
                    "result_hash": authoritative["result_hash"]}
        store.add_nondeterminism_revision(db, key, rev)
        return {
            "outcome": "nondeterminism_revision",
            "minutes_key": key,
            "result_hash": authoritative["result_hash"],  # autoritativ bleibt
            "nondeterminism_revision": rev,
        }

    outcome = store.commit_result(db, key, built["document"]["result_hash"],
                                  built["document"], built["warnings"], provenance)
    if outcome != "committed":
        return {"outcome": outcome, "minutes_key": key, "result_hash": None}
    final_state = outcome_state(built["warnings"])
    return {
        "outcome": final_state,
        "minutes_key": key,
        "status": final_state,
        "result_hash": built["document"]["result_hash"],
    }


@router.post("/minutes/{minutes_key}/cancel")
def cancel_minutes(minutes_key: str, db: Session = Depends(get_db)):
    """Bestaetigter Abbruch: endet ausschliesslich dieser Versuch als
    ``canceled``; nach dem Ergebnis-Commit sichtbar „zu spaet"."""
    outcome = store.cancel_generation(db, minutes_key)
    return {"minutes_key": minutes_key, "outcome": outcome}


@router.get("/minutes/{minutes_key}")
def get_minutes(minutes_key: str, db: Session = Depends(get_db)):
    """Status, Warnliste, Nondeterminismus-Revisionen und Ergebnis des Auftrags."""
    row = store.get_minutes(db, minutes_key)
    if row is None:
        raise HTTPException(status_code=404, detail="unknown_minutes_key")
    return row
