"""JFW-13: Pseudonymregister ``pseudonym_v1`` — revisionierte, getrennte Zuordnung.

Kandidaten (Personen, Organisationen, Kontaktdaten, externe Links, vergleichbar
identifizierende Einzelangaben) erhalten konsistent GENAU EIN Pseudonym in der
Reihenfolge des erstmaligen Vorkommens: Personen ``Person 1..n``, Organisationen
``Organisation A..Z``, danach ``Organisation AA``, ``Organisation AB``, …
Nicht trennsicher zuordenbare Stellen werden ``Person (unklar)``/sichtbare
Auslassung — nie geraten, gemischt oder still zugeordnet.

Zustaende exakt nach Spec-Pseudonymisierungsvertrag. Nutzeraktionen
(bestaetigen/umbenennen/zusammenlegen/aufteilen/nicht_ersetzbar) erzeugen
jeweils eine neue, nachvollziehbare Revision (Parent-Referenz); die Aktion
selbst ist die Bestaetigung dieser Stelle. ``delete_register`` entfernt die
Zuordnungsinformation vollstaendig (Pseudonyme bleiben gueltig) und haelt die
AC-74-Regel: ein offener Exportdialog an die geloeschte Registerrevision wird
fuer jede weitere Ausgabe fail-closed abgelehnt.

Revision-IDs sind inhaltsdeterministisch (Hash der Revision), damit identische
Eingaben identische Schluessel und Bytes ergeben (Byte-Regel).
"""
from __future__ import annotations

from .provenance import canonical_hash

REGISTER_CONTRACT_VERSION = "pseudonym_v1"

STATE_VORGESCHLAGEN = "vorgeschlagen"
STATE_BESTAETIGT = "bestaetigt"
STATE_UMBENANNT = "umbenannt"
STATE_UNKLAR = "unklar"
STATE_NICHT_ERSATZBAR = "nicht_ersetzbar"
STATE_GELOESCHT = "geloescht"

KIND_PERSON = "person"
KIND_ORG = "organisation"
KIND_KONTAKT = "kontakt"
KIND_LINK = "link"
KIND_IDENT = "identifizierende_angabe"

PERSON_UNKLAR = "Person (unklar)"
ORG_UNKLAR = "Organisation (unklar)"

#: Felder, die Zuordnungen tragen koennten — in Logs/Spuren prinzipiell verboten.
FORBIDDEN_CONTENT_KEYS = (
    "original_text",
    "zuordnung",
    "register_mapping",
    "name",
    "email",
    "kontakt",
    "klarname",
)


def person_label(index: int) -> str:
    return f"Person {index}"


def org_label(index: int) -> str:
    """``Organisation A..Z``, danach ``AA``, ``AB`` … (26er-Basis, Excel-artig)."""
    letters = ""
    n = index
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return f"Organisation {letters}"


def _first_occurrence(entry: dict, turn_order: dict) -> tuple:
    best = None
    for occ in entry.get("occurrences") or []:
        key = (turn_order.get(occ.get("turn_id"), 10**9), occ.get("start", 0))
        if best is None or key < best:
            best = key
    return best or (10**9, 0)


def _revision_id(register_id: str, revision: int, parent_revision_id, status: str,
                 entries: list) -> str:
    return "pr-" + canonical_hash({
        "register_id": register_id,
        "revision": revision,
        "parent_revision_id": parent_revision_id,
        "status": status,
        "entries": entries,
    })[:32]


def _clone(register: dict, *, entries: list, status: str) -> dict:
    revision = int(register["revision"]) + 1
    out = {
        "contract_version": REGISTER_CONTRACT_VERSION,
        "register_id": register["register_id"],
        "revision": revision,
        "parent_revision_id": register["revision_id"],
        "status": status,
        "entries": entries,
    }
    out["revision_id"] = _revision_id(out["register_id"], revision,
                                     out["parent_revision_id"], status, entries)
    return out


def _next_pseudonym(entries: list, kind: str) -> str | None:
    if kind == KIND_PERSON:
        used = []
        for entry in entries:
            label = entry.get("pseudonym") or ""
            if entry.get("kind") == KIND_PERSON and label.startswith("Person "):
                tail = label[len("Person "):]
                if tail.isdigit():
                    used.append(int(tail))
        return person_label(max(used, default=0) + 1)
    if kind == KIND_ORG:
        used = []
        for entry in entries:
            label = entry.get("pseudonym") or ""
            if entry.get("kind") == KIND_ORG and label.startswith("Organisation "):
                used.append(_org_index(label[len("Organisation "):]))
        return org_label(max(used, default=0) + 1)
    return None  # Kontakt/Link/Identifizierende Angabe: sichtbare Entfernung


def _org_index(letters: str) -> int:
    n = 0
    for ch in letters:
        if not ch.isalpha():
            return 0
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n


def propose_register(candidates, turn_order: dict, register_id: str = "reg") -> dict:
    """Kandidaten -> Register-Entwurf (``vorgeschlagen``) nach Erstvorkommen."""
    ordered = sorted(
        candidates or [],
        key=lambda c: _first_occurrence(c, turn_order),
    )
    entries: list[dict] = []
    for index, candidate in enumerate(ordered):
        kind = str(candidate.get("kind") or KIND_IDENT)
        ambiguous = bool(candidate.get("ambiguous"))
        if ambiguous:
            state = STATE_UNKLAR
            pseudonym = PERSON_UNKLAR if kind == KIND_PERSON else (
                ORG_UNKLAR if kind == KIND_ORG else None)
        else:
            state = STATE_VORGESCHLAGEN
            pseudonym = _next_pseudonym(entries, kind)
        entries.append({
            "entry_id": f"{register_id}-e{index + 1}",
            "candidate_id": candidate.get("candidate_id"),
            "kind": kind,
            "pseudonym": pseudonym,
            "state": state,
            "original_text": candidate.get("text"),
            "occurrences": [dict(o) for o in (candidate.get("occurrences") or [])],
            "provenance": {"herkunft": "automatisch"},
            "ambiguous": ambiguous,
        })
    register = {
        "contract_version": REGISTER_CONTRACT_VERSION,
        "register_id": register_id,
        "revision": 1,
        "parent_revision_id": None,
        "status": STATE_VORGESCHLAGEN,
        "entries": entries,
    }
    register["revision_id"] = _revision_id(register_id, 1, None,
                                           STATE_VORGESCHLAGEN, entries)
    return register


def register_labels(register: dict) -> list[str]:
    return [e["pseudonym"] for e in register["entries"] if e.get("pseudonym")]


def label_map(register: dict) -> dict:
    return {e.get("candidate_id"): e.get("pseudonym") for e in register["entries"]}


def entry_by_candidate(register: dict, candidate_id: str) -> dict:
    for entry in register["entries"]:
        if entry.get("candidate_id") == candidate_id:
            return entry
    raise KeyError(candidate_id)


def confirm_register(register: dict) -> dict:
    """Bestaetigte Fassung: neue Revision, alle Vorschlaege -> ``bestaetigt``."""
    entries = []
    for entry in register["entries"]:
        entry = dict(entry)
        if entry["state"] == STATE_VORGESCHLAGEN:
            entry["state"] = STATE_BESTAETIGT
        entries.append(entry)
    return _clone(register, entries=entries, status=STATE_BESTAETIGT)


def apply_action(register: dict, action: dict) -> dict:
    """Nutzeraktion erzeugt eine neue, nachvollziehbare Registerrevision."""
    kind = action.get("kind")
    entries = [dict(e) for e in register["entries"]]

    if kind == "bestaetigen":
        for entry in entries:
            if entry["entry_id"] == action.get("entry_id"):
                entry["state"] = STATE_BESTAETIGT
    elif kind == "umbenennen":
        for entry in entries:
            if entry["entry_id"] == action.get("entry_id"):
                entry["state"] = STATE_UMBENANNT
                entry["pseudonym"] = action.get("pseudonym")
                entry["provenance"] = {"herkunft": "manuell"}
    elif kind == "nicht_ersetzbar":
        for entry in entries:
            if entry["entry_id"] == action.get("entry_id"):
                entry["state"] = STATE_NICHT_ERSATZBAR
    elif kind == "zusammenlegen":
        wanted = list(action.get("entry_ids") or [])
        keep = None
        merged = []
        for entry in entries:
            if entry["entry_id"] in wanted:
                if keep is None:
                    keep = entry
                    continue
                merged.append(entry)
        if keep is None:
            raise ValueError("zusammenlegen_ohne_ziel")
        for entry in merged:
            keep["occurrences"] = list(keep["occurrences"]) + list(entry["occurrences"])
        keep["provenance"] = {"herkunft": "manuell"}
        entries = [e for e in entries if e["entry_id"] not in wanted] + [keep]
    elif kind == "aufteilen":
        target = next((e for e in entries
                       if e["entry_id"] == action.get("entry_id")), None)
        if target is None:
            raise ValueError("aufteilen_ohne_ziel")
        splits = action.get("splits") or []
        occs = list(target["occurrences"])
        groups = [[occs[i] for i in group] for group in splits]
        new_entries = []
        for gi, group in enumerate(groups):
            if gi == 0:
                target["occurrences"] = group
                target["provenance"] = {"herkunft": "manuell"}
                continue
            clone = dict(target)
            clone["entry_id"] = f"{target['entry_id']}#{gi + 1}"
            clone["candidate_id"] = f"{target.get('candidate_id')}#{gi + 1}"
            clone["occurrences"] = group
            clone["pseudonym"] = _next_pseudonym(entries + new_entries, target["kind"])
            clone["state"] = STATE_VORGESCHLAGEN
            clone["provenance"] = {"herkunft": "manuell"}
            new_entries.append(clone)
        entries = entries + new_entries
    else:
        raise ValueError("unbekannte_aktion")

    status = register["status"]
    return _clone(register, entries=entries, status=status)


def delete_register(register: dict) -> dict:
    """Loescht die Zuordnungsinformation vollstaendig; Pseudonyme bleiben gueltig."""
    entries = []
    for entry in register["entries"]:
        entry = dict(entry)
        entry["original_text"] = None
        entry["occurrences"] = []
        entry["state"] = STATE_GELOESCHT
        entries.append(entry)
    return _clone(register, entries=entries, status=STATE_GELOESCHT)


def check_export_binding(register: dict, bound_register_revision) -> str:
    """AC 74: offener Exportdialog an die geloeschte Registerrevision = abgelehnt."""
    if register.get("status") == STATE_GELOESCHT:
        return "register_geloescht"
    known = {register.get("revision_id"), register.get("parent_revision_id")}
    if bound_register_revision in known:
        return "ok"
    return "register_revision_fremd"


def consistency_errors(register: dict) -> list[str]:
    errors: list[str] = []
    seen_labels: dict[str, str] = {}
    for entry in register["entries"]:
        label = entry.get("pseudonym")
        if not label:
            continue
        other = seen_labels.get(label)
        if other is not None and other != entry.get("candidate_id"):
            errors.append(f"pseudonym_doppelt:{label}")
        seen_labels[label] = entry.get("candidate_id")
    return errors


def assert_content_free(payload: dict) -> None:
    """Zuordnungen/Namen duerfen nie in Logs, Metriken oder Crash-Dumps."""
    for key in FORBIDDEN_CONTENT_KEYS:
        if key in payload:
            raise ValueError(f"inhalt_im_log:{key}")
    for value in payload.values():
        if isinstance(value, dict):
            assert_content_free(value)
