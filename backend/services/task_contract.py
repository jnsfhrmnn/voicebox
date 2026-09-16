"""JFW-12 Task-/Lease-Datenvertrag (Spec B7 + C): Exactly-once-Finalisierung.

Dieser Service ist die einzige Stelle, in der Attempt-Zustandsübergänge und
autoritative Rohrevisionen geschrieben werden. Der Vertrag:

- Jeder Attempt bindet ``job_id``, ``attempt_id`` (= tasks.id), ``app_epoch``,
  ``backend_generation``, Backend-/Modellvertrag (``model_contract_hash``) und
  Eingabehash (``input_hash``).
- Ein Transkriptresultat wird zusammen mit der terminalen Tasktransition in
  EINER DB-Transaktion gespeichert. Die Mutation ist bedingt auf den noch
  aktiven Attempt und nichtterminalen Status; die eindeutige
  ``source_attempt_id`` verhindert doppelte Rohrevisionen.
- Cancel verwendet dieselbe Vergleichsbedingung. Gewinnt Finalisierung, bleibt
  das Ergebnis genau einmal autoritativ und Cancel wird abgelehnt. Gewinnt
  Cancel, entsteht keine autoritative Teilrevision; ein neuer Attempt kann den
  selben Eingabesnapshot übernehmen.
- Späte Responses alter Generationen werden verworfen (``stale_generation``).

Kein anderer Code darf ``tasks.status`` oder ``transcript_revisions`` direkt
mutieren — nur über diese Funktionen (fail-closed Exactly-once-Gate).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..database.models import TERMINAL_TASK_STATUSES, TaskAttempt, TranscriptRevision

#: Ergebnis einer bedingten Zustandsübergabe (Spec B7).
FinalizeOutcome = Literal[
    "finalized",        # Terminaltransition + Rohrevision gewonnen (genau einmal)
    "already_terminal", # Attempt war bereits terminal — keine zweite Revision
    "cancelled",        # Cancel hat die Race gewonnen — Ergebnis verworfen
]

CancelOutcome = Literal["cancelled", "not_cancelled"]


@dataclass(frozen=True)
class FinalizeResult:
    """Ergebnis von :func:`finalize_attempt` (deterministisch, testbar)."""

    outcome: FinalizeOutcome
    attempt_id: str
    revision_id: str | None = None  # gesetzt nur bei outcome == "finalized"


def new_attempt(
    session: Session,
    *,
    job_id: str,
    app_epoch: str,
    backend_generation: int,
    backend_variant: str,
    model_contract_hash: str,
    input_hash: str,
) -> TaskAttempt:
    """Legt einen neuen ``pending``-Attempt an (Supervisor-/API-Seite).

    ``attempt_id`` wird hier zentral vergeben (uuid4), damit die eindeutige
    Attempt-Referenz in ``transcript_revisions.source_attempt_id`` immer zu
    genau einem Attempt zeigt.
    """
    attempt = TaskAttempt(
        id=str(uuid.uuid4()),
        job_id=job_id,
        app_epoch=app_epoch,
        backend_generation=int(backend_generation),
        backend_variant=backend_variant,
        model_contract_hash=model_contract_hash,
        input_hash=input_hash,
        status="pending",
    )
    session.add(attempt)
    session.commit()
    return attempt


def _conditional_terminal_update(session: Session, attempt_id: str, new_status: str) -> bool:
    """Bedingte Terminalmutation: nur wenn der Attempt noch nichtterminal ist.

    Die WHERE-Bedingung (``status NOT IN terminal``) macht die Transition atomar
    gegen konkurrierende Finalisierung/Cancel — genau eine Seite gewinnt.
    """
    result = session.execute(
        update(TaskAttempt)
        .where(TaskAttempt.id == attempt_id, TaskAttempt.status.notin_(sorted(TERMINAL_TASK_STATUSES)))
        .values(status=new_status, terminal_at=datetime.utcnow(), updated_at=datetime.utcnow())
    )
    return (result.rowcount or 0) > 0


def finalize_attempt(
    session: Session,
    *,
    attempt_id: str,
    transcript_raw: str,
    stt_model: str | None = None,
    language: str | None = None,
    duration_ms: int | None = None,
) -> FinalizeResult:
    """Speichert das Rohresultat zusammen mit der terminalen Transition.

    Gewinnt die bedingte Terminalmutation (Attempt war noch nichtterminal), wird
    in derselben Transaktion genau eine autoritative Rohrevision geschrieben.
    Verliert sie gegen Cancel oder eine frühere Finalisierung, wird nichts
    geschrieben und das Ergebnis verworfen — Audio bleibt erhalten, es entsteht
    keine Teilrevision.
    """
    if not _conditional_terminal_update(session, attempt_id, "succeeded"):
        # Race verloren: Status ist jetzt terminal (cancelled/succeeded/failed).
        current = session.get(TaskAttempt, attempt_id)
        status = current.status if current is not None else "missing"
        outcome: FinalizeOutcome = "cancelled" if status == "cancelled" else "already_terminal"
        return FinalizeResult(outcome=outcome, attempt_id=attempt_id)

    revision = TranscriptRevision(
        id=str(uuid.uuid4()),
        source_attempt_id=attempt_id,  # UNIQUE — doppelte Revision ist unmöglich
        transcript_raw=transcript_raw,
        stt_model=stt_model,
        language=language,
        duration_ms=duration_ms,
    )
    session.add(revision)
    session.commit()
    return FinalizeResult(outcome="finalized", attempt_id=attempt_id, revision_id=revision.id)


def cancel_attempt(session: Session, *, attempt_id: str) -> CancelOutcome:
    """Bedingtes Cancel — dieselbe Vergleichsbedingung wie Finalisierung.

    Gewinnt Cancel, bleibt Audio erhalten und es entsteht keine autoritative
    Teilrevision; ein neuer Attempt kann denselben Eingabesnapshot übernehmen.
    Gewinnt Finalisierung, wird Cancel abgelehnt (``not_cancelled``).
    """
    if not _conditional_terminal_update(session, attempt_id, "cancelled"):
        return "not_cancelled"
    session.commit()
    return "cancelled"


def mark_failed(
    session: Session, *, attempt_id: str, error_class: str | None = None
) -> bool:
    """Bedingte Fehler-Transition (z. B. Worker-Crash, Timeout).

    ``error_class`` ist inhaltsfrei (keine Nutzdaten im Statusfeld); Details
    gehören in das Rust-Switch-Journal, nicht in die DB.
    """
    if not _conditional_terminal_update(session, attempt_id, "failed"):
        return False
    session.commit()
    return True


def recover_stale_attempts(session: Session, *, current_epoch: str) -> int:
    """Crash-Recovery (Spec C): alte ``running``-Attempts der vorigen Epoche.

    Beim Neustart werden nichtterminale Attempts einer anderen ``app_epoch``
    bedingt auf ``failed`` überführt — eine bereits terminale Revision bleibt
    unverändert. Liefert die Anzahl übergeführter Attempts.
    """
    result = session.execute(
        update(TaskAttempt)
        .where(
            TaskAttempt.app_epoch != current_epoch,
            TaskAttempt.status.notin_(sorted(TERMINAL_TASK_STATUSES)),
        )
        .values(status="failed", terminal_at=datetime.utcnow(), updated_at=datetime.utcnow())
    )
    session.commit()
    return result.rowcount or 0
