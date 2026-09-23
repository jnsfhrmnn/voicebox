"""JFW-3: Ergebnisvertrag Diarisierung — Vertragstests.

Zustandsableitung (diarized / partially_diarized / no_speech / failed), Turn-
Zeitintegrität, neutrale Cluster-IDs mit getrennten Anzeigeetiketten, Overlap-
Status, heiliger Unveränderlichkeits-Check der JFW-2-Wortliste
(``verify_words_unchanged``), Wortzuordnungs-Overlay, Abdeckungspartition
(Intervall-Unionen) und kanonischer Ergebnis-Hash.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_diarization_contract.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.diarization.contract import (
    OVERLAP,
    OVERLAP_NONE,
    OVERLAP_UNCERTAIN,
    REASON_CODES,
    RESULT_CONTRACT_VERSION,
    TURN_ASSIGNED,
    TURN_UNASSIGNED,
    TURN_UNCERTAIN,
    WORD_AMBIGUOUS,
    WORD_ASSIGNED,
    WORD_OVERLAP,
    WORD_UNASSIGNED,
    build_result,
    coverage,
    derive_state,
    verify_words_unchanged,
)

DURATION_MS = 60000.0
SPEC_AUTO = {"mode": "auto", "count": None, "minimum": None, "maximum": None}

TEXT = "hallo welt das ist ein test"

def words_with_times(times):
    """Minimaler JFW-2-Wortvertrag: Wort i hat Grenzen ``times[i]`` (oder None)."""
    out = []
    pos = 0
    for i, t in enumerate(times):
        word = TEXT.split()[i % len(TEXT.split())]
        out.append(
            {
                "word_id": f"w-{i:04d}",
                "order": i,
                "char_start": pos,
                "char_end": pos + len(word),
                "text": word,
                "status": "alignable",
                "start_ms": t[0] if t else None,
                "end_ms": t[1] if t else None,
            }
        )
        pos += len(word) + 1
    return out

def turn(start, end, key=None, candidates=None, hint="unknown", score=0.9):
    return {
        "start_ms": float(start),
        "end_ms": float(end),
        "speaker_key": key,
        "candidates": candidates,
        "overlap_hint": hint,
        "score": score,
    }

def build(words, turns, spec=SPEC_AUTO, duration=DURATION_MS):
    return build_result(
        words,
        turns,
        duration_ms=duration,
        provider="fake",
        speaker_spec=spec,
        frame_ms=20.0,
    )

# --- Zustandsableitung ------------------------------------------------------

def test_full_single_assignment_is_diarized():
    words = words_with_times([(0, 400), (1000, 1400)])
    res = build(
        words,
        [turn(0, 900, "A"), turn(1000, 1900, "B")],
    )
    assert res["status"] == "diarized"
    assert res["contract_version"] == RESULT_CONTRACT_VERSION
    assert res["coverage"]["usable_ms"] == res["coverage"]["speech_ms"]

def test_coverage_95_to_99_is_partially_diarized():
    # 20 gleich lange Turns à 1000 ms, einer unsicher -> 19/20 = 95 %.
    turns = [turn(i * 1000, i * 1000 + 1000, "A") for i in range(20)]
    turns[0] = turn(0, 1000, "A", hint="uncertain")
    res = build([], turns)
    assert res["status"] == "partially_diarized"
    assert res["coverage"]["unsicher_ms"] == 1000.0
    assert res["coverage"]["usable_ms"] == 19000.0

def test_below_95_percent_is_failed():
    turns = [turn(i * 1000, i * 1000 + 1000, "A", hint="uncertain") for i in range(20)]
    turns[0] = turn(0, 1000, "A")
    res = build([], turns)
    assert res["status"] == "failed"  # 1/20 = 5 %
    assert res["reason_code"] in (None, "no_acoustic_match")

def test_no_turns_is_no_speech_without_percentage():
    res = build([], [])
    assert res["status"] == "no_speech"
    assert res["coverage"]["speech_ms"] == 0.0
    assert "coverage_percent" not in res
    assert res["clusters"] == []

# --- Turn-Integrität --------------------------------------------------------

def test_invalid_turn_boundary_marks_integrity_failure():
    words = words_with_times([(0, 400)])
    res = build(words, [turn(0, 900, "A"), turn(1000, 999999.0, "B")])
    assert res["status"] == "failed"  # Integritätsprüfung scheitert
    assert res["reason_code"] == "integrity_error"
    assert res["counters"]["invalid_turn_count"] == 1
    for t in res["turns"]:
        assert 0 <= t["start_ms"] < t["end_ms"] <= DURATION_MS

def test_non_finite_turn_is_integrity_failure():
    res = build([], [turn(float("nan"), 1000, "A")])
    assert res["status"] == "failed"
    assert res["counters"]["invalid_turn_count"] == 1

def test_stored_turns_carry_all_required_fields():
    words = words_with_times([(0, 400)])
    res = build(words, [turn(0, 900, "A")])
    t = res["turns"][0]
    assert t["turn_id"] == "t-0000"
    assert t["cluster_candidates"] == ["speaker_01"]
    assert t["assignment_status"] == TURN_ASSIGNED
    assert t["overlap_status"] == OVERLAP_NONE
    assert t["quality"]["provider"] == "fake"

# --- Neutrale Cluster -------------------------------------------------------

def test_cluster_ids_are_neutral_and_deterministic():
    res = build([], [turn(2000, 3000, "B"), turn(0, 900, "A"), turn(4000, 5000, "B")])
    # Reihenfolge nach erster Turn-Startzeit: A -> speaker_01, B -> speaker_02.
    assert [c["cluster_id"] for c in res["clusters"]] == ["speaker_01", "speaker_02"]
    assert [c["display_label"] for c in res["clusters"]] == ["Sprecher 1", "Sprecher 2"]
    # Keine Namen, keine jobübergreifende Identität.
    for c in res["clusters"]:
        assert set(c) == {"cluster_id", "display_label"}

def test_same_result_recomputed_is_identical():
    turns = [turn(0, 900, "A"), turn(2000, 3000, "B")]
    words = words_with_times([(0, 400)])
    r1 = build(words, turns)
    r2 = build(words, list(turns))
    assert r1["result_hash"] == r2["result_hash"]
    assert [c["cluster_id"] for c in r1["clusters"]] == [
        c["cluster_id"] for c in r2["clusters"]
    ]

def test_result_hash_is_sensitive():
    words = words_with_times([(0, 400)])
    r1 = build(words, [turn(0, 900, "A")])
    r2 = build(words, [turn(0, 901, "A")])
    assert r1["result_hash"] != r2["result_hash"]

# --- Overlap und Unsicherheit ----------------------------------------------

def test_time_detected_overlap_is_ueberlappend_with_candidates():
    words = words_with_times([(0, 400)])
    res = build(words, [turn(0, 2000, "A"), turn(1000, 3000, "B")])
    by_key = {t["quality"]["speaker_key"]: t for t in res["turns"]}
    ta, tb = by_key["A"], by_key["B"]
    for t in (ta, tb):
        assert t["overlap_status"] == OVERLAP
        assert t["cluster_candidates"] == ["speaker_01", "speaker_02"]
        assert t["assignment_status"] == TURN_ASSIGNED  # belegte Kandidaten
    # Overlap mit >= 2 belegten Kandidaten ist verwertbar.
    assert res["coverage"]["overlap_ms"] > 0
    assert res["counters"]["overlap_count"] == 2

def test_overlap_hint_uncertain_is_overlap_unsicher():
    res = build([], [turn(0, 2000, "A", hint="uncertain"), turn(1000, 3000, "B")])
    statuses = {(t["assignment_status"], t["overlap_status"]) for t in res["turns"]}
    assert (TURN_UNCERTAIN, OVERLAP_UNCERTAIN) in statuses
    # Nicht verwertbar, bleibt im Nenner sichtbar.
    assert res["coverage"]["overlap_unsicher_ms"] > 0
    assert res["coverage"]["usable_ms"] < res["coverage"]["speech_ms"]

def test_overlap_with_single_candidate_is_overlap_unsicher():
    res = build([], [turn(0, 2000, "A", hint="yes"), turn(1000, 3000, None, hint="yes")])
    for t in res["turns"]:
        assert t["overlap_status"] in (OVERLAP_UNCERTAIN, OVERLAP)
        if t["overlap_status"] == OVERLAP:
            assert len(t["cluster_candidates"]) >= 2
    assert res["coverage"]["overlap_unsicher_ms"] > 0

def test_uncertain_single_assignment_is_unsicher():
    res = build([], [turn(0, 2000, "A", hint="uncertain")])
    t = res["turns"][0]
    assert t["assignment_status"] == TURN_UNCERTAIN
    assert t["overlap_status"] == OVERLAP_NONE
    assert res["coverage"]["unsicher_ms"] == 2000.0
    assert res["coverage"]["usable_ms"] == 0.0

def test_no_candidates_is_nicht_zugeordnet():
    res = build([], [turn(0, 2000, None)])
    t = res["turns"][0]
    assert t["assignment_status"] == TURN_UNASSIGNED
    assert t["cluster_candidates"] == []
    assert t["reason_code"] == "no_acoustic_match"
    assert res["coverage"]["nicht_zugeordnet_ms"] == 2000.0

def test_same_speaker_overlapping_turns_are_merged():
    res = build([], [turn(0, 2000, "A"), turn(1500, 3000, "A")])
    assert len(res["turns"]) == 1
    t = res["turns"][0]
    assert (t["start_ms"], t["end_ms"]) == (0.0, 3000.0)
    assert t["overlap_status"] == OVERLAP_NONE

# --- Abdeckungspartition ----------------------------------------------------

def test_coverage_is_interval_union_without_double_counting():
    # Zwei überlappende verwertbare Turns (A 0-2000, B 1000-3000): Union 3000 ms.
    res = build([], [turn(0, 2000, "A"), turn(1000, 3000, "B")])
    cov = res["coverage"]
    assert cov["speech_ms"] == 3000.0
    assert cov["usable_ms"] == 3000.0
    total = (
        cov["usable_ms"]
        + cov["unsicher_ms"]
        + cov["overlap_unsicher_ms"]
        + cov["nicht_zugeordnet_ms"]
    )
    assert total == cov["speech_ms"]  # Partition

def test_partition_covers_mixed_categories_disjointly():
    # Drei nicht überlappende Turns aus drei Kategorien: Partition ohne Doppelzählung.
    res = build(
        [],
        [
            turn(0, 2000, "A"),                       # verwertbar (single)
            turn(2500, 3500, "B", hint="uncertain"),  # unsicher
            turn(4000, 5000, None),                   # nicht_zugeordnet
        ],
    )
    cov = res["coverage"]
    assert cov["speech_ms"] == 4000.0
    assert cov["usable_ms"] == 2000.0
    assert cov["unsicher_ms"] == 1000.0
    assert cov["nicht_zugeordnet_ms"] == 1000.0
    total = (
        cov["usable_ms"]
        + cov["unsicher_ms"]
        + cov["overlap_unsicher_ms"]
        + cov["nicht_zugeordnet_ms"]
    )
    assert total == cov["speech_ms"]  # Partition

def test_coverage_helper_counts_only_usable():
    res = build([], [turn(0, 2000, "A"), turn(3000, 4000, "B", hint="uncertain")])
    speech, usable = coverage(res)
    assert (speech, usable) == (3000.0, 2000.0)

# --- Heiliger Unveränderlichkeits-Vertrag -----------------------------------

def test_word_overlay_unique_turn_is_zugeordnet():
    words = words_with_times([(100, 400), (1500, 1800)])
    res = build(words, [turn(0, 900, "A"), turn(1000, 1900, "B")])
    w0, w1 = res["words"]
    assert w0["assignment_status"] == WORD_ASSIGNED
    assert w0["speaker_ref"] == "speaker_01"
    assert w1["speaker_ref"] == "speaker_02"
    for w in res["words"]:
        assert len([w["speaker_ref"]]) == 1

def test_word_cutting_overlap_is_ueberlappend_kept_once():
    words = words_with_times([(1500, 1800)])
    res = build(words, [turn(0, 2000, "A"), turn(1000, 3000, "B")])
    w = res["words"][0]
    assert w["assignment_status"] == WORD_OVERLAP
    assert w["speaker_ref"] is None
    assert w["cluster_candidates"] == ["speaker_01", "speaker_02"]
    assert len(res["words"]) == 1  # Wort bleibt genau einmal erhalten

def test_word_cutting_uncertain_boundary_is_mehrdeutig():
    words = words_with_times([(1500, 1800)])
    res = build(words, [turn(0, 2000, "A", hint="uncertain")])
    w = res["words"][0]
    assert w["assignment_status"] == WORD_AMBIGUOUS
    assert w["speaker_ref"] is None

def test_word_without_precise_boundary_keeps_unassigned():
    words = words_with_times([(0, 400), None])
    res = build(words, [turn(0, 900, "A")])
    w = res["words"][1]
    assert w["assignment_status"] == WORD_UNASSIGNED
    assert w["reason_code"] == "no_precise_boundary"
    assert w["speaker_ref"] is None

def test_word_outside_any_turn_is_nicht_zugeordnet():
    words = words_with_times([(5000, 5400)])
    res = build(words, [turn(0, 900, "A")])
    w = res["words"][0]
    assert w["assignment_status"] == WORD_UNASSIGNED
    assert w["reason_code"] == "no_acoustic_match"

def test_words_preserved_fieldwise_and_exactly_once():
    words = words_with_times([(0, 400), (1000, 1400), None])
    res = build(words, [turn(0, 900, "A")])
    assert [w["word_id"] for w in res["words"]] == [w["word_id"] for w in words]
    for base, out in zip(words, res["words"], strict=True):
        for field in ("word_id", "order", "char_start", "char_end", "text",
                      "status", "start_ms", "end_ms"):
            assert out[field] == base[field]

def test_verify_words_unchanged_blocks_every_mutation():
    words = words_with_times([(0, 400), (1000, 1400)])
    ok = build(words, [turn(0, 900, "A")])["words"]
    verify_words_unchanged(words, ok)  # darf nicht werfen

    mutated = [dict(w) for w in ok]
    mutated[0]["text"] = "geaendert"
    with pytest.raises(ValueError, match="mutiert"):
        verify_words_unchanged(words, mutated)

    mutated = [dict(w) for w in ok]
    mutated[1]["end_ms"] = mutated[1]["end_ms"] + 1
    with pytest.raises(ValueError, match="mutiert"):
        verify_words_unchanged(words, mutated)

    with pytest.raises(ValueError, match="Wortmenge veraendert"):
        verify_words_unchanged(words, ok[:-1])  # Wort entfernt

    with pytest.raises(ValueError, match="mutiert"):
        verify_words_unchanged(words, list(reversed(ok)))  # Reihenfolge geaendert

def test_wordset_change_blocks_result_build():
    words = words_with_times([(0, 400)])
    bad = [dict(words[0], text="MUTATION")]
    with pytest.raises(ValueError, match="mutiert"):
        build(bad, [turn(0, 900, "A")])

# --- Sprecheranzahlmodus in Provenienz --------------------------------------

def test_forced_speaker_spec_is_visible_in_payload():
    spec = {"mode": "exact", "count": 3, "minimum": None, "maximum": None}
    res = build([], [turn(0, 900, "A")], spec=spec)
    assert res["speaker_spec"]["mode"] == "exact"
    assert res["speaker_spec"]["count"] == 3

# --- Zustandsableitung als Funktion -----------------------------------------

def test_reason_codes_are_content_free():
    # Inhaltsfreie, versionierte Grundcodes: keine Textinhalte, Namen oder Merkmale.
    for code in REASON_CODES:
        assert code == code.lower()
        assert " " not in code
        assert code.replace("_", "").isalnum()
        assert not any(ch in code for ch in "äöüÄÖÜß")

def test_derive_state_rules():
    assert derive_state([], [], {"speech_ms": 0.0, "usable_ms": 0.0}, 0) == "no_speech"
    assert (
        derive_state([{}], [], {"speech_ms": 1000.0, "usable_ms": 1000.0}, 0)
        == "diarized"
    )
    assert (
        derive_state([{}], [], {"speech_ms": 1000.0, "usable_ms": 960.0}, 0)
        == "partially_diarized"
    )
    assert (
        derive_state([{}], [], {"speech_ms": 1000.0, "usable_ms": 500.0}, 0) == "failed"
    )
    assert (
        derive_state([{}], [], {"speech_ms": 1000.0, "usable_ms": 1000.0}, 1)
        == "failed"
    )
    # 100 % Abdeckung ohne verwertbaren Turn bleibt failed.
    assert derive_state([], [], {"speech_ms": 1000.0, "usable_ms": 1000.0}, 0) == "failed"
