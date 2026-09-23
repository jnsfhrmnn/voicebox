"""JFW-7: Processing Contract (Zustandsmaschine, I/O-frei).

Spec „Processing Contract" (Tabelle exakt):

* ``queued`` — Handoff und Snapshot angenommen (noch kein Text)
* ``waiting_for_backend`` — JFW-12-Wechsel/Health nicht eindeutig (kein stiller Fallback)
* ``waiting_for_model`` — lokales Artefakt fehlt oder laedt (keine automatische Netzwerkaktion)
* ``transcribing`` — ein gebundener Attempt verarbeitet lokal (nur Fortschritt)
* ``raw_ready`` — Rohtext und Provenienz atomar committet (genau eine immutable
  Rohtranskriptrevision)
* ``no_speech`` — kein sicherer Sprachinhalt (kein JFW-8-Payload)
* ``failed`` — Verarbeitung/Integritaet fehlgeschlagen (Audio und Diagnose bleiben)
* ``canceled`` — Nutzerabbruch wurde zuerst terminal (kein spaeterer Commit)
* ``invalidated`` — Audio oder Snapshot aenderte sich (neue Revision/Attempt noetig)

Attempt-Bindung: jeder Attempt bindet ``(backend_variant, backend_generation)`` bis
zu seinem terminalen Ausgang; ein notwendiger Backendwechsel erzeugt einen
sichtbaren neuen Attempt (``superseded_by_switch``), nie stille Fortsetzung.
Terminal-Race: der zuerst dauerhaft gespeicherte terminale Ausgang gewinnt.
"""
from __future__ import annotations

from dataclasses import dataclass, field

QUEUED = "queued"
WAITING_FOR_BACKEND = "waiting_for_backend"
WAITING_FOR_MODEL = "waiting_for_model"
TRANSCRIBING = "transcribing"
RAW_READY = "raw_ready"
NO_SPEECH = "no_speech"
FAILED = "failed"
CANCELED = "canceled"
INVALIDATED = "invalidated"

TERMINAL_STATES = frozenset({RAW_READY, NO_SPEECH, FAILED, CANCELED, INVALIDATED})
NON_TERMINAL_STATES = (QUEUED, WAITING_FOR_BACKEND, WAITING_FOR_MODEL, TRANSCRIBING)

BACKEND_VARIANTS = ("cpu", "cuda")


class ContractError(RuntimeError):
    """Fail-closed: unzulaessige Transition oder fehlende Bindung."""


@dataclass
class Attempt:
    """Ein sichtbar gebundener Verarbeitungsversuch (I/O-frei)."""

    attempt_id: str
    backend_variant: str
    backend_generation: int
    model_profile: str = "jfw7-dictate-v1"
    outcome: str | None = None  # None = aktiv, sonst terminaler/sichtbarer Ausgang

    def to_dict(self) -> dict:
        return {
            "attempt_id": self.attempt_id,
            "backend_variant": self.backend_variant,
            "backend_generation": self.backend_generation,
            "model_profile": self.model_profile,
            "outcome": self.outcome,
        }


@dataclass
class ProcessingSession:
    """Reiner Verarbeitungszustand eines Transkriptions-Runs (I/O-frei)."""

    run_id: str
    snapshot_hash: str
    state: str = QUEUED
    attempts: list = field(default_factory=list)
    current_attempt: Attempt | None = None
    terminal_outcome: str | None = None
    reason_code: str | None = None
    revision_id: str | None = None
    text_hash: str | None = None

    def _assert_not_terminal(self) -> None:
        if self.terminal_outcome is not None or self.state in TERMINAL_STATES:
            raise ContractError("bereits_terminal")

    def wait_for_backend(self, *, reason: str = "backend_unsicher") -> str:
        """Run bleibt wartend — keinem alten, neuen oder stillen Fallback zugestellt."""
        if self.state == TRANSCRIBING or self.state in TERMINAL_STATES:
            raise ContractError("zustand_erdlaubt_nicht")
        self.state = WAITING_FOR_BACKEND
        self.reason_code = reason
        return self.state

    def wait_for_model(self, *, reason: str = "artefakt_fehlt") -> str:
        """Lokales Artefakt fehlt/laedt — keine automatische Netzwerkaktion."""
        if self.state == TRANSCRIBING or self.state in TERMINAL_STATES:
            raise ContractError("zustand_erdlaubt_nicht")
        self.state = WAITING_FOR_MODEL
        self.reason_code = reason
        return self.state

    def start_attempt(
        self,
        *,
        backend_variant: str,
        backend_generation: int,
        model_profile: str,
        model_ready: bool,
        backend_stable: bool,
    ) -> Attempt | None:
        """Startet einen gebundenen Attempt; Wartefaelle bleiben wartend.

        Zurueck ``None`` bei ``waiting_for_backend``/``waiting_for_model`` — der
        Run wird dann an kein Backend zugestellt.
        """
        self._assert_not_terminal()
        if backend_variant not in BACKEND_VARIANTS:
            raise ContractError("backendvariante_unbekannt")
        if int(backend_generation) < 1:
            raise ContractError("backendgeneration_unzulaessig")
        if not backend_stable:
            self.wait_for_backend()
            return None
        if not model_ready:
            self.wait_for_model()
            return None
        active = self.current_attempt if (self.current_attempt and self.current_attempt.outcome is None) else None
        if active is not None:
            if (
                active.backend_variant == backend_variant
                and int(active.backend_generation) == int(backend_generation)
            ):
                raise ContractError("attempt_bereits_gebunden")
            # Notwendiger Backendwechsel: sichtbarer neuer Attempt statt stiller Fortsetzung.
            active.outcome = "superseded_by_switch"
        attempt = Attempt(
            attempt_id=_new_attempt_id(),
            backend_variant=backend_variant,
            backend_generation=int(backend_generation),
            model_profile=model_profile,
        )
        self.attempts.append(attempt)
        self.current_attempt = attempt
        self.state = TRANSCRIBING
        return attempt

    def commit_raw(self, *, revision_id: str, text_hash: str) -> None:
        """Genau ein atomarer Rohtranskript-Commit (``raw_ready``)."""
        self._assert_not_terminal()
        if self.state != TRANSCRIBING or self.current_attempt is None:
            raise ContractError("kein_aktiver_attempt")
        self.current_attempt.outcome = RAW_READY
        self.revision_id = revision_id
        self.text_hash = text_hash
        self.terminal_outcome = RAW_READY
        self.state = RAW_READY

    def mark_no_speech(self) -> None:
        """Kein sicherer Sprachinhalt — terminal OHNE JFW-8-Payload."""
        self._assert_not_terminal()
        if self.state != TRANSCRIBING or self.current_attempt is None:
            raise ContractError("kein_aktiver_attempt")
        self.current_attempt.outcome = NO_SPEECH
        self.terminal_outcome = NO_SPEECH
        self.state = NO_SPEECH

    def fail(self, *, reason_code: str) -> None:
        """Verarbeitung/Integritaet fehlgeschlagen — Audio und Diagnose bleiben."""
        self._assert_not_terminal()
        if self.current_attempt is not None and self.current_attempt.outcome is None:
            self.current_attempt.outcome = FAILED
        self.reason_code = reason_code
        self.terminal_outcome = FAILED
        self.state = FAILED

    def cancel(self, *, reason_code: str = "nutzerabbruch") -> None:
        """Nutzerabbruch — nach terminal kein spaeterer Commit autoritativ."""
        self._assert_not_terminal()
        if self.current_attempt is not None and self.current_attempt.outcome is None:
            self.current_attempt.outcome = CANCELED
        self.reason_code = reason_code
        self.terminal_outcome = CANCELED
        self.state = CANCELED

    def invalidate(self, *, reason_code: str = "snapshot_geaendert") -> None:
        """Audio oder Snapshot aenderte sich — neue Revision/Attempt erforderlich."""
        if self.state == CANCELED:
            raise ContractError("bereits_terminal")
        if self.state == INVALIDATED:
            raise ContractError("bereits_terminal")
        if self.current_attempt is not None and self.current_attempt.outcome is None:
            self.current_attempt.outcome = INVALIDATED
        self.reason_code = reason_code
        self.terminal_outcome = INVALIDATED
        self.state = INVALIDATED

    def attempt_history(self) -> list[dict]:
        return [a.to_dict() for a in self.attempts]


def _new_attempt_id() -> str:
    import uuid

    return str(uuid.uuid4())


def mark_attempt_outcome(attempts: list[dict], attempt_id: str | None, outcome: str) -> list[dict]:
    """Reiner Helper fuer die Persistenz: aktiven Attempt sichtbar abschliessen."""
    out = [dict(a) for a in (attempts or [])]
    for entry in reversed(out):
        if entry.get("attempt_id") == attempt_id and entry.get("outcome") is None:
            entry["outcome"] = outcome
            break
    return out
