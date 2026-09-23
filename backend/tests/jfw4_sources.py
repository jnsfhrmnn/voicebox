"""JFW-4: eingefrorene Quell-Fixtures fuer die Vertragstests (TDD).

Baut JFW-2-/JFW-3-/(optional) JFW-11-Quellpayloads deterministisch auf. Die
Fixtures sind die Eingangsautoritaet der Tests fuer No-invention, No-omission,
Overlap, Namen, Deduplizierung, Quellenluecken und Byte-Reproduzierbarkeit.
"""
from __future__ import annotations

from backend.export.provenance import ExportRequest, transcript_text_hash

TEXT = "Guten Tag, das ist ein Test."


def req(**over) -> ExportRequest:
    base = dict(
        job_id="job-1",
        audio_asset_id="asset-1",
        audio_hash="a" * 64,
        audio_duration_ms=10_000,
        timebase="audio_ms_v1",
        transcript_run_id="run-1",
        transcript_revision_id="rev-1",
        transcript_revision_hash="b" * 64,
        transcript_text_hash=transcript_text_hash(TEXT),
        jfw2_result_hash="1" * 64,
        jfw2_status="aligned",
        jfw3_result_hash="2" * 64,
        jfw3_status="diarized",
        jfw11_commit_hash=None,
        jfw11_status=None,
        jfw11_expected=False,
        formats=("json",),
        export_profile="lesbare_untertitel_v1",
        name_policy="neutral",
        partial_mode=None,
        partial_confirmed=False,
        target_dir=None,
    )
    base.update(over)
    return ExportRequest(**base)


def w(word_id, text, char_start, char_end, timing_status="aligned",
      start_ms=None, end_ms=None, reason_code=None) -> dict:
    return {
        "word_id": word_id,
        "text": text,
        "char_start": char_start,
        "char_end": char_end,
        "timing_status": timing_status,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "reason_code": reason_code,
    }


def words_full() -> list[dict]:
    return [
        w("w1", "Guten", 0, 5, start_ms=0, end_ms=400),
        w("w2", "Tag,", 6, 10, start_ms=401, end_ms=800),
        w("w3", "das", 11, 14, start_ms=801, end_ms=1100),
        w("w4", "ist", 15, 18, start_ms=1101, end_ms=1300),
        w("w5", "ein", 19, 22, start_ms=1301, end_ms=1500),
        w("w6", "Test.", 23, 28, start_ms=1501, end_ms=2000),
    ]


def clusters_full() -> list[dict]:
    return [
        {"cluster_id": "speaker_01", "display_label": "Sprecher 1"},
        {"cluster_id": "speaker_02", "display_label": "Sprecher 2"},
    ]


def turns_full() -> list[dict]:
    return [
        {"turn_id": "t1", "cluster_id": "speaker_01", "start_ms": 0, "end_ms": 800,
         "overlap": "nicht_ueberlappend", "word_ids": ["w1", "w2"]},
        {"turn_id": "t2", "cluster_id": "speaker_02", "start_ms": 801, "end_ms": 2000,
         "overlap": "nicht_ueberlappend", "word_ids": ["w3", "w4", "w5", "w6"]},
    ]


def assignments_full() -> list[dict]:
    return [
        {"word_id": "w1", "speaker_status": "sicher", "cluster_id": "speaker_01", "turn_id": "t1"},
        {"word_id": "w2", "speaker_status": "sicher", "cluster_id": "speaker_01", "turn_id": "t1"},
        {"word_id": "w3", "speaker_status": "sicher", "cluster_id": "speaker_02", "turn_id": "t2"},
        {"word_id": "w4", "speaker_status": "sicher", "cluster_id": "speaker_02", "turn_id": "t2"},
        {"word_id": "w5", "speaker_status": "sicher", "cluster_id": "speaker_02", "turn_id": "t2"},
        {"word_id": "w6", "speaker_status": "sicher", "cluster_id": "speaker_02", "turn_id": "t2"},
    ]


def sources_full() -> dict:
    return {
        "text": TEXT,
        "jfw2": {"result_hash": "1" * 64, "status": "aligned", "words": words_full()},
        "jfw3": {
            "result_hash": "2" * 64,
            "status": "diarized",
            "clusters": clusters_full(),
            "turns": turns_full(),
            "word_assignments": assignments_full(),
        },
        "segments": [],
        "jfw11": None,
    }


def sources_partial() -> dict:
    """JFW-2 teilweise ausgerichtet: w3 unaligned, aber vom Turn t2 umfasst."""
    src = sources_full()
    words = words_full()
    words[2] = w("w3", "das", 11, 14, timing_status="unaligned",
                 reason_code="keine_genaue_grenze")
    src["jfw2"] = {"result_hash": "1" * 64, "status": "partially_aligned", "words": words}
    src["jfw3"]["status"] = "partially_diarized"
    return src


def sources_no_cover() -> dict:
    """w3 unaligned OHNE umfassenden autoritativen Zeitbereich -> SRT/VTT blockiert."""
    src = sources_partial()
    src["jfw3"]["turns"] = [
        {"turn_id": "t1", "cluster_id": "speaker_01", "start_ms": 0, "end_ms": 800,
         "overlap": "nicht_ueberlappend", "word_ids": ["w1", "w2"]},
        {"turn_id": "t2", "cluster_id": "speaker_02", "start_ms": 1101, "end_ms": 2000,
         "overlap": "nicht_ueberlappend", "word_ids": ["w4", "w5", "w6"]},
    ]
    src["jfw3"]["word_assignments"] = [
        a for a in assignments_full() if a["word_id"] != "w3"
    ] + [{"word_id": "w3", "speaker_status": "nicht_zugeordnet", "cluster_id": None, "turn_id": None}]
    return src


def sources_overlap() -> dict:
    """Zwei Sprecher ueberlappen sich; Wortgrenzen ueberlappen echt (t1/t2)."""
    src = sources_full()
    src["jfw2"]["words"] = [
        w("w1", "Guten", 0, 5, start_ms=0, end_ms=400),
        w("w2", "Tag,", 6, 10, start_ms=401, end_ms=1100),
        w("w3", "das", 11, 14, start_ms=801, end_ms=1200),
        w("w4", "ist", 15, 18, start_ms=1201, end_ms=1400),
        w("w5", "ein", 19, 22, start_ms=1401, end_ms=1600),
        w("w6", "Test.", 23, 28, start_ms=1601, end_ms=2000),
    ]
    src["jfw3"]["turns"] = [
        {"turn_id": "t1", "cluster_id": "speaker_01", "start_ms": 0, "end_ms": 1100,
         "overlap": "ueberlappend", "word_ids": ["w1", "w2"]},
        {"turn_id": "t2", "cluster_id": "speaker_02", "start_ms": 801, "end_ms": 2000,
         "overlap": "ueberlappend", "word_ids": ["w3", "w4", "w5", "w6"]},
    ]
    return src


def sources_unicode() -> dict:
    text = "Größe 🙂 مرحبا"
    return {
        "text": text,
        "jfw2": {
            "result_hash": "1" * 64,
            "status": "aligned",
            "words": [
                w("u1", "Größe", 0, 5, start_ms=0, end_ms=500),
                w("u2", "🙂", 6, 7, start_ms=501, end_ms=900),
                w("u3", "مرحبا", 8, 13, start_ms=901, end_ms=1500),
            ],
        },
        "jfw3": {
            "result_hash": "2" * 64,
            "status": "diarized",
            "clusters": [{"cluster_id": "speaker_01", "display_label": "Sprecher 1"}],
            "turns": [{"turn_id": "t1", "cluster_id": "speaker_01", "start_ms": 0,
                       "end_ms": 1500, "overlap": "nicht_ueberlappend",
                       "word_ids": ["u1", "u2", "u3"]}],
            "word_assignments": [
                {"word_id": x, "speaker_status": "sicher", "cluster_id": "speaker_01", "turn_id": "t1"}
                for x in ("u1", "u2", "u3")
            ],
        },
        "segments": [],
        "jfw11": None,
    }


def sources_jfw11() -> dict:
    """Dual-Source mit bestaetigtem Duplikat, Namensmapping und Quellenluecke."""
    src = sources_full()
    src["jfw11"] = {
        "commit_hash": "e" * 64,
        "sync_status": "sync_ok",
        "dedupe_status": "duplikat_bestaetigt",
        "name_mapping_revision": "nm-3",
        "name_mappings": [
            {"cluster_id": "speaker_01", "name": "Erika", "state": "confirmed",
             "revision": "nm-3", "provenance": {"quelle": "manuell", "beleg": "b1"}},
            {"cluster_id": "speaker_02", "name": "Max", "state": "suggested",
             "revision": "nm-3", "provenance": {"quelle": "vorschlag", "beleg": "b2"}},
        ],
        "dedupe_decisions": [
            {"decision_id": "d1", "kind": "duplikat_bestaetigt",
             "canonical_source_id": "src-a", "source_ids": ["src-a", "src-b"],
             "provenance": {"regel": "identische_zeit"}},
        ],
        "source_representations": [
            {"source_id": "src-a", "source": "jfw11_track_a", "word_ids": ["w1", "w2"],
             "turn_id": "t1", "start_ms": 0, "end_ms": 800, "status": "belegt"},
            {"source_id": "src-b", "source": "jfw11_track_b", "word_ids": ["w1", "w2"],
             "turn_id": "t1b", "start_ms": 0, "end_ms": 800, "status": "belegt"},
        ],
        "overlap_regions": [],
        "source_gaps": [
            {"source_id": "src-b", "start_ms": 2500, "end_ms": 4000,
             "reason_code": "quelle_fehlend"},
        ],
    }
    return src


def req_jfw11(**over) -> ExportRequest:
    base = dict(
        jfw11_commit_hash="e" * 64,
        jfw11_status="secured_dual",
        jfw11_expected=True,
    )
    base.update(over)
    return req(**base)
