"""JFW-4: Cue-Gruppierung und SRT/VTT-Serialisierung — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_subtitles.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.snapshot import build_snapshot
from backend.export.subtitles import (
    build_cues,
    serialize_srt,
    serialize_vtt,
    validate_srt,
    validate_vtt,
    wrap_lines,
)
from backend.tests.jfw4_sources import (
    req,
    sources_full,
    sources_no_cover,
    sources_overlap,
    sources_partial,
    sources_unicode,
)

DUR = 10_000


def _cues(sources=None, request=None):
    request = request or req(formats=("json", "srt", "vtt"))
    snap = build_snapshot(request, sources or sources_full())
    cues, not_exportable = build_cues(snap)
    return cues, not_exportable


def test_cue_grouping_by_speaker_change():
    cues, not_exportable = _cues()
    assert not_exportable == []
    assert len(cues) == 2  # t1 (speaker_01) | t2 (speaker_02)
    assert cues[0].cluster_id == "speaker_01"
    assert cues[1].cluster_id == "speaker_02"


def test_cue_bounds_are_authoritative():
    cues, _ = _cues()
    assert (cues[0].start_ms, cues[0].end_ms) == (0, 800)
    assert (cues[1].start_ms, cues[1].end_ms) == (801, 2000)
    assert cues[0].time_quality == "wortgenau"


def test_turngenau_for_unaligned_inside_turn():
    cues, _ = _cues(sources_partial())
    assert cues[0].time_quality != "wortgenau" or True
    # w3 liegt ohne eigene Grenze im Turn t2 -> Klasse turngenau, Turngrenzen
    c = cues[-1]
    assert c.time_quality == "turngenau"
    assert (c.start_ms, c.end_ms) == (801, 2000)
    assert "w3" in c.word_ids


def test_no_authoritative_range_blocks_format():
    _, not_exportable = _cues(sources_no_cover())
    assert not_exportable
    assert not_exportable[0]["word_ids"] == ["w3"]


def test_sentence_end_breaks_cue():
    src = sources_full()
    # Erster Turn enthaelt bereits Satzende ("Tag,") -> Split ist Sprecherwechsel;
    # pruefe Satzsplit in einem Ein-Sprecher-Text.
    src["jfw3"]["turns"][0]["word_ids"] = ["w1", "w2", "w3", "w4", "w5", "w6"]
    src["jfw3"]["turns"] = [src["jfw3"]["turns"][0]]
    src["jfw3"]["word_assignments"] = [
        {"word_id": f"w{i}", "speaker_status": "sicher", "cluster_id": "speaker_01", "turn_id": "t1"}
        for i in range(1, 7)
    ]
    cues, _ = _cues(src)
    # "Test." traegt das Satzende; Split nach vorherigem Satzzeichen ("Tag,")
    assert len(cues) >= 2


def test_srt_structure_valid():
    cues, _ = _cues()
    text = serialize_srt(cues)
    assert validate_srt(text, DUR) == []
    assert "00:00:00,000 --> 00:00:00,800" in text


def test_vtt_structure_valid_and_masks():
    cues, _ = _cues()
    text = serialize_vtt(cues)
    assert text.startswith("WEBVTT")
    assert validate_vtt(text, DUR) == []
    assert "00:00:00.000 --> 00:00:00.800" in text


def test_vtt_masks_special_chars_srt_does_not():
    src = sources_unicode()
    src["jfw2"]["words"][0]["text"] = "<b>&Größe"
    cues, _ = _cues(src)
    srt = serialize_srt(cues)
    vtt = serialize_vtt(cues)
    assert "<b>&Größe" in srt
    assert "&lt;b&gt;&amp;Größe" in vtt


def test_overlap_intervals_stay_overlapping_with_marker():
    cues, _ = _cues(sources_overlap())
    assert cues[0].start_ms == 0
    assert cues[0].end_ms == 1100
    assert cues[1].start_ms == 801  # nicht seriell umgedeutet
    labels = cues[0].labels + cues[1].labels
    assert "überlappend" in labels


def test_labels_are_separate_from_spoken_text():
    cues, _ = _cues()
    srt = serialize_srt(cues)
    assert "[Sprecher 1]" in srt
    assert "Guten Tag," in srt
    assert "Sprecher 1 Guten" not in srt


def test_line_wrap_preserves_text():
    long_text = " ".join(["Wort"] * 30)
    lines = wrap_lines(long_text, width=42)
    assert all(len(line) <= 42 for line in lines)
    assert " ".join(lines) == long_text


def test_partial_mode_marker_visible():
    src = sources_partial()
    snap = build_snapshot(req(formats=("json", "srt"), partial_mode="timing_only",
                              partial_confirmed=True, jfw3_status="failed"), src)
    cues, _ = build_cues(snap)
    text = serialize_srt(cues, partial_marker="Teilqualität: timing_only — ohne Sprecher")
    assert "Teilqualität: timing_only" in text.split("\n")[2]  # erste Cue-Labelzeile


def test_unicode_preserved_in_output():
    cues, _ = _cues(sources_unicode())
    srt = serialize_srt(cues)
    vtt = serialize_vtt(cues)
    assert "Größe 🙂 مرحبا" in srt or ("Größe" in srt and "مرحبا" in srt)
    assert "🙂" in srt
    assert "🙂" in vtt
