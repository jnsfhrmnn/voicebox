"""JFW-5: Ressourcenzulassung, Diktatvorrang und Preflight (I/O-frei).

`jfw5_serial_v1`: ohne belegte sichere Parallelitaet laeuft HOECHSTENS eine
ressourcenintensive KI-Phase gleichzeitig. Ressourcenmangel ist ein sichtbarer
Wartezustand mit Grundcode — nie ein Scheinfehler und nie ein stiller
Profilwechsel. Der Diktatvorrang wirkt als Zulassungsriegel (``dictation_hold``);
Phasen ohne nachweislich kurzen sicheren Yield-Punkt starten nur mit
reservierten Diktatressourcen.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Admission:
    admitted: bool
    reason_code: str | None
    state: str  # "zugelassen" | "warten" | "blockiert"
    is_error: bool = False


def admit_phase(
    *,
    active_ai_phases: int,
    resource_policy: dict,
    dictation_active: bool = False,
    dictation_reserved: bool = True,
    phase_has_safe_yield: bool = True,
    free_bytes: int | None = None,
) -> Admission:
    if dictation_active:
        return Admission(False, "dictat_vorrang", "warten")
    limit = int(resource_policy.get("max_concurrent_ai_phases") or 1)
    if active_ai_phases >= limit:
        return Admission(False, "ressource_belegt", "warten")
    if not phase_has_safe_yield and not dictation_reserved:
        return Admission(False, "diktatressourcen_nicht_reserviert", "warten")
    min_free = int(resource_policy.get("min_free_bytes") or 0)
    if free_bytes is not None and free_bytes < min_free:
        return Admission(False, "speicher_gering", "blockiert")
    return Admission(True, None, "zugelassen")
