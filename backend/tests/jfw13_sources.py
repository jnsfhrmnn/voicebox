"""JFW-13: eingefrorene Quell-Fixtures fuer die Vertragstests (TDD).

Erzeugt ECHTE ``jfw4_export_v1``-Dokumente ueber die JFW-4-Vertragsschicht
(``build_snapshot``/``build_set``) — JFW-13 ist ausschliesslich Konsument
dieses Dokuments. Dazu Namens-/Kontakt-Text-Fixtures mit Kandidaten-,
Aufgaben- und Zusammenfassungs-Vorschlaegen (Modell-Ausgaben als Daten).
"""
from __future__ import annotations

import re

from backend.export.document import build_set
from backend.export.snapshot import build_snapshot
from backend.tests.jfw4_sources import (
    req,
    req_jfw11,
    sources_full,
    sources_jfw11,
)

TEXT = (
    "Guten Tag, hier spricht Erika Muster von der Muster GmbH, erika@muster.de. "
    "Max Beispiel uebernimmt die Aufgabe bis Freitag. "
    "Wir entscheiden uns fuer Option A."
)

MS_PER_WORD = 500

TURN_ORDER = {"t1": 0, "t2": 1, "t3": 2}

# Feste Turn-Zuordnung ueber Wortindizes (Satzgrenzen wegen "muster.de." unbrauchbar).
TURN_PLAN = (
    ("t1", "speaker_01", 0, 11),
    ("t2", "speaker_02", 11, 18),
    ("t3", "speaker_01", 18, 24),
)

_SPAN_ERIKA = (TEXT.index("Erika Muster"), TEXT.index("Erika Muster") + len("Erika Muster"))
_SPAN_ORG = (TEXT.index("Muster GmbH"), TEXT.index("Muster GmbH") + len("Muster GmbH"))
_SPAN_MAIL = (TEXT.index("erika@muster.de"), TEXT.index("erika@muster.de") + len("erika@muster.de"))
_SPAN_MAX = (TEXT.index("Max Beispiel"), TEXT.index("Max Beispiel") + len("Max Beispiel"))


def words_from_text(text: str) -> list[dict]:
    words = []
    for i, m in enumerate(re.finditer(r"\S+", text)):
        words.append({
            "word_id": f"w{i + 1}",
            "text": m.group(0),
            "char_start": m.start(),
            "char_end": m.end(),
            "timing_status": "aligned",
            "start_ms": i * MS_PER_WORD,
            "end_ms": i * MS_PER_WORD + (MS_PER_WORD - 10),
            "reason_code": None,
        })
    return words


def turn_range(words: list[dict], lo: int, hi: int) -> tuple[int, int]:
    group = words[lo:hi]
    return group[0]["start_ms"], group[-1]["end_ms"]


def ev(turn_id: str) -> dict:
    """Belegreferenz mit den REALLEN Zeiten des Turns aus ``TURN_PLAN``."""
    words = words_from_text(TEXT)
    for tid, _cluster, lo, hi in TURN_PLAN:
        if tid == turn_id:
            start_ms, end_ms = turn_range(words, lo, hi)
            return {"turn_ids": [tid], "start_ms": start_ms, "end_ms": end_ms}
    raise KeyError(turn_id)


def sources_names(with_jfw11: bool = False) -> dict:
    """Drei Turns mit Personen-, Organisations- und Kontakt-Erkennungen."""
    words = words_from_text(TEXT)
    turns = []
    assignments = []
    for tid, cluster, lo, hi in TURN_PLAN:
        start_ms, end_ms = turn_range(words, lo, hi)
        turn_words = words[lo:hi]
        turns.append({
            "turn_id": tid,
            "cluster_id": cluster,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "overlap": "nicht_ueberlappend",
            "word_ids": [w["word_id"] for w in turn_words],
        })
        for w in turn_words:
            assignments.append({
                "word_id": w["word_id"],
                "speaker_status": "sicher",
                "cluster_id": cluster,
                "turn_id": tid,
            })
    src = {
        "text": TEXT,
        "jfw2": {"result_hash": "1" * 64, "status": "aligned", "words": words},
        "jfw3": {
            "result_hash": "2" * 64,
            "status": "diarized",
            "clusters": [
                {"cluster_id": "speaker_01", "display_label": "Sprecher 1"},
                {"cluster_id": "speaker_02", "display_label": "Sprecher 2"},
            ],
            "turns": turns,
            "word_assignments": assignments,
        },
        "segments": [],
        "jfw11": None,
    }
    if with_jfw11:
        j = sources_jfw11()
        src["jfw11"] = j["jfw11"]
    return src


def jfw4_doc(sources: dict | None = None, request=None) -> dict:
    src = sources if sources is not None else sources_full()
    r = request or req()
    return build_set(build_snapshot(r, src), r)["document"]


def names_doc(with_jfw11: bool = False) -> dict:
    src = sources_names(with_jfw11=with_jfw11)
    r = req_jfw11() if with_jfw11 else req()
    return build_set(build_snapshot(r, src), r)["document"]


def req13_kwargs(doc: dict, **over) -> dict:
    """MinutesRequest-Kwargs aus dem echten JFW-4-Dokument abgeleitet."""
    jfw11 = doc["revisions"]["jfw11"]
    base = dict(
        job_id=doc["job"]["job_id"],
        audio_asset_id=doc["job"]["audio_asset_id"],
        audio_hash=doc["job"]["audio_hash"],
        audio_duration_ms=doc["job"]["audio_duration_ms"],
        timebase=doc["job"]["timebase"],
        transcript_run_id=doc["transcript"]["run_id"],
        transcript_revision_id=doc["transcript"]["revision_id"],
        transcript_revision_hash=doc["transcript"]["revision_hash"],
        transcript_text_hash=doc["transcript"]["text_hash"],
        jfw2_result_hash=doc["revisions"]["jfw2"]["result_hash"],
        jfw2_status=doc["revisions"]["jfw2"]["status"],
        jfw3_result_hash=doc["revisions"]["jfw3"]["result_hash"],
        jfw3_status=doc["revisions"]["jfw3"]["status"],
        jfw4_export_key=doc["export_key"],
        jfw4_result_hash=doc["result_hash"],
        jfw11_commit_hash=jfw11["commit_hash"] if jfw11 else None,
        jfw11_status=jfw11["status"] if jfw11 else None,
        jfw11_expected=jfw11 is not None,
        register_revision="reg-1",
    )
    base.update(over)
    return base


def candidates_names() -> list[dict]:
    return [
        {"candidate_id": "c1", "kind": "person", "text": "Erika Muster",
         "occurrences": [{"turn_id": "t1", "start": _SPAN_ERIKA[0], "end": _SPAN_ERIKA[1]}]},
        {"candidate_id": "c2", "kind": "organisation", "text": "Muster GmbH",
         "occurrences": [{"turn_id": "t1", "start": _SPAN_ORG[0], "end": _SPAN_ORG[1]}]},
        {"candidate_id": "c3", "kind": "kontakt", "text": "erika@muster.de",
         "occurrences": [{"turn_id": "t1", "start": _SPAN_MAIL[0], "end": _SPAN_MAIL[1]}]},
        {"candidate_id": "c4", "kind": "person", "text": "Max Beispiel",
         "occurrences": [{"turn_id": "t2", "start": _SPAN_MAX[0], "end": _SPAN_MAX[1]}]},
    ]


def tasks_proposals() -> list[dict]:
    return [
        {"task_id": "a1", "task_group": "g1", "kind": "aufgabe",
         "action": "Aufgabe uebernehmen",
         "assignee": {"candidate_id": "c4"},
         "due": "Freitag", "status_hint": "offen",
         "evidence": [ev("t2")]},
        {"task_id": "a2", "task_group": "g2", "kind": "entscheidung",
         "action": "Option A gewaehlt",
         "evidence": [ev("t3")]},
        {"task_id": "a3", "task_group": "g3", "kind": "aufgabe",
         "action": "Nicht belegte Aktion",
         "evidence": [{"turn_ids": ["t99"], "start_ms": 0, "end_ms": 1}]},
    ]


def tasks_duplicates() -> list[dict]:
    """Dieselbe Aufgabe zweimal genannt — spaetere Fassung gewinnt."""
    base = tasks_proposals()[0]
    frueh = dict(base, task_id="a0", action="Aufgabe vorbereiten",
                 evidence=[ev("t1")])
    return [frueh, base]


def summary_proposals() -> list[dict]:
    return [
        {"statement_id": "s1", "kind": "aussage", "status": "belegt",
         "text": "Die Muster GmbH meldet sich.",
         "quotes": ["Muster GmbH"],
         "evidence": [ev("t1")]},
        {"statement_id": "s2", "kind": "entscheidung", "status": "belegt",
         "text": "Option A wird umgesetzt.",
         "quotes": ["entscheiden uns fuer Option A"],
         "evidence": [ev("t3")]},
        {"statement_id": "s3", "kind": "aussage", "status": "unklar",
         "text": "Moeeglicherweise gibt es einen Anhang.",
         "quotes": [],
         "evidence": [ev("t2")]},
        {"statement_id": "s4", "kind": "aussage", "status": "belegt",
         "text": "Erfundene Aussage ohne Beleg.",
         "quotes": [],
         "evidence": []},
        {"statement_id": "s5", "kind": "aussage", "status": "belegt",
         "text": "Aussage mit nicht belegtem Zitat.",
         "quotes": ["Das steht nirgends"],
         "evidence": [ev("t1")]},
    ]
