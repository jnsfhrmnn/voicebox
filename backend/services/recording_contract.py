"""JFW-6: Atomare Persistenz des Aufnahme-Runs (einzige Schreibstelle).

Vertrag analog zu ``task_contract.py``/``alignment_contract.py``/
``diarization_contract.py``/``meeting_contract.py``: genau ein terminaler
Ausgang pro Attempt ueber bedingte DB-Transaktionen.

* ``submit_run``: Idempotenz ueber ``payload_hash`` (``existing``),
  fail-closed Provenienzkonflikt bei abweichendem Payload bei gleicher
  Identität (``conflict``) — Auto-Repeat/Mehrfachfeuer erzeugt nie einen
  zweiten Run.
* ``begin_recording``: genau einmalig ``starting → recording`` (kein zweiter
  Start ueber einem angenommenen Uebergang).
* ``accept_stop``: genau ein angenommener Stopp-Intent mit Ursache und
  monotonem Zeitpunkt (``already_stopped`` fuer Auto-Repeat).
* ``commit_result``: atomarer Ergebnis-Commit (``secured``) — Frame-Bilanz und
  Manifestvalidierung fail-closed VOR dem Schreiben. Verwerfen-/Fehler-Race:
  der zuerst dauerhaft gespeicherte terminale Ausgang gewinnt.
* ``discard_run``: ausdrueckliches Verwerfen nur mit Bestätigung und
  gebundenem Löschvertrag; erzeugt kein Transkriptions-Handoff.
* ``deliver_handoff``: idempotenter JFW-7-Handoff — identische erneute
  Zustellung setzt denselben Run fort (``existing``) und autorisiert keine
  zweite Transkription.
* ``recover_interrupted``: unterbrochene Laeufe fremder Epoche werden
  ``failed`` (``unterbrochen``) — nie als vollstaendig ausgegeben.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import or_, update
from sqlalchemy.orm import Session

from ..database.models import RecordingRun
from ..recording.contract import STOP_CAUSES
from ..recording.manifest import canonical_hash, validate_manifest
from ..recording.provenance import RecordingRequest
from ..recording.recovery import account_frames

#: Nichtterminale Zustaende, in denen ein Run aktiv ist.
NON_TERMINAL_STATES = ("starting", "recording", "stopping")
#: Zustand mit autoritativem Ergebnis-Commit.
COMMITTED_STATE = "secured"

CommitOutcome = Literal[
    "committed",
    "already_terminal",
    "canceled",
    "not_found",
    "frame_bilanz_unvollstaendig",
    "manifest_ungueltig",
]


def _now() -> datetime:
    return datetime.utcnow()


def submit_run(session: Session, request: RecordingRequest) -> dict:
    """Legt den Run an oder liefert den vorhandenen Stand idempotent."""
    identity_hash = request.identity_hash()
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None:
        outcome = "existing" if row.payload_hash == request.payload_hash() else "conflict"
        return {
            "outcome": outcome,
            "identity_hash": identity_hash,
            "status": row.status,
            "result_hash": row.result_hash,
        }

    row = RecordingRun(
        id=str(uuid.uuid4()),
        identity_hash=identity_hash,
        payload_hash=request.payload_hash(),
        run_id=request.run_id,
        contract_version=request.contract_version,
        device_stable_id_hash=request.device_stable_id_hash,
        format=dict(request.format),
        started_at_100ns=int(request.started_at_100ns),
        status="starting",
    )
    session.add(row)
    session.commit()
    return {
        "outcome": "created",
        "identity_hash": identity_hash,
        "status": "starting",
        "result_hash": None,
    }


def begin_recording(session: Session, identity_hash: str, app_epoch: str = "api") -> str | None:
    """Bedingt ``starting → recording``. Liefert die ``attempt_id`` oder None
    (kein zweiter Start bei bereits angenommenem Uebergang)."""
    attempt_id = str(uuid.uuid4())
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.status == "starting",
        )
        .values(
            status="recording",
            attempt_id=attempt_id,
            app_epoch=app_epoch,
            updated_at=_now(),
        )
    )
    session.commit()
    return attempt_id if (result.rowcount or 0) > 0 else None


def accept_stop(session: Session, identity_hash: str, cause: str, t_100ns: int) -> str:
    """Genau ein angenommener Stopp-Intent mit Ursache und monotonem Punkt.

    Liefert ``accepted`` / ``already_stopped`` (kein zweiter Abschluss) /
    ``zeitpunkt_nicht_monoton`` / ``stopgrund_unbekannt`` / ``not_active`` /
    ``not_found``.
    """
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    if row.stop_at_100ns is not None:
        return "already_stopped"
    if cause not in STOP_CAUSES:
        return "stopgrund_unbekannt"
    if row.status != "recording":
        return "not_active"
    if int(t_100ns) <= int(row.started_at_100ns):
        return "zeitpunkt_nicht_monoton"
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.status == "recording",
            RecordingRun.stop_at_100ns.is_(None),
        )
        .values(
            status="stopping",
            stop_cause=cause,
            stop_at_100ns=int(t_100ns),
            updated_at=_now(),
        )
    )
    session.commit()
    return "accepted" if (result.rowcount or 0) > 0 else "already_stopped"


def commit_result(session: Session, identity_hash: str, built: dict) -> CommitOutcome:
    """Atomarer Ergebnis-Commit (``secured``): Frame-Bilanz und Manifest
    validieren fail-closed vor dem Schreiben; genau ein terminaler Ausgang."""
    frames = built.get("frames") or {}
    bilanz = account_frames(
        accepted_frames=frames.get("accepted_frames", 0),
        final_frames=frames.get("final_frames", 0),
        gap_frames=frames.get("gap_frames", 0),
    )
    if not bilanz["bilanz_ok"]:
        return "frame_bilanz_unvollstaendig"
    manifest = built.get("manifest") or {}
    if validate_manifest(manifest):
        return "manifest_ungueltig"
    result_hash = canonical_hash(manifest)
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.status.in_(NON_TERMINAL_STATES),
            RecordingRun.result_hash.is_(None),
        )
        .values(
            status=COMMITTED_STATE,
            stop_reason=built.get("stop_reason"),
            audio_hash=built.get("audio_hash"),
            manifest_hash=result_hash,
            result_hash=result_hash,
            manifest=manifest,
            frames=bilanz,
            gaps=built.get("gaps") or [],
            sound_cue_marks=built.get("sound_cue_marks") or [],
            recovery_status=built.get("recovery_status") or {},
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "committed"
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    return "canceled" if row.status == "canceled" else "already_terminal"


def fail_attempt(session: Session, identity_hash: str, reason_code: str) -> bool:
    """Fehler-Transition OHNE Ergebnis-Commit (kein automatischer Neustart)."""
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.status.in_(NON_TERMINAL_STATES),
            RecordingRun.result_hash.is_(None),
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


def discard_run(
    session: Session,
    identity_hash: str,
    *,
    confirmed: bool,
    deletion_contract_hash: str | None,
) -> str:
    """Ausdrueckliches „Verwerfen": nur mit Bestätigung und gebundenem
    Löschvertrag; erzeugt kein Transkriptions-Handoff.

    Liefert ``canceled`` (gewonnen) / ``bestaetigung_erforderlich`` /
    ``loeschvertrag_erforderlich`` / ``too_late`` (Ergebnis bereits committet)
    / ``already_terminal`` / ``not_active``.
    """
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_active"
    if not confirmed:
        return "bestaetigung_erforderlich"
    if not deletion_contract_hash:
        return "loeschvertrag_erforderlich"
    if row.status == "canceled":
        return "already_terminal"
    if row.result_hash is not None or row.status == COMMITTED_STATE:
        return "too_late"
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.status.in_((*NON_TERMINAL_STATES, "failed")),
            RecordingRun.result_hash.is_(None),
        )
        .values(
            status="canceled",
            deletion_contract_hash=deletion_contract_hash,
            transcription_authorized=False,
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    if (result.rowcount or 0) > 0:
        return "canceled"
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is not None and row.status == "canceled":
        return "already_terminal"
    if row is not None and row.result_hash is not None:
        return "too_late"
    return "not_active"


def deliver_handoff(
    session: Session, identity_hash: str, *, audio_hash: str, manifest_hash: str
) -> str:
    """Idempotenter JFW-7-Handoff: identische erneute Zustellung setzt den-
    selben Run fort und autorisiert keine zweite Transkription.

    Liefert ``delivered`` (genau einmal) / ``existing`` / ``conflict``
    (fail-closed bei abweichendem Payload) / ``not_ready`` / ``not_found``.
    """
    row = session.query(RecordingRun).filter_by(identity_hash=identity_hash).one_or_none()
    if row is None:
        return "not_found"
    if row.result_hash is None or row.status != COMMITTED_STATE:
        return "not_ready"
    if row.audio_hash != audio_hash or row.manifest_hash != manifest_hash:
        return "conflict"
    if (row.handoff_count or 0) > 0:
        return "existing"
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.identity_hash == identity_hash,
            RecordingRun.handoff_count == 0,
        )
        .values(
            handoff_count=1,
            transcription_authorized=True,
            updated_at=_now(),
        )
    )
    session.commit()
    return "delivered" if (result.rowcount or 0) > 0 else "existing"


def recover_interrupted(session: Session, current_epoch: str) -> int:
    """Crash-Recovery: unterbrochene Laeufe anderer (oder ohne) Epoche werden
    ``failed``/``unterbrochen`` — nie als vollstaendig ausgegeben. Bereits
    committete Ergebnisse bleiben unangetastet."""
    result = session.execute(
        update(RecordingRun)
        .where(
            RecordingRun.status.in_(NON_TERMINAL_STATES),
            or_(
                RecordingRun.app_epoch.is_(None),
                RecordingRun.app_epoch != current_epoch,
            ),
            RecordingRun.result_hash.is_(None),
        )
        .values(
            status="failed",
            reason_code="unterbrochen",
            terminal_at=_now(),
            updated_at=_now(),
        )
    )
    session.commit()
    return result.rowcount or 0
