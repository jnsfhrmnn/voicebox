"""JFW-13: vollstaendiges anonymisiertes Transkript (I/O-frei).

Jeder Redebeitrag des JFW-4-Snapshots erscheint GENAU EINMAL in unveraenderter
Reihenfolge mit Sprecherpseudonym oder neutralem Unsicherheitslabel und sichtbarer
Zeitmarke. Overlap, ``unsicher``, ``nicht_zugeordnet``, ``dedupe_unsicher`` und
Quellenluecken bleiben erkennbar — nie geglaettet, nie auf einen sicheren
Sprecher reduziert. Bestaetigte JFW-11-Namen werden derselben Pseudonymisierung
unterzogen wie jede andere Erwaehnung; ein Rohname erscheint NIE. Beitragsrollen
(eigen/fremd) nur bei belegter JFW-11-Dual-Source-Evidenz, sonst ausgeblendet.
"""
from __future__ import annotations

from .redaction import build_replacement_plan, redact_text

UNSIICHER_LABEL = "Sprecher (unsicher)"


def _word_index(document: dict) -> dict:
    return {str(w.get("word_id")): w for w in document.get("words", [])}


def _turn_char_range(turn: dict, words: dict) -> tuple[int, int]:
    starts, ends = [], []
    for word_id in turn.get("word_ids") or []:
        word = words.get(str(word_id))
        if word is None:
            continue
        starts.append(int(word["char_start"]))
        ends.append(int(word["char_end"]))
    if not starts:
        return 0, 0
    return min(starts), max(ends)


def _authorized_name(document: dict, cluster_id: str) -> str | None:
    for mapping in document.get("name_mappings") or []:
        if str(mapping.get("cluster_id")) != str(cluster_id):
            continue
        if mapping.get("state") in ("confirmed", "manual") and mapping.get("name"):
            return str(mapping["name"])
    return None


def _name_to_pseudonym(register: dict | None, name: str) -> str | None:
    if not register:
        return None
    for entry in register.get("entries") or []:
        if entry.get("original_text") == name and entry.get("pseudonym"):
            return entry["pseudonym"]
    return None


def _display_label(document: dict, cluster_id: str) -> str:
    for speaker in document.get("speakers") or []:
        if str(speaker.get("cluster_id")) == str(cluster_id):
            return str(speaker.get("display_label") or cluster_id)
    return str(cluster_id)


def _turn_flags(document: dict, turn: dict, words: dict, uncertainty: bool) -> list[str]:
    flags: list[str] = []
    overlap = turn.get("overlap")
    if overlap and overlap != "nicht_ueberlappend":
        flags.append(str(overlap))
    if uncertainty:
        flags.append("sprecher_unsicher")
    jfw11 = (document.get("revisions", {}) or {}).get("jfw11") or {}
    for decision in document.get("dedupe_decisions") or []:
        if decision.get("kind") in ("dedupe_unsicher", "getrennt_behalten"):
            flags.append("dedupe_unsicher")
            break
    for gap in document.get("source_gaps") or []:
        if int(gap.get("start_ms") or 0) <= int(turn.get("end_ms") or 0) and \
                int(gap.get("end_ms") or 0) >= int(turn.get("start_ms") or 0):
            flags.append("quelle_fehlend")
            break
    if jfw11.get("dedupe_status") == "dedupe_unsicher" and "dedupe_unsicher" not in flags:
        flags.append("dedupe_unsicher")
    return flags


def _roles_by_turn(document: dict) -> dict:
    """Beitragsrollen NUR bei belegter JFW-11-Dual-Source-Evidenz."""
    jfw11 = (document.get("revisions", {}) or {}).get("jfw11") or {}
    if not jfw11 or jfw11.get("status") != "secured_dual":
        return {}
    roles: dict = {}
    for source in document.get("source_representations") or []:
        role = source.get("contribution_role")
        if role in ("eigen", "fremd") and source.get("turn_id"):
            roles[str(source["turn_id"])] = role
    return roles


def _sources_by_turn(document: dict) -> dict:
    out: dict = {}
    for source in document.get("source_representations") or []:
        turn_id = str(source.get("turn_id"))
        out.setdefault(turn_id, []).append({
            "source_id": source.get("source_id"),
            "source": source.get("source"),
        })
    return out


def build_transcript(document: dict, register: dict | None = None,
                     role_evidence: dict | None = None) -> dict:
    words = _word_index(document)
    text = document.get("transcript", {}).get("text", "")
    plan = build_replacement_plan(register) if register else []
    roles = role_evidence or _roles_by_turn(document)
    sources = _sources_by_turn(document)

    entries: list[dict] = []
    for order, turn in enumerate(document.get("turns") or []):
        char_start, char_end = _turn_char_range(turn, words)
        source_slice = text[char_start:char_end]

        uncertainty = False
        for word_id in turn.get("word_ids") or []:
            word = words.get(str(word_id)) or {}
            if word.get("speaker_status") != "sicher" or not word.get("cluster_id"):
                uncertainty = True

        cluster_id = turn.get("cluster_id")
        name = _authorized_name(document, cluster_id)
        if name is not None:
            label = _name_to_pseudonym(register, name) or "Person (unklar)"
        elif uncertainty:
            label = UNSIICHER_LABEL
        else:
            label = _display_label(document, cluster_id)

        entries.append({
            "order": order,
            "turn_id": turn.get("turn_id"),
            "char_start": char_start,
            "char_end": char_end,
            "time_marke": {"start_ms": turn.get("start_ms"), "end_ms": turn.get("end_ms")},
            "sprecher": {
                "cluster_id": cluster_id,
                "label": label,
                "uncertainty": uncertainty,
            },
            "source_text": source_slice,
            "text": redact_text(source_slice, plan, offset=char_start),
            "flags": _turn_flags(document, turn, words, uncertainty),
            "contribution_role": roles.get(str(turn.get("turn_id"))),
            "quellen": sources.get(str(turn.get("turn_id")), []),
        })
    return {"entries": entries}


def verify_transcript_complete(document: dict, entries) -> list[str]:
    """Vollstaendigkeit und Reihenfolge gegen den Snapshot — fail-closed."""
    errors: list[str] = []
    expected = [t.get("turn_id") for t in document.get("turns") or []]
    actual = [e.get("turn_id") for e in entries or []]
    if len(actual) != len(set(actual)):
        errors.append("turn_doppelt")
    if actual != expected:
        errors.append("reihenfolge_oder_vollstaendigkeit_veraendert")
    words = _word_index(document)
    text = document.get("transcript", {}).get("text", "")
    for entry in entries or []:
        turn = next((t for t in document.get("turns") or []
                     if t.get("turn_id") == entry.get("turn_id")), None)
        if turn is None:
            errors.append(f"turn_unbekannt:{entry.get('turn_id')}")
            continue
        char_start, char_end = _turn_char_range(turn, words)
        if (entry.get("char_start"), entry.get("char_end")) != (char_start, char_end):
            errors.append(f"zeichenbereich_veraendert:{entry.get('turn_id')}")
        if entry.get("source_text") != text[char_start:char_end]:
            errors.append(f"text_veraendert:{entry.get('turn_id')}")
    return errors
