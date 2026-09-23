"""JFW-4: Exportstatus-Maschine, Readiness und Wort-Invarianten (I/O-frei).

Zustaende 1:1 nach der Spec-Sektion „Export Result Contract":
``preparing`` erzeugt keine Enddateien, ``blocked`` keinen Auftrag, und nur der
gemeinsam dauerhaft bestaetigte Set wird ``exported``. Der zuerst dauerhaft
gespeicherte terminale Ausgang gewinnt (atomare bedingte DB-Transaktionen in
``services/export_contract.py``).
"""
from __future__ import annotations

STATES = (
    "preparing",
    "ready",
    "ready_with_warnings",
    "blocked",
    "exporting",
    "exported",
    "failed",
    "canceled",
    "invalidated",
)

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "preparing": ("ready", "ready_with_warnings", "blocked", "failed", "canceled"),
    "ready": ("exporting", "blocked", "canceled", "failed"),
    "ready_with_warnings": ("exporting", "canceled", "failed"),
    "blocked": (),
    "exporting": ("exported", "failed", "canceled"),
    "exported": ("invalidated",),
    "failed": ("exporting",),
    "canceled": ("exporting",),
    "invalidated": (),
}

#: Nicht terminale Zustaende (Auftrag kann noch laufen/gesetzt werden).
NON_TERMINAL_STATES = ("preparing", "ready", "ready_with_warnings", "exporting")
#: Terminale Zustaende OHNE autoritativen Set.
FAILED_STATES = ("failed", "canceled")
#: Terminale Zustaende MIT autoritativen Ergebnis-Commit.
COMMITTED_STATES = ("exported",)

TIMING_OK = "vollstaendig"
TIMING_PARTIAL = "teilweise"
TIMING_MISSING = "nicht_verfuegbar"
DIMENSION_VALUES = (TIMING_OK, TIMING_PARTIAL, TIMING_MISSING, "nicht_betroffen", "sauber", "unsicher")


def can_transition(old: str, new: str) -> bool:
    return new in TRANSITIONS.get(old, ())


def assess_readiness(
    *,
    timing: str,
    speakers: str,
    sources: str = "nicht_betroffen",
    dedupe: str = "nicht_betroffen",
    partial_confirmed: bool = False,
    counts: dict | None = None,
) -> dict:
    """Bereitet die Bereitschaft inklusive Warn-/Sperrgruenden vor.

    Genau eine nicht verfuegbare Praezisionsdimension erlaubt nur nach
    ausdruecklicher Bestaetigung einen Teilmodus; ohne Bestaetigung bleibt der
    kombinierte Export ``blocked`` (fail-closed, nichts wird hochgestuft).
    ``counts`` enthaelt je Qualitaetsdimension die reale Anzahl betroffener
    Eintraege fuer die vor der Bestaetigung sichtbaren Zaehler.
    """
    counts = counts or {}
    quality = {
        "timing": timing,
        "speakers": speakers,
        "sources": sources,
        "dedupe": dedupe,
    }
    warnings: list[dict] = []
    if timing == TIMING_PARTIAL:
        warnings.append({"code": "timing_teilweise", "dimension": "timing",
                         "count": int(counts.get("timing", 1))})
    if speakers == TIMING_PARTIAL:
        warnings.append({"code": "sprecher_teilweise", "dimension": "speakers",
                         "count": int(counts.get("speakers", 1))})
    if sources == TIMING_PARTIAL:
        warnings.append({"code": "quellen_teilweise", "dimension": "sources",
                         "count": int(counts.get("sources", 1))})
    if dedupe == "unsicher":
        warnings.append({"code": "dedupe_unsicher", "dimension": "dedupe",
                         "count": int(counts.get("dedupe", 1))})

    base = {
        "partial_mode": None,
        "warnings": warnings,
        "quality_dimensions": quality,
        "reason_code": None,
    }

    timing_missing = timing == TIMING_MISSING
    speakers_missing = speakers == TIMING_MISSING
    if timing_missing and speakers_missing:
        return {**base, "state": "blocked", "reason_code": "kein_mindestinhalt"}
    if timing_missing or speakers_missing:
        if not partial_confirmed:
            return {
                **base,
                "state": "blocked",
                "reason_code": "teilmodus_bestaetigung_erforderlich",
            }
        partial_mode = "timing_only" if speakers_missing else "speaker_only"
        warnings = [*warnings, {"code": "teilqualitaet", "dimension": "partial",
                                "count": int(counts.get("partial", 1))}]
        return {
            "state": "ready_with_warnings",
            "partial_mode": partial_mode,
            "warnings": warnings,
            "quality_dimensions": quality,
            "reason_code": None,
        }
    state = "ready_with_warnings" if warnings else "ready"
    return {**base, "state": state}


def blocked_readiness(reason_code: str, quality: dict | None = None) -> dict:
    return {
        "state": "blocked",
        "partial_mode": None,
        "warnings": [],
        "quality_dimensions": quality
        or {"timing": TIMING_MISSING, "speakers": TIMING_MISSING,
            "sources": "nicht_betroffen", "dedupe": "nicht_betroffen"},
        "reason_code": reason_code,
    }


def validate_words(words, audio_duration_ms: int) -> list[str]:
    """Heilige Wort-Invarianten: keine erfundene Grenze, keine Mutation.

    Fehlercodes sind inhaltsfrei (Wort-IDs, Feldnamen — nie Anzeigetext).
    """
    errors: list[str] = []
    seen: set[str] = set()
    duration = int(audio_duration_ms)
    for wd in words:
        word_id = str(wd.get("word_id", ""))
        if word_id in seen:
            errors.append(f"wort_doppelt:{word_id}")
        seen.add(word_id)
        status = wd.get("timing_status")
        if status not in ("aligned", "unaligned", "not_applicable"):
            errors.append(f"wort_status_unbekannt:{word_id}")
            continue
        start = wd.get("start_ms")
        end = wd.get("end_ms")
        if status == "aligned":
            if start is None or end is None:
                errors.append(f"wort_grenze_fehlt:{word_id}")
                continue
            if not (0 <= int(start) < int(end) <= duration):
                errors.append(f"wort_zeit_ungueltig:{word_id}")
        else:
            if start is not None or end is not None:
                errors.append(f"wort_grenze_erfunden:{word_id}")
    return errors
