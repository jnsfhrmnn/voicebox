"""JFW-13: Ersetzplan, Redaktion und Re-Identifizierungshinweise (I/O-frei).

Kontaktdaten, externe Links und vergleichbar identifizierende Einzelangaben
werden entfernt bzw. als ``[entfernt]`` gekennzeichnet; Personen/Organisationen
tragen ihr bestaetigtes Pseudonym. ``nicht_ersetzbar``-Stellen bleiben sichtbar
mit Re-Identifizierungshinweis — es wird keine Anonymitaet behauptet.
Ueberlappende Erkennungen werden nie geraten, sondern sichtbar gemarkt.
"""
from __future__ import annotations

import re

from .pseudonym import (
    KIND_KONTAKT,
    KIND_LINK,
    KIND_PERSON,
    PERSON_UNKLAR,
    STATE_NICHT_ERSATZBAR,
    STATE_UNKLAR,
)

REMOVED_MARKER = "[entfernt]"
#: USCRX-2026-16006 (RL-04): sichtbare Reststelle — keine Anonymitaet behauptet.
RESTE_MARKER = "[Reststelle]"


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _replacement_for(entry: dict) -> str | None:
    if entry.get("state") == STATE_NICHT_ERSATZBAR:
        return None  # Originalstelle bleibt, mit Hinweis
    if entry.get("kind") in (KIND_KONTAKT, KIND_LINK, "identifizierende_angabe"):
        return REMOVED_MARKER
    if entry.get("pseudonym"):
        return entry["pseudonym"]
    return REMOVED_MARKER


def build_replacement_plan(register: dict) -> list[dict]:
    """Plan aus Register-Okkurrenzen; Konflikte (Ueberschneidungen) = sichtbarer Marker."""
    raw: list[dict] = []
    for entry in register["entries"]:
        replacement = _replacement_for(entry)
        if replacement is None:
            continue
        for occ in entry.get("occurrences") or []:
            raw.append({
                "start": int(occ["start"]),
                "end": int(occ["end"]),
                "replacement": replacement,
                "entry_id": entry["entry_id"],
                "kind": entry.get("kind"),
                "conflict": False,
            })
    raw.sort(key=lambda r: (r["start"], r["end"]))

    # Ueberschneidungen zwischen VERSCHIEDENEN Eintraegen: nie raten.
    plan: list[dict] = []
    for item in raw:
        overlapping = [p for p in plan if p["start"] < item["end"] and item["start"] < p["end"]]
        cross = [p for p in overlapping if p["entry_id"] != item["entry_id"]]
        if not cross:
            plan.append(item)
            continue
        merged_start = min([p["start"] for p in cross] + [item["start"]])
        merged_end = max([p["end"] for p in cross] + [item["end"]])
        kinds = {p.get("kind") for p in cross} | {item.get("kind")}
        marker = PERSON_UNKLAR if KIND_PERSON in kinds else REMOVED_MARKER
        for p in cross:
            plan.remove(p)
        plan.append({
            "start": merged_start,
            "end": merged_end,
            "replacement": marker,
            "entry_id": ",".join(sorted({p["entry_id"] for p in cross} | {item["entry_id"]})),
            "kind": "konflikt",
            "conflict": True,
        })
    plan.sort(key=lambda r: (r["start"], r["end"]))
    return plan


def redact_text(text: str, plan: list[dict], offset: int = 0) -> str:
    """Wendet den Plan deterministisch an (rechts nach links, exakte Spans)."""
    out = text
    for item in sorted(plan, key=lambda r: r["start"], reverse=True):
        start = item["start"] - offset
        end = item["end"] - offset
        if end <= 0 or start >= len(out):
            continue
        start = max(start, 0)
        end = min(end, len(out))
        out = out[:start] + item["replacement"] + out[end:]
    return out


def redact_free_text(text: str, register: dict) -> str:
    """Anonymisierung freier Texte (Aufgaben/Zusammenfassung) — wortgrenzengenau.

    Sofortstand (USCRX-2026-16006/RL-04): Ganze Woerter werden ersetzt
    (Schreibvarianten inklusive, laengste zuerst); Woerter, die einen
    Registerbegriff nur als Teilwort tragen, werden NICHT verstümmelt
    (nie „Person 1mann"), sondern als Reststelle SICHTBAR gemarkt — es wird
    keine volle Anonymitaet behauptet. Zielloesung (separat): spanbasierte
    Redaktion der Freitext-Generierungen ueber Register-Spans.
    """
    if not text:
        return text
    pairs: list[tuple[str, str]] = []
    for entry in register.get("entries") or []:
        original = entry.get("original_text")
        replacement = _replacement_for(entry)
        if original and replacement:
            pairs.append((original, replacement))
    # laengste Fundstellen zuerst (z. B. „Erika Muster" vor „Muster")
    pairs.sort(key=lambda pair: len(pair[0]), reverse=True)

    # Phase 1: ganze Woerter ersetzen (Schreibvarianten wie „muster" inklusive).
    out = text
    for original, replacement in pairs:
        word_pat = re.compile(r"(?<!\w)" + re.escape(original) + r"(?!\w)", re.IGNORECASE)
        out = word_pat.sub(replacement, out)

    # Phase 2: Reste — Woerter mit Registerbegriff als Teilwort sichtbar markieren.
    marked = out
    for original, _ in pairs:
        part_pat = re.compile(re.escape(original), re.IGNORECASE)
        spans: set[tuple[int, int]] = set()
        for m in part_pat.finditer(marked):
            start, end = m.start(), m.end()
            while start > 0 and _is_word_char(marked[start - 1]):
                start -= 1
            while end < len(marked) and _is_word_char(marked[end]):
                end += 1
            spans.add((start, end))
        for start, end in sorted(spans, reverse=True):
            marked = marked[:start] + RESTE_MARKER + marked[end:]
    return marked


def reident_hinweise(register: dict) -> list[dict]:
    hints: list[dict] = []
    for entry in register["entries"]:
        if entry.get("state") == STATE_NICHT_ERSATZBAR:
            hints.append({"entry_id": entry["entry_id"], "reason": "nicht_ersetzbar",
                          "kind": entry.get("kind")})
        elif entry.get("state") == STATE_UNKLAR:
            hints.append({"entry_id": entry["entry_id"], "reason": "unklar",
                          "kind": entry.get("kind")})
    return hints


# Englischer Alias fuer die Importstruktur der Tests
reident_hints = reident_hinweise
