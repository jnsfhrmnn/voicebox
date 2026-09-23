"""JFW-7: Atomare Persistenz des Transkriptions-Runs (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``recording_contract.py``/``alignment_contract.py``:
genau ein terminaler Ausgang pro Attempt ueber bedingte DB-Transaktionen.

* ``submit_run``: Idempotenz ueber ``payload_hash`` (``existing``), fail-closed
  Provenienzkonflikt bei abweichendem Payload (``conflict``) — identische erneute
  Zustellung von Handoff + Snapshot erzeugt nie eine zweite autoritative Revision.
* ``start_attempt``: bindet ``(backend_variant, backend_generation)`` bis zum
  terminalen Attempt; unklare Health/Generation = ``waiting_for_backend`` (kein
  stiller Fallback), fehlendes Artefakt = ``waiting_for_model`` (kein Download);
  notwendiger Backendwechsel = sichtbarer neuer Attempt.
* ``commit_raw``: atomarer Rohtranskript-Commit — genau eine unveraenderte
  ``raw_transcript``-Revision pro Attempt (``source_attempt_id`` UNIQUE). Leerer
  Text ohne Segmente wird ``no_speech`` (kein leerer Erfolgstext).
* ``cancel_run``/``fail_run``/``invalidate_run``: Terminal-Race — der zuerst
  dauerhaft gespeicherte terminale Ausgang gewinnt; nach Cancel wird kein
  spaeteres Ergebnis autoritativ.
* ``save_user_edit``: getrennte ``user_edited``-Kindrevision; die Rohrevision
  bleibt unveraendert.
* ``retranscribe``: neuer Attempt-Zyklus — fruehere Revisionen bleiben erhalten.
* ``recover_interrupted``: unterbrochene Laeufe fremder Epoche werden
  kontrolliert ``queued`` (nie ein Teilergebnis als final).
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import TranscriptionRun, TranscriptRevision
from ..transcription.contract import TERMINAL_STATES, mark_attempt_outcome
from ..transcription.provenance import TranscriptionRequest
from ..transcription.raw_transcript import (
    RAW_KIND,
    USER_EDITED_KIND,
    build_raw_revision,
    has_speech,
    user_edited_revision,
)
from ..transcription.snapshot import MODEL_PROFILE_ID, snapshot_hash

#: Nichtterminale Zustaende des Processing Contracts.
NON_TERMINAL_STATES = ("queued", "waiting_for_backend", "waiting_for_model", "transcribing")
#: Zustand mit autoritativem Rohtranskript-Commit.
COMMITTED_STATE = "raw_ready"


def _now() -> datetime:
    return datetime.utcnow()


def _get(session: Session, identity_hash: str):
    return (
        session.query(TranscriptionRun)
        .filter_by(identity_hash=identity_hash)
        .one_or_none()
    )


def submit_run(session: Session, request: TranscriptionRequest) -> dict:
    """Legt den Run an oder liefert den vorhandenen Stand idempotent."""
    identity_hash = request.identity_hash()
    row = _get(session, identity_hash)
    if row is not None:
        outcome = "existing" if row.payload_hash == request.payload_hash() else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
            "result_hash": row.result_hash,
        }
    errors = request.snapshot_errors()
    if errors:
        return {
            "outcome": "snapshot_ungueltig",
            "identity_hash": identity_hash,
            "status": None,
            "result_hash": None,
            "errors": errors,
        }
    snap = dict(request.snapshot)
    row = TranscriptionRun(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=request.payload_hash(),
        run_id=request.run_id,
        source_kind=request.source_kind,
        contract_version=request.contract_version,
        audio_hash=request.audio_hash,
        manifest_hash=request.manifest_hash,
        capture_id=request.capture_id,
        snapshot=snap,
        snapshot_hash=snapshot_hash(snap),
        stt_model=snap.get("stt_model"),
        model_revision=snap.get("model_revision"),
        language_setting=snap.get("language_setting"),
        backend_variant=snap.get("backend_variant"),
        backend_generation=snap.get("backend_generation"),
        attempts=[],
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


def start_attempt(
    session: Session,
    identity_hash: str,
    *,
    backend_variant: str,
    backend_generation: int,
    model_ready: bool = True,
    backend_stable: bool = True,
    model_profile: str = MODEL_PROFILE_ID,
    app_epoch: str = "api",
) -> dict:
    """Bindet einen Attempt an ``(backend_variant, backend_generation)``."""
    row = _get(session, identity_hash)
    if row is None:
        return {"outcome": "not_found", "attempt_id": None, "status": None}
    if row.status in TERMINAL_STATES:
        return {"outcome": "already_terminal", "attempt_id": row.attempt_id, "status": row.status}
    attempts = [dict(a) for a in (row.attempts or [])]
    if not backend_stable:
        _set_waiting(session, row, "waiting_for_backend", "backend_unsicher")
        return {"outcome": "waiting_for_backend", "attempt_id": None, "status": "waiting_for_backend"}
    if not model_ready:
        _set_waiting(session, row, "waiting_for_model", "artefakt_fehlt")
        return {"outcome": "waiting_for_model", "attempt_id": None, "status": "waiting_for_model"}
    if row.status == "transcribing" and row.attempt_id:
        current = next(
            (
                a
                for a in reversed(attempts)
                if a.get("attempt_id") == row.attempt_id and a.get("outcome") is None
            ),
            None,
        )
        if current is not None:
            if current.get("backend_variant") == backend_variant and int(
                current.get("backend_generation")
            ) == int(backend_generation):
                return {
                    "outcome": "attempt_bereits_gebunden",
                    "attempt_id": row.attempt_id,
                    "status": row.status,
                }
            # Notwendiger Backendwechsel: sichtbarer neuer Attempt statt stiller Fortsetzung.
            attempts = mark_attempt_outcome(attempts, row.attempt_id, "superseded_by_switch")
    attempt_id = str(uuid.uuid4())
    attempts.append(
        {
            "attempt_id": attempt_id,
            "backend_variant": backend_variant,
            "backend_generation": int(backend_generation),
            "model_profile": model_profile,
            "outcome": None,
        }
    )
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status.notin_(TERMINAL_STATES),
        )
        .values(
            status="transcribing",
            attempt_id=attempt_id,
            attempts=attempts,
            backend_variant=backend_variant,
            backend_generation=int(backend_generation),
            app_epoch=app_epoch,
            reason_code=None,
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return {"outcome": "started", "attempt_id": attempt_id, "status": "transcribing"}
    row2 = _get(session, identity_hash)
    return {
        "outcome": "already_terminal",
        "attempt_id": None,
        "status": row2.status if row2 else None,
    }


def _set_waiting(session: Session, row, status: str, reason: str) -> None:
    session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == row.identity_hash,
            TranscriptionRun.status.notin_(TERMINAL_STATES),
        )
        .values(status=status, reason_code=reason, updated_at=_now())
    )
    session.commit()


def commit_raw(
    session: Session,
    identity_hash: str,
    *,
    text: str,
    segments: list | None = None,
    language_output: dict,
    model_provenance: dict | None = None,
    duration_ms: int | None = None,
) -> dict:
    """Atomarer Rohtranskript-Commit — genau eine unveraenderte Revision."""
    row = _get(session, identity_hash)
    if row is None:
        return {"outcome": "not_found"}
    if not has_speech(text, segments):
        # Kein sicherer Sprachinhalt: kein leerer/halluzinierter Erfolgstext.
        return record_no_speech(
            session, identity_hash, language_output=language_output,
            model_provenance=model_provenance,
        )
    rev_id = str(uuid.uuid4())
    prov_model = dict(model_provenance or {})
    prov_model.update(
        {
            "stt_model": row.stt_model,
            "model_revision": row.model_revision,
            "backend_variant": row.backend_variant,
            "backend_generation": row.backend_generation,
        }
    )
    rev = build_raw_revision(
        text=text,
        segments=segments,
        language_output=language_output,
        audio_hash=row.audio_hash,
        attempt_id=row.attempt_id or rev_id,
        model_provenance=prov_model,
    )
    lang = rev["language"] or {}
    attempts = mark_attempt_outcome(
        [dict(a) for a in (row.attempts or [])], row.attempt_id, "raw_ready"
    )
    # Reihenfolge fail-closed: erst der bedingte terminale Gewinner (atomares
    # Gate), dann die Revision in derselben Transaktion — sonst wuerde ein
    # Autoflush die Revision auch bei verlorenem Race einfuegen.
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status == "transcribing",
            TranscriptionRun.result_hash.is_(None),
        )
        .values(
            status=COMMITTED_STATE,
            revision_id=rev_id,
            text_hash=rev["text_hash"],
            result_hash=rev["text_hash"],
            attempts=attempts,
            duration_ms=duration_ms,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    if (result.rowcount or 0) > 0:
        session.add(
            TranscriptRevision(
                id=rev_id,
                source_attempt_id=row.attempt_id or rev_id,
                transcript_raw=rev["text"],
                stt_model=row.stt_model,
                language=lang.get("detected") or lang.get("setting"),
                duration_ms=duration_ms,
                revision_kind=RAW_KIND,
                parent_revision_id=None,
                text_hash=rev["text_hash"],
                segments=rev["segments"],
                provenance={**rev["provenance"], "language": rev["language"]},
                run_identity_hash=identity_hash,
            )
        )
        session.commit()
        return {
            "outcome": "committed",
            "identity_hash": identity_hash,
            "revision_id": rev_id,
            "text_hash": rev["text_hash"],
        }
    session.rollback()
    row2 = _get(session, identity_hash)
    if row2 is not None and row2.status == "canceled":
        return {"outcome": "canceled"}
    return {"outcome": "already_terminal"}


def record_no_speech(
    session: Session,
    identity_hash: str,
    *,
    language_output: dict | None = None,
    model_provenance: dict | None = None,
) -> dict:
    """Terminal ``no_speech`` — kein JFW-8-Payload, keine Revision."""
    row = _get(session, identity_hash)
    if row is None:
        return {"outcome": "not_found"}
    attempts = mark_attempt_outcome(
        [dict(a) for a in (row.attempts or [])], row.attempt_id, "no_speech"
    )
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status == "transcribing",
            TranscriptionRun.result_hash.is_(None),
        )
        .values(status="no_speech", attempts=attempts, terminal_at=_now(), updated_at=_now())
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return {"outcome": "no_speech", "identity_hash": identity_hash}
    row2 = _get(session, identity_hash)
    return {"outcome": "already_terminal", "status": row2.status if row2 else None}


def cancel_run(session: Session, identity_hash: str, *, reason_code: str = "nutzerabbruch") -> str:
    """Terminaler Abbruch — gewinnt den Race gegen den Commit, wenn zuerst dauerhaft."""
    row = _get(session, identity_hash)
    if row is None:
        return "not_active"
    if row.status == "canceled":
        return "already_terminal"
    if row.result_hash is not None or row.status == COMMITTED_STATE:
        return "too_late"
    attempts = mark_attempt_outcome(
        [dict(a) for a in (row.attempts or [])], row.attempt_id, "canceled"
    )
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status.notin_(TERMINAL_STATES),
            TranscriptionRun.result_hash.is_(None),
        )
        .values(
            status="canceled",
            reason_code=reason_code,
            attempts=attempts,
            cancel_requested_at=_now(),
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row2 = _get(session, identity_hash)
    if row2 is None:
        return "not_active"
    if row2.status == "canceled":
        return "already_terminal"
    return "too_late"


def fail_run(session: Session, identity_hash: str, reason_code: str) -> str:
    """Fehler-Transition OHNE Ergebnis (Audio und Diagnose bleiben; keine Rekonstruktion)."""
    row = _get(session, identity_hash)
    if row is None:
        return "not_found"
    attempts = mark_attempt_outcome(
        [dict(a) for a in (row.attempts or [])], row.attempt_id, "failed"
    )
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status.notin_(TERMINAL_STATES),
            TranscriptionRun.result_hash.is_(None),
        )
        .values(
            status="failed",
            reason_code=reason_code,
            attempts=attempts,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    return "failed" if (result.rowcount or 0) > 0 else "already_terminal"


def invalidate_run(session: Session, identity_hash: str, reason_code: str) -> str:
    """Audio oder Snapshot aenderte sich — neue Revision/Attempt erforderlich."""
    row = _get(session, identity_hash)
    if row is None:
        return "not_found"
    if row.status == "canceled":
        return "already_terminal"
    attempts = mark_attempt_outcome(
        [dict(a) for a in (row.attempts or [])], row.attempt_id, "invalidated"
    )
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status != "canceled",
        )
        .values(
            status="invalidated",
            reason_code=reason_code,
            attempts=attempts,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    return "invalidated" if (result.rowcount or 0) > 0 else "already_terminal"


def _latest_raw(session: Session, identity_hash: str):
    return (
        session.query(TranscriptRevision)
        .filter(
            TranscriptRevision.run_identity_hash == identity_hash,
            TranscriptRevision.revision_kind.in_((RAW_KIND, None)),
        )
        .order_by(TranscriptRevision.created_at.desc())
        .first()
    )


def save_user_edit(session: Session, identity_hash: str, new_text: str) -> dict:
    """Getrennte ``user_edited``-Kindrevision; ``raw_transcript`` bleibt unveraendert."""
    row = _get(session, identity_hash)
    if row is None:
        return {"outcome": "not_found"}
    raw = _latest_raw(session, identity_hash)
    if raw is None:
        return {"outcome": "keine_rawrevision"}
    kind = user_edited_revision(
        {
            "revision_id": raw.id,
            "revision_kind": raw.revision_kind or RAW_KIND,
            "text": raw.transcript_raw,
            "text_hash": raw.text_hash,
            "language": (raw.provenance or {}).get("language") or {},
            "provenance": raw.provenance or {},
        },
        new_text,
    )
    rev_id = str(uuid.uuid4())
    session.add(
        TranscriptRevision(
            id=rev_id,
            source_attempt_id=f"user-edit-{uuid.uuid4().hex}",
            transcript_raw=new_text,
            stt_model=raw.stt_model,
            language=raw.language,
            revision_kind=USER_EDITED_KIND,
            parent_revision_id=raw.id,
            text_hash=kind["text_hash"],
            segments=[],
            provenance={**kind["provenance"], "language": kind["language"]},
            run_identity_hash=identity_hash,
        )
    )
    session.commit()
    return {
        "outcome": "saved",
        "identity_hash": identity_hash,
        "revision_id": rev_id,
        "parent_revision_id": raw.id,
    }


def retranscribe(session: Session, identity_hash: str) -> dict:
    """Neuer Transkriptionszyklus; fruehere Revisionen bleiben erhalten."""
    row = _get(session, identity_hash)
    if row is None:
        return {"outcome": "not_found"}
    if row.status == "canceled":
        return {"outcome": "too_late", "identity_hash": identity_hash}
    if row.status not in ("raw_ready", "no_speech", "failed", "invalidated"):
        return {"outcome": "not_requed", "identity_hash": identity_hash}
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.identity_hash == identity_hash,
            TranscriptionRun.status == row.status,
        )
        .values(
            status="queued",
            attempt_id=None,
            revision_id=None,
            text_hash=None,
            result_hash=None,
            reason_code="retranscribe",
            terminal_at=None,
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return {"outcome": "requed", "identity_hash": identity_hash}
    return {"outcome": "not_requed", "identity_hash": identity_hash}


def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: unterbrochene Laeufe werden kontrolliert ``queued``.

    Nie ein Teilergebnis als final; der Attempt kann mit demselben Snapshot
    kontrolliert fortgesetzt oder neu gestartet werden.
    """
    result = session.execute(
        update(TranscriptionRun)
        .where(
            TranscriptionRun.status == "transcribing",
            or_(
                TranscriptionRun.app_epoch.is_(None),
                TranscriptionRun.app_epoch != current_epoch,
            ),
            TranscriptionRun.result_hash.is_(None),
        )
        .values(status="queued", attempt_id=None, reason_code="unterbrochen", updated_at=_now())
    )
    session.commit()
    return result.rowcount or 0


def get_state(session: Session, identity_hash: str) -> dict | None:
    """Run-Zustand inklusive autoritativer Rohrevision (inhaltsfrei bis auf Text)."""
    row = _get(session, identity_hash)
    if row is None:
        return None
    raw = _latest_raw(session, identity_hash)
    edit = (
        session.query(TranscriptRevision)
        .filter_by(run_identity_hash=identity_hash, revision_kind=USER_EDITED_KIND)
        .order_by(TranscriptRevision.created_at.desc())
        .first()
    )
    return {
        "identity_hash": row.identity_hash,
        "payload_hash": row.payload_hash,
        "run_id": row.run_id,
        "source_kind": row.source_kind,
        "contract_version": row.contract_version,
        "status": row.status,
        "reason_code": row.reason_code,
        "stop_reason": row.stop_reason,
        "snapshot_hash": row.snapshot_hash,
        "audio_hash": row.audio_hash,
        "attempt_id": row.attempt_id,
        "attempts": row.attempts or [],
        "revision_id": raw.id if raw else row.revision_id,
        "raw_text": raw.transcript_raw if raw else None,
        "text_hash": raw.text_hash if raw else row.text_hash,
        "user_edited_text": edit.transcript_raw if edit else None,
        "result_hash": row.result_hash,
    }
