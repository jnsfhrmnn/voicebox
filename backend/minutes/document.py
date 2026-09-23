"""JFW-13: Protokolldokument ``jfw13_minutes_v1`` und Byte-Regel (I/O-frei).

Kanonische Serialisierung identisch zu ``jfw4_export_v1`` (``sort_keys``,
Trenner ``(",", ":")``, ``ensure_ascii=False``, UTF-8/LF, genau ein
abschliessender Zeilenumbruch). Byte-Regel (AC 88): identische Eingangs- UND
Registerrevision liefert byteidentische Protokollbytes; eine nicht
reproduzierbare Modellantwort erzeugt NIE eine zweite Fassung, sondern eine
sichtbar dokumentierte, gesondert versionierte Ergebnisrevision
(``nondeterminism_revision``) — genau ein autoritatives Ergebnis je Schluessel.

Das Dokument enthaelt NIE die Zuordnungsinformation (Register nur als
Revision-Referenz) und nie Klarnamen.
"""
from __future__ import annotations

from .provenance import (
    CONTRACT_VERSION,
    canonical_bytes,
    canonical_hash,
    minutes_key,
)
from .redaction import redact_free_text

DOC_KEYS = (
    "contract_version", "minutes_key", "result_hash", "header", "aufgaben",
    "zusammenfassung", "transkript", "warnliste", "reident_hinweise",
    "register", "provenance",
)

__all__ = ["DOC_KEYS", "build_document", "canonical_bytes", "nondeterminism_revision",
           "verify_document"]


def _redact_tasks(tasks: dict, register: dict) -> dict:
    out = []
    for task in tasks.get("tasks") or []:
        item = dict(task)
        item["action"] = redact_free_text(item.get("action") or "", register)
        out.append(item)
    return {"tasks": out, "verworfen": list(tasks.get("verworfen") or []),
            "getrennte_punkte": [
                dict(p, action=redact_free_text(p.get("action") or "", register))
                for p in (tasks.get("getrennte_punkte") or [])]}


def _redact_summary(summary: dict, register: dict) -> dict:
    def section(items):
        out = []
        for item in items or []:
            entry = dict(item)
            entry["text"] = redact_free_text(entry.get("text") or "", register)
            entry["quotes"] = [redact_free_text(q, register)
                               for q in (entry.get("quotes") or [])]
            out.append(entry)
        return out

    return {"kurz": section(summary.get("kurz")), "lang": section(summary.get("lang")),
            "verworfen": list(summary.get("verworfen") or [])}


def _redact_transcript(entries, register: dict) -> list[dict]:
    out = []
    for entry in entries or []:
        item = {k: v for k, v in entry.items() if k != "source_text"}
        item["text"] = redact_free_text(item.get("text") or "", register)
        out.append(item)
    return out


def build_document(request, source_document: dict, register: dict, sections: dict,
                   model_provenance: dict, attempt_id: str) -> dict:
    tasks = _redact_tasks(sections["tasks"], register)
    summary = _redact_summary(sections["summary"], register)
    transcript_section = sections.get("transcript") or sections.get("transkript")
    transkript = _redact_transcript(transcript_section["entries"], register)

    datum = sections.get("datum") or None
    warnliste = [
        {"kind": w.get("kind"), "quelle": w.get("quelle")}
        for w in (sections["tasks"].get("verworfen") or [])
    ]
    warnliste = [
        {"kind": w.get("reason_code"), "quelle": "aufgaben"}
        for w in (sections["tasks"].get("verworfen") or [])
    ] + [
        {"kind": w.get("reason_code"), "quelle": "zusammenfassung"}
        for w in (summary.get("verworfen") or [])
    ]
    if datum is None:
        warnliste.append({"kind": "datum_nicht_belegt", "quelle": "kopf"})

    header = {
        "datum": (datum or {}).get("value"),
        "datum_beleg": (datum or {}).get("evidence"),
        "dauer_ms": int(request.audio_duration_ms),
        "teilnehmer_pseudonyme": sorted({
            e["pseudonym"] for e in register["entries"] if e.get("pseudonym")}),
    }
    document = {
        "contract_version": CONTRACT_VERSION,
        "minutes_key": minutes_key(request),
        "result_hash": None,
        "header": header,
        "aufgaben": tasks,
        "zusammenfassung": {"kurz": summary["kurz"], "lang": summary["lang"]},
        "transkript": transkript,
        "warnliste": warnliste,
        "reident_hinweise": sections.get("reident_hinweise", []),
        "register": {
            "register_id": register["register_id"],
            "revision_id": register["revision_id"],
            "revision": register["revision"],
            "status": register["status"],
        },
        "provenance": {
            "contract_version": CONTRACT_VERSION,
            "minutes_key": minutes_key(request),
            "attempt_id": attempt_id,
            "register_revision": register["revision"],
            "model": dict(model_provenance),
            "eingang": {
                "job_id": request.job_id,
                "audio_hash": request.audio_hash,
                "transcript_revision_id": request.transcript_revision_id,
                "transcript_revision_hash": request.transcript_revision_hash,
                "jfw2_result_hash": request.jfw2_result_hash,
                "jfw3_result_hash": request.jfw3_result_hash,
                "jfw4_export_key": request.jfw4_export_key,
                "jfw4_result_hash": request.jfw4_result_hash,
                "jfw11_commit_hash": request.jfw11_commit_hash,
            },
        },
    }
    assert set(document.keys()) == set(DOC_KEYS)
    without = {k: v for k, v in document.items() if k != "result_hash"}
    document["result_hash"] = canonical_hash(without)
    return {"document": document, "warnings": warnliste}


def nondeterminism_revision(authoritative: dict, rerun: dict,
                            revision_no: int = 1) -> dict | None:
    """Nicht reproduzierbare Modellantwort -> gesondert versionierte Ergebnisrevision."""
    if rerun.get("result_hash") == authoritative.get("result_hash"):
        return None
    return {
        "revision_no": revision_no,
        "result_hash": rerun.get("result_hash"),
        "authoritative_result_hash": authoritative.get("result_hash"),
        "reason_code": "modell_nicht_reproduzierbar",
        "authoritative_kept": True,
        "documented": True,
    }


def _verify_transcript_shape(source_document: dict, entries) -> list[str]:
    """Strukturpruefung der anonymisierten Ausgabe: Reihenfolge, Vollstaendigkeit
    und Zeichenbereiche gegen den Snapshot (die Texte sind per Definition
    anonymisiert und duerfen dem Quelltext nicht entsprechen)."""
    errors: list[str] = []
    expected = [t.get("turn_id") for t in source_document.get("turns") or []]
    actual = [e.get("turn_id") for e in entries or []]
    if len(actual) != len(set(actual)):
        errors.append("turn_doppelt")
    if actual != expected:
        errors.append("reihenfolge_oder_vollstaendigkeit_veraendert")
    words = {str(w.get("word_id")): w for w in source_document.get("words") or []}
    for entry in entries or []:
        turn = next((t for t in source_document.get("turns") or []
                     if t.get("turn_id") == entry.get("turn_id")), None)
        if turn is None:
            errors.append(f"turn_unbekannt:{entry.get('turn_id')}")
            continue
        starts = [int(words[str(w)]["char_start"]) for w in (turn.get("word_ids") or [])
                  if str(w) in words]
        ends = [int(words[str(w)]["char_end"]) for w in (turn.get("word_ids") or [])
                if str(w) in words]
        span = (min(starts), max(ends)) if starts else (0, 0)
        if (entry.get("char_start"), entry.get("char_end")) != span:
            errors.append(f"zeichenbereich_veraendert:{entry.get('turn_id')}")
    return errors


def verify_document(document: dict, source_document: dict) -> list[str]:
    errors: list[str] = []
    if set(document.keys()) != set(DOC_KEYS):
        errors.append("dokumentstruktur_veraendert")
    without = {k: v for k, v in document.items() if k != "result_hash"}
    if canonical_hash(without) != document.get("result_hash"):
        errors.append("ergebnis_hash_inkonsistent")
    header = document.get("header") or {}
    for field in ("datum", "dauer_ms", "teilnehmer_pseudonyme"):
        if field not in header:
            errors.append(f"kopfdaten_fehlen:{field}")
    if "entries" in (document.get("register") or {}):
        errors.append("zuordnung_im_dokument")
    errors.extend(_verify_transcript_shape(source_document, document.get("transkript") or []))
    known_turns = {t.get("turn_id") for t in source_document.get("turns") or []}
    refs = []
    for task in (document.get("aufgaben") or {}).get("tasks") or []:
        refs.extend(task.get("evidence") or [])
    for section in ((document.get("zusammenfassung") or {}).get("kurz") or []) + \
                   ((document.get("zusammenfassung") or {}).get("lang") or []):
        refs.extend(section.get("evidence") or [])
    for ref in refs:
        if not set(ref.get("turn_ids") or []) <= known_turns:
            errors.append("belegreferenz_unbekannt")
    return errors
