"""JFW-11: Run-Zustandsmaschine (Dual-Source Capture Contract, I/O-frei).

Spec-Tabelle „Dual-Source Capture Contract":

* ``preparing`` — Preflight nicht vollständig, kein Startton, keine Behauptung.
* ``recording_dual`` — beide Quellen und Recovery bestätigt, erster Block jeder
  Spur auf die gemeinsame Run-Zeitbasis abbildbar.
* ``recording_degraded`` — mindestens eine Quelle ausgefallen (Ausfall ist Stille
  **ohne** Fehlerflag, Spike §6.1 — der Fehlerfall wird hier nur als Diagnose mit
 geführt), verbleibende Spur läuft weiter, sichtbare Lücke, Nutzerentscheidung.
* ``secured_dual`` / ``secured_partial`` / ``failed`` / ``canceled`` / ``invalidated``.

Quellenwiederkehr ist **nie** still: eine Fortsetzung derselben Quellenrolle
erfordert ausdrückliche Bestätigung und erzeugt einen neuen Track-Abschnitt.
"""
from __future__ import annotations

PREPARING = "preparing"
RECORDING_DUAL = "recording_dual"
RECORDING_DEGRADED = "recording_degraded"
SECURED_DUAL = "secured_dual"
SECURED_PARTIAL = "secured_partial"
FAILED = "failed"
CANCELED = "canceled"
INVALIDATED = "invalidated"

RECORDING_STATES = (RECORDING_DUAL, RECORDING_DEGRADED)


class RunStateError(RuntimeError):
    """Fail-closed: unzulässige Transition oder fehlende Bestätigung."""


def start_dual(
    state: str,
    *,
    mic_ready: bool,
    remote_ready: bool,
    recovery_ready: bool,
    first_blocks_mapped: bool,
) -> str:
    """Genau einmalig zu ``recording_dual`` — erst wenn alles bereit ist."""
    if state != PREPARING:
        raise RunStateError(f"start_dual nicht aus {state!r} zulaessig")
    if not (mic_ready and remote_ready and recovery_ready and first_blocks_mapped):
        raise RunStateError("start_blockiert: Vorbereitung unvollstaendig")
    return RECORDING_DUAL


def source_lost(state: str, *, source_error: bool) -> str:
    """Quellenausfall → ``recording_degraded`` (Stille ohne Fehlerflag ist der
    Normalfall; ``source_error`` trägt nur die Diagnose, z. B.
    ``AUDCLNT_E_DEVICE_INVALIDATED``, Spike §6.6)."""
    del source_error  # Vertragszustand ist identisch, Diagnose ist Manifest-Metadatum
    if state not in RECORDING_STATES:
        raise RunStateError(f"source_lost nicht aus {state!r} zulaessig")
    return RECORDING_DEGRADED


def source_returned(state: str, *, confirmed: bool) -> str:
    """Wiederkehrende Quelle nur mit ausdrücklicher Bestätigung (nie still)."""
    if state != RECORDING_DEGRADED:
        raise RunStateError(f"source_returned nicht aus {state!r} zulaessig")
    if not confirmed:
        raise RunStateError("wiedereintritt_bestaetigung_erforderlich")
    return RECORDING_DUAL


def secure_run(state: str, recovery_summary: dict) -> str:
    """Abschluss je nach Recovery-Status der Spuren (vollstaendig/teilweise/fehlend)."""
    if state not in RECORDING_STATES:
        raise RunStateError(f"secure_run nicht aus {state!r} zulaessig")
    vollstaendig = int(recovery_summary.get("vollstaendig", 0))
    teilweise = int(recovery_summary.get("teilweise", 0))
    fehlend = int(recovery_summary.get("fehlend", 0))
    del fehlend
    if vollstaendig + teilweise == 0:
        return FAILED
    if teilweise == 0 and vollstaendig == 2:
        return SECURED_DUAL
    return SECURED_PARTIAL


def cancel_run(state: str, *, confirmed: bool) -> str:
    """Bestätigter Abbruch/Verwerfen."""
    if not confirmed:
        raise RunStateError("abbruch_bestaetigung_erforderlich")
    if state not in (PREPARING, *RECORDING_STATES):
        raise RunStateError(f"cancel_run nicht aus {state!r} zulaessig")
    return CANCELED


def invalidate(state: str) -> str:
    """Autoritative Spur/Manifest/gebundene Revision geändert/gelöscht."""
    if state not in (SECURED_DUAL, SECURED_PARTIAL):
        raise RunStateError(f"invalidate nicht aus {state!r} zulaessig")
    return INVALIDATED
