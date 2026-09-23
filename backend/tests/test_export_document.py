"""JFW-4: verlustfreies JSON `jfw4_export_v1` — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_document.py
"""
import copy
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.document import (
    build_set,
    canonical_bytes,
    canonical_json,
    verify_lossless,
)
from backend.export.provenance import export_key
from backend.export.snapshot import build_snapshot
from backend.tests.jfw4_sources import (
    req,
    req_jfw11,
    sources_full,
    sources_jfw11,
    sources_no_cover,
    sources_partial,
)

DOC_KEYS = {
    "contract_version", "export_key", "result_hash", "job", "transcript", "revisions",
    "options", "readiness", "speakers", "turns", "words", "source_representations",
    "dedupe_decisions", "name_mappings", "overlap_regions", "source_gaps", "cues",
    "presentation", "not_exportable_ranges",
}


def _doc(request=None, sources=None):
    request = request or req(formats=("json", "srt", "vtt"))
    sources = sources or sources_full()
    snap = build_snapshot(request, sources)
    built = build_set(snap, request)
    return built["document"], built


def test_document_structure_keys():
    doc, _ = _doc()
    assert set(doc.keys()) == DOC_KEYS
    assert doc["contract_version"] == "jfw4_export_v1"


def test_words_exactly_once_in_source_order():
    doc, _ = _doc()
    ids = [wd["word_id"] for wd in doc["words"]]
    assert ids == ["w1", "w2", "w3", "w4", "w5", "w6"]
    assert [wd["text"] for wd in doc["words"]] == ["Guten", "Tag,", "das", "ist", "ein", "Test."]
    assert [wd["char_start"] for wd in doc["words"]] == [0, 6, 11, 15, 19, 23]
    assert [wd["order"] for wd in doc["words"]] == [0, 1, 2, 3, 4, 5]


def test_bounds_only_for_aligned():
    doc, _ = _doc(req(formats=("json",)), sources_partial())
    w3 = next(wd for wd in doc["words"] if wd["word_id"] == "w3")
    assert w3["timing_status"] == "unaligned"
    assert w3["start_ms"] is None
    assert w3["end_ms"] is None
    assert w3["reason_code"] == "keine_genaue_grenze"
    w1 = doc["words"][0]
    assert (w1["start_ms"], w1["end_ms"]) == (0, 400)


def test_canonical_bytes_deterministic_three_runs():
    bytes_runs = {canonical_bytes(_doc()[0]) for _ in range(3)}
    assert len(bytes_runs) == 1
    assert next(iter(bytes_runs)).endswith(b"\n")


def test_canonical_bytes_stable_under_dict_order():
    doc, _ = _doc()
    reordered = json.loads(json.dumps(doc))
    assert canonical_bytes(doc) == canonical_bytes(reordered)
    assert canonical_json(doc).startswith("{")


def test_roundtrip_json():
    doc, _ = _doc()
    assert canonical_bytes(json.loads(canonical_bytes(doc).decode("utf-8"))) == canonical_bytes(doc)


def test_presentation_hashes_match_actual_files():
    doc, built = _doc()
    for entry in doc["presentation"]:
        data = built["files"][entry["file_name"]]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["bytes"] == len(data)
    assert {e["role"] for e in doc["presentation"]} == {"srt", "vtt"}


def test_result_hash_binds_content():
    doc1, _ = _doc()
    tampered = sources_full()
    tampered["jfw2"]["words"][0]["text"] = "Hallo"
    doc2, _ = _doc(req(formats=("json", "srt", "vtt"), jfw2_result_hash="1" * 64), tampered)
    assert doc1["result_hash"] != doc2["result_hash"]


def test_export_key_in_document_equals_provenance():
    r = req(formats=("json",))
    doc, _ = _doc(r, sources_full())
    assert doc["export_key"] == export_key(r)


def test_dedupe_source_representations_retained():
    doc, _ = _doc(req_jfw11(formats=("json",)), sources_jfw11())
    ids = {s["source_id"] for s in doc["source_representations"]}
    assert ids == {"src-a", "src-b"}
    assert doc["dedupe_decisions"][0]["kind"] == "duplikat_bestaetigt"
    # Worte erscheinen trotz Deduplizierung nur einmal in words
    assert len(doc["words"]) == 6


def test_name_mappings_original_states_kept():
    doc, _ = _doc(req_jfw11(name_policy="confirmed_names", formats=("json",)), sources_jfw11())
    states = {m["cluster_id"]: m["state"] for m in doc["name_mappings"]}
    assert states == {"speaker_01": "confirmed", "speaker_02": "suggested"}
    disp = {d["cluster_id"]: d for d in doc["speakers"]}
    assert disp["speaker_01"]["display_name"] == "Erika"
    assert disp["speaker_02"]["display_name"] is None
    assert disp["speaker_02"]["unauthorized_name_annotations"][0]["provenance"]["beleg"] == "b2"


def test_no_cover_ranges_are_named_not_invented():
    doc, built = _doc(req(formats=("json", "srt")), sources_no_cover())
    assert doc["not_exportable_ranges"]
    assert built["format_status"]["srt"] == "blockiert"
    for rng in doc["not_exportable_ranges"]:
        assert rng["start_ms"] is None
        assert rng["end_ms"] is None
        assert rng["word_ids"] == ["w3"]


def test_verify_lossless_detects_tampering():
    sources = sources_full()
    doc, _ = _doc(req(formats=("json",)), sources)
    assert verify_lossless(doc, sources) == []
    tampered = copy.deepcopy(doc)
    tampered["words"] = tampered["words"][:-1]
    assert verify_lossless(tampered, sources)
    tampered2 = copy.deepcopy(doc)
    tampered2["words"][0]["text"] = "Hallo"
    assert verify_lossless(tampered2, sources)


def test_options_and_revisions_documented():
    doc, _ = _doc(req_jfw11(name_policy="confirmed_names", formats=("json", "srt", "vtt")),
                  sources_jfw11())
    assert doc["options"]["name_policy"] == "confirmed_names"
    assert set(doc["options"]["formats"]) == {"json", "srt", "vtt"}
    assert doc["revisions"]["jfw2"]["result_hash"] == "1" * 64
    assert doc["revisions"]["jfw11"]["commit_hash"] == "e" * 64
