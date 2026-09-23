"""JFW-11: Ergebnis- und Annotations-Vertrag (I/O-frei).

Spec „Nachgelagerte Pipeline": Das kombinierte Sprecherergebnis ergänzt
**ausschließlich** Quellen-, Deduplizierungs- und Namenszuordnungen. Sichtbarer
Text, Wortreihenfolge, Wortgrenzen, neutrale Cluster-IDs und Overlap-/
Unsicherheitsstatus aus JFW-2/JFW-3 bleiben feldweise unverändert — jede
Abweichung ist fail-closed.
"""
from __future__ import annotations

from .manifest import canonical_hash

RESULT_CONTRACT_VERSION = "meeting_result_v1"

STATUS_DUAL = "dual_source"
STATUS_DUAL_PARTIAL = "dual_source_partial"
STATUS_SINGLE = "single_source"

FLAG_MISSING_SOURCE = "fehlende_quelle"
FLAG_PARTIAL_TRACK = "unvollstaendige_spur"
FLAG_SYNC_UNCERTAIN = "sync_unsicher"


def verify_annotations_only(base_result: dict, combined_result: dict) -> list[str]:
    """Heiliger Unveränderlichkeits-Check; leere Liste = OK."""
    errors: list[str] = []
    if combined_result.get("text") != base_result.get("text"):
        errors.append("text_geaendert")
    if combined_result.get("words") != base_result.get("words"):
        errors.append("wortgrenzen_geaendert")
    if combined_result.get("clusters") != base_result.get("clusters"):
        errors.append("cluster_geaendert")
    if combined_result.get("turns") != base_result.get("turns"):
        errors.append("overlap_geaendert")
    return errors


def derive_result_status(
    secured_state: str, recovery_status: dict, sync_quality: str
) -> dict:
    """Leitet die Downstream-Kennzeichnung aus Spur- und Sync-Status ab.

    ``single_source`` / ``dual_source_partial`` behaupten weder vollständige
    Eigenstimmen-Deduplizierung noch vollständige Fremdstimmenabdeckung.
    """
    del secured_state  # Verwertbarkeit folgt dem nachgewiesenen Spurstatus
    states = list(recovery_status.values())
    flags: list[str] = []
    if not states or any(s == "fehlend" for s in states):
        status = STATUS_SINGLE
        flags.append(FLAG_MISSING_SOURCE)
    elif all(s == "vollstaendig" for s in states) and len(states) == 2:
        status = STATUS_DUAL
    else:
        status = STATUS_DUAL_PARTIAL
        flags.append(FLAG_PARTIAL_TRACK)
    if sync_quality != "sync_ok":
        flags.append(FLAG_SYNC_UNCERTAIN)
    return {"status": status, "flags": flags}


def result_hash(payload: dict) -> str:
    """Kanonischer Ergebnis-Hash der Annotationsebene."""
    return canonical_hash(payload)
