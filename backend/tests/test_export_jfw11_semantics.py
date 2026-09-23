"""JFW-4: Namens-, Deduplizierungs-, Quellen- und Markersemantik (TDD).

Schliesst die QA-Luecken der AC-Gruppe „Sprecher, Namen, Overlap, Quellen und
Deduplizierung" (98-109), die Readiness-Zaehler (75), den Cancel-Race-Schutz
(120) sowie die Randfaelle leere Sprache und sehr lange Cues (136).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_jfw11_semantics.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.document import build_set
from backend.export.provenance import transcript_text_hash
from backend.export.snapshot import build_snapshot
from backend.export.subtitles import build_cues, serialize_srt, validate_srt
from backend.tests.jfw4_sources import (
    assignments_full,
    clusters_full,
    req,
    req_jfw11,
    sources_full,
    sources_jfw11,
    sources_partial,
    turns_full,
    w,
    words_full,
)
from backend.tests.test_export_router import _body, _session


def _display(snapshot) -> dict:
    return {s["cluster_id"]: s for s in snapshot.speakers_display}


def _cue_labels(cues) -> set:
    out = set()
    for cue in cues:
        out |= set(cue.labels)
    return out


def test_unbestaetigte_namen_bleiben_neutrale_annotation():
    snap = build_snapshot(req_jfw11(name_policy="confirmed_names"), sources_jfw11())
    disp = _display(snap)
    c1, c2 = disp["speaker_01"], disp["speaker_02"]
    # bestaetigter Name darf erscheinen ...
    assert c1["display_name"] == "Erika"
    assert c1["display_name_source"] == "confirmed"
    # ... ein Vorschlag niemals als bestaetigter Personenname
    assert c2["display_name"] is None
    assert c2["mapping_state"] == "suggested"
    names = [a["name"] for a in c2["unauthorized_name_annotations"]]
    assert "Max" in names  # als klar nicht autorisierte Annotation erhalten
    # Präsentation verwendet ausschliesslich das neutrale Clusterlabel
    cues, _ = build_cues(snap)
    assert c2["display_label"] in (cues[-1].label or "")


def test_bestaetigter_name_mit_provenienz_ersetzt_keinen_text():
    src = sources_jfw11()
    snap = build_snapshot(req_jfw11(name_policy="confirmed_names"), src)
    built = build_set(snap, snap.request)
    doc = built["document"]
    # Originaler Mappingzustand samt Provenienz unveraendert
    assert doc["name_mappings"] == src["jfw11"]["name_mappings"]
    entry = _display(snap)["speaker_01"]
    assert entry["mapping_provenance"] == {"quelle": "manuell", "beleg": "b1"}
    # getrennte Anzeigeentscheide ersetzen weder Text noch Maschinen-ID
    assert [wd["text"] for wd in doc["words"]] == [x["text"] for x in words_full()]
    assert [wd["word_id"] for wd in doc["words"]] == [x["word_id"] for x in words_full()]


def test_namensnutzung_deaktiviert_bleibt_neutral():
    snap = build_snapshot(req_jfw11(name_policy="neutral"), sources_jfw11())
    assert all(s["display_name"] is None for s in snap.speakers_display)
    built = build_set(snap, snap.request)
    doc = built["document"]
    assert doc["options"]["name_policy"] == "neutral"
    # gebundene Mapping-Revision bleibt ohne stillen Datenverlust dokumentiert
    assert doc["revisions"]["jfw11"]["commit_hash"] == "e" * 64
    assert doc["name_mappings"] == sources_jfw11()["jfw11"]["name_mappings"]


def test_unsicher_zugeordnet_wird_nicht_wahrscheinlichst_zugeschrieben():
    src = sources_full()
    src["jfw3"]["word_assignments"] = [
        {**a, "speaker_status": "unsicher"} if a["word_id"] == "w2" else a
        for a in assignments_full()
    ]
    snap = build_snapshot(req(formats=("json", "srt")), src)
    cues, _ = build_cues(snap)
    cue = next(c for c in cues if "w2" in c.word_ids)
    assert "unsicher" in cue.labels  # neutraler Unbekannt-/Unsicherheitsstatus
    built = build_set(snap, snap.request)
    doc_w2 = next(x for x in built["document"]["words"] if x["word_id"] == "w2")
    assert doc_w2["speaker_status"] == "unsicher"  # unveraendert sichtbar erhalten


def test_overlap_unsicher_marker():
    src = sources_full()
    src["jfw3"]["turns"] = [
        {**t, "overlap": "overlap_unsicher"} for t in turns_full()
    ]
    snap = build_snapshot(req(formats=("json", "srt")), src)
    cues, _ = build_cues(snap)
    assert "overlap_unsicher" in _cue_labels(cues)


def test_quellenbegrenzt_marker():
    src = sources_jfw11()
    src["jfw11"]["source_gaps"] = [
        {"source_id": "src-b", "start_ms": 801, "end_ms": 1200,
         "reason_code": "quelle_fehlend"},
    ]
    snap = build_snapshot(req_jfw11(formats=("json", "srt")), src)
    cues, _ = build_cues(snap)
    assert "quellenbegrenzt" in _cue_labels(cues)


def _dedupe_unsicher_sources() -> dict:
    src = sources_jfw11()
    src["jfw11"]["dedupe_decisions"] = [
        {"decision_id": "d1", "kind": "dedupe_unsicher",
         "canonical_source_id": "src-a", "source_ids": ["src-a", "src-b"],
         "provenance": {"regel": "zeit_unsicher"}},
        {"decision_id": "d2", "kind": "getrennt_behalten",
         "canonical_source_id": "src-c", "source_ids": ["src-c"],
         "provenance": {"regel": "verschiedene_traeger"}},
    ]
    src["jfw11"]["source_representations"] = [
        {"source_id": "src-a", "source": "jfw11_track_a", "word_ids": ["w1", "w2"],
         "turn_id": "t1", "start_ms": 0, "end_ms": 800, "status": "belegt"},
        {"source_id": "src-b", "source": "jfw11_track_b", "word_ids": ["w1", "w2"],
         "turn_id": "t1b", "start_ms": 0, "end_ms": 800, "status": "belegt"},
        {"source_id": "src-c", "source": "jfw11_track_b", "word_ids": ["w5", "w6"],
         "turn_id": "t2", "start_ms": 1301, "end_ms": 2000, "status": "belegt"},
    ]
    return src


def test_dedupe_unsicher_und_getrennt_behalten_sichtbar():
    snap = build_snapshot(req_jfw11(formats=("json", "srt")), _dedupe_unsicher_sources())
    cues, _ = build_cues(snap)
    assert "Deduplizierung unsicher" in _cue_labels(cues)
    assert "Getrennt behalten" in _cue_labels(cues)
    # nichts entfernt: alle sechs Woerter bleiben praesent
    present = [wid for c in cues for wid in c.word_ids]
    assert sorted(present) == ["w1", "w2", "w3", "w4", "w5", "w6"]


def test_dedupe_bestaetigt_scheint_genau_einmal_auf():
    src = sources_jfw11()
    snap = build_snapshot(req_jfw11(formats=("json", "srt", "vtt")), src)
    cues, _ = build_cues(snap)
    present = [wid for c in cues for wid in c.word_ids]
    assert len(present) == len(set(present)) == 6  # kanonisch genau einmal
    built = build_set(snap, snap.request)
    doc = built["document"]
    # jede gebundene Quellenrepraesentation mit urspruenglichen Zeiten genau einmal
    assert doc["source_representations"] == src["jfw11"]["source_representations"]
    assert doc["dedupe_decisions"] == src["jfw11"]["dedupe_decisions"]


def test_readiness_zaehler_sind_real():
    snap_p = build_snapshot(
        req(jfw2_status="partially_aligned", jfw3_status="partially_diarized"),
        sources_partial(),
    )
    counts = {x["dimension"]: x["count"] for x in snap_p.readiness["warnings"]}
    assert counts["timing"] == 1  # w3 ohne eigene Grenze
    assert counts["speakers"] == 1  # w3 nicht_zugeordnet
    snap_j = build_snapshot(req_jfw11(), sources_jfw11())
    counts_j = {x["dimension"]: x["count"] for x in snap_j.readiness["warnings"]}
    assert counts_j["sources"] == 1  # genau eine Quellenluecke
    snap_d = build_snapshot(req_jfw11(), _dedupe_unsicher_sources())
    counts_d = {x["dimension"]: x["count"] for x in snap_d.readiness["warnings"]}
    assert counts_d["dedupe"] == 2  # dedupe_unsicher + getrennt_behalten


def test_leere_sprache_blocked():
    src = {
        "text": "",
        "jfw2": {"result_hash": "1" * 64, "status": "no_alignable_speech", "words": []},
        "jfw3": {"result_hash": "2" * 64, "status": "no_speech",
                 "clusters": [], "turns": [], "word_assignments": []},
        "segments": [],
        "jfw11": None,
    }
    request = req(
        transcript_text_hash=transcript_text_hash(""),
        jfw2_status="no_alignable_speech",
        jfw3_status="no_speech",
    )
    snap = build_snapshot(request, src)
    assert snap.readiness["state"] == "blocked"
    assert snap.readiness["reason_code"] == "kein_mindestinhalt"


def test_sehr_langer_cue_block_bleibt_formal_gueltig():
    words = [w(f"x{i}", "bla", i * 5, i * 5 + 4, start_ms=i * 20, end_ms=i * 20 + 15)
             for i in range(300)]
    text = " ".join(x["text"] for x in words)
    src = {
        "text": text,
        "jfw2": {"result_hash": "1" * 64, "status": "aligned", "words": words},
        "jfw3": {
            "result_hash": "2" * 64,
            "status": "diarized",
            "clusters": clusters_full(),
            "turns": [{"turn_id": "t1", "cluster_id": "speaker_01", "start_ms": 0,
                       "end_ms": 6000, "overlap": "nicht_ueberlappend",
                       "word_ids": [x["word_id"] for x in words]}],
            "word_assignments": [
                {"word_id": x["word_id"], "speaker_status": "sicher",
                 "cluster_id": "speaker_01", "turn_id": "t1"}
                for x in words
            ],
        },
        "segments": [],
        "jfw11": None,
    }
    request = req(transcript_text_hash=transcript_text_hash(text),
                  formats=("json", "srt"))
    snap = build_snapshot(request, src)
    cues, not_exportable = build_cues(snap)
    assert not_exportable == []
    assert len(cues) > 1  # lesbare Gruppierung statt Monolith
    srt = serialize_srt(cues)
    assert validate_srt(srt, request.audio_duration_ms) == []
    # kein Textverlust
    assert " ".join(c.text for c in cues) == text


def test_cancel_race_vor_commit_hinterlaesst_keinen_set():
    db = _session()
    target = Path(tempfile.mkdtemp(prefix="jfw_export_target_"))
    from backend.routes import export as export_routes

    real_commit = export_routes.store.commit_set

    def _race(db_, key, manifest, result_hash):
        # Abbruch trifft vor dem Set-Commit ein und gewinnt die DB-Race
        export_routes.store.cancel_export(db_, key)
        return real_commit(db_, key, manifest=manifest, result_hash=result_hash)

    export_routes.store.commit_set = _race
    try:
        out = export_routes.run_export(_body(target), db)
    finally:
        export_routes.store.commit_set = real_commit
    assert out["outcome"] == "canceled"
    assert not any((target / n).exists() for n in out["expected_files"])
    assert sorted(p.name for p in target.iterdir()) == []  # Kompensation bereinigt
