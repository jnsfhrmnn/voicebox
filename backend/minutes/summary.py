"""JFW-13: Zusammenfassung Kurz/Lang — keine neuen Fakten (I/O-frei).

Jede Aussage traegt mindestens eine existierende Belegreferenz; Zitate muessen
exakte Substrings der Quelle sein (Wortlaut/Bedeutung unveraendert). Aussagen
ohne Beleg oder mit nicht belegbarem Zitat werden sichtbar verworfen — es gelangt
kein Aussageblock ohne Beleg in die Endausgabe. Aussagen mit unklarem Status
bleiben mit sichtbarem Marker.
"""
from __future__ import annotations

from .tasks import valid_evidence

OHNE_BELEG = "aussage_ohne_beleg_verworfen"
ZITAT_NICHT_BELEGT = "zitat_nicht_belegt"


def _validate(items, source_text: str, turns) -> tuple[list[dict], list[dict]]:
    kept: list[dict] = []
    verworfen: list[dict] = []
    for item in items or []:
        evidence = valid_evidence(item.get("evidence"), turns)
        if not evidence:
            verworfen.append({
                "statement_id": item.get("statement_id"),
                "reason_code": OHNE_BELEG,
            })
            continue
        quotes = list(item.get("quotes") or [])
        if any(q not in source_text for q in quotes):
            verworfen.append({
                "statement_id": item.get("statement_id"),
                "reason_code": ZITAT_NICHT_BELEGT,
            })
            continue
        status = str(item.get("status") or "belegt")
        kept.append({
            "statement_id": item.get("statement_id"),
            "kind": item.get("kind") or "aussage",
            "text": item.get("text"),
            "quotes": quotes,
            "status": status,
            "status_marker": "status_unklar" if status == "unklar" else None,
            "evidence": evidence,
        })
    return kept, verworfen


def build_summary(kurz_items, lang_items, source_text: str, turns,
                  assignment: dict | None = None) -> dict:
    kurz, verworfen_kurz = _validate(kurz_items, source_text, turns)
    lang, verworfen_lang = _validate(lang_items, source_text, turns)
    return {
        "kurz": kurz,
        "lang": lang,
        "verworfen": verworfen_kurz + verworfen_lang,
    }
