"""JFW-2: Ergebnisvertrag — Zeitintegrität, Zustände, kanonische Hashes.

Vertrag (Spec „Alignment Result Contract" + „Zeitintegrität"):

* ``aligned``-Eintraege erfuellen immer ``0 <= start_ms < end_ms <= Audiodauer``
  und bleiben auf der (nicht ueberlappenden) Ergebnisspur zeitlich monoton.
* Verletzungen werden NIEMALS kaschiert: das betroffene Wort bleibt mit
  unveraendertem Anzeigetext ``unaligned``, leerer Grenze und versioniertem,
  inhaltsfreiem Grundcode.
* ``not_applicable``-Token erhalten nie Zeiten und zaehlen nicht zur Abdeckung.
* Zustandsableitung: 100 % -> ``aligned``; >= 95 % und < 100 % ->
  ``partially_aligned``; < 95 % -> ``failed``; kein ausrichtbares Token ->
  ``no_alignable_speech`` (ohne prozentuale Scheinabdeckung).
"""
from __future__ import annotations

import hashlib
import json
import math

from .text_map import STATUS_NOT_APPLICABLE, verify_no_text_change

#: Version des Ergebnisvertrags (Teil jedes Ergebniskopfes und jedes Hashes).
RESULT_CONTRACT_VERSION = "jfw2-alignment-v1"
#: Zeitbasis aller Grenzen: Fließkomma-Millisekunden ueber die gebundene Audioquelle.
TIMEBASE_AUDIO_MS = "audio_ms_v1"

WORD_ALIGNABLE = "alignable"
WORD_ALIGNED = "aligned"
WORD_UNALIGNED = "unaligned"
WORD_NOT_APPLICABLE = STATUS_NOT_APPLICABLE

#: Versionierte, inhaltsfreie Grundcodes (keine Textinhalte).
REASON_CODES = frozenset(
    {
        "no_acoustic_match",
        "invalid_boundary",
        "non_monotonic",
        "not_romanizable",
        "uncertain_language",
        "text_change_detected",
        "provider_error",
        "provider_runtime_missing",
        "artifact_missing",
    }
)

#: Zustaende des Alignment-Result-Contracts der Spec.
RESULT_STATES = frozenset(
    {
        "queued",
        "waiting_for_local_artifact",
        "aligning",
        "aligned",
        "partially_aligned",
        "no_alignable_speech",
        "failed",
        "canceled",
        "invalidated",
    }
)
#: Zustaende mit autoritativem, fuer JFW-3/JFW-4 freigegebenem Ergebnis.
RELEASABLE_STATES = frozenset({"aligned", "partially_aligned"})


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def coverage(words: list[dict]) -> tuple[int, int]:
    """(ausrichtbare Token, davon ausgerichtet) — ``not_applicable`` zaehlt nie."""
    alignable = [w for w in words if w.get("status") != WORD_NOT_APPLICABLE]
    aligned = [w for w in alignable if w.get("status") == WORD_ALIGNED]
    return len(alignable), len(aligned)


def derive_state(words: list[dict]) -> str:
    alignable_n, aligned_n = coverage(words)
    if alignable_n == 0:
        return "no_alignable_speech"
    if aligned_n == alignable_n:
        return "aligned"
    if aligned_n / alignable_n >= 0.95:
        return "partially_aligned"
    return "failed"


def _downgrade(word: dict, reason_code: str) -> dict:
    word["status"] = WORD_UNALIGNED
    word["start_ms"] = None
    word["end_ms"] = None
    word["reason_code"] = reason_code
    word["quality"] = None
    return word


def build_result(
    text: str,
    base_words: list[dict],
    assignments: dict,
    *,
    duration_ms: float,
    provider: str,
    frame_ms: float | None = None,
) -> dict:
    """Baut das Ergebnis aus Grundvertrag und Provider-Zuweisungen.

    ``assignments``: ``word_id -> (start_ms, end_ms, ctc_score)`` oder ``None``
    (nicht verortbar). Fehlende Zuweisungen erzeugen ``unaligned`` mit
    ``no_acoustic_match``. Niemals wird interpoliert, geerbt oder geschaetzt.
    """
    verify_no_text_change(text, base_words)  # heiliges No-text-change-Gate

    words: list[dict] = []
    prev_end: float | None = None
    for bw in base_words:
        w = {
            "word_id": bw["word_id"],
            "order": bw["order"],
            "char_start": bw["char_start"],
            "char_end": bw["char_end"],
            "text": bw["text"],
            "status": bw["status"],
            "start_ms": None,
            "end_ms": None,
            "reason_code": None,
            "quality": None,
        }
        if bw["status"] == WORD_NOT_APPLICABLE:
            words.append(w)
            continue

        assignment = assignments.get(bw["word_id"])
        if assignment is None:
            words.append(_downgrade(w, "no_acoustic_match"))
            continue
        start, end, score = assignment
        try:
            start_f, end_f = float(start), float(end)
        except (TypeError, ValueError):
            words.append(_downgrade(w, "invalid_boundary"))
            continue
        if not (math.isfinite(start_f) and math.isfinite(end_f)):
            words.append(_downgrade(w, "invalid_boundary"))
            continue
        if not (0 <= start_f < end_f <= float(duration_ms)):
            words.append(_downgrade(w, "invalid_boundary"))
            continue
        if prev_end is not None and start_f < prev_end:
            words.append(_downgrade(w, "non_monotonic"))
            continue
        w["status"] = WORD_ALIGNED
        w["start_ms"] = start_f
        w["end_ms"] = end_f
        w["reason_code"] = None
        w["quality"] = {
            "provider": provider,
            "frame_ms": frame_ms,
            "ctc_score": (float(score) if score is not None else None),
        }
        prev_end = end_f
        words.append(w)

    alignable_n, aligned_n = coverage(words)
    state = derive_state(words)
    payload = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "timebase": TIMEBASE_AUDIO_MS,
        "status": state,
        "coverage_alignable": alignable_n,
        "coverage_aligned": aligned_n,
        "words": words,
    }
    return {**payload, "result_hash": canonical_hash(payload)}
