"""JFW-2: Ergebnisvertrag + Zeitintegrität — Vertragstests.

Zustandsableitung (aligned / partially_aligned / no_alignable_speech / failed),
Grenzvalidierung (0 <= start < end <= Dauer), Monotonie + Nicht-Ueberlappung je
Spur, not_applicable ohne Zeiten, Coverage-Zaehler und kanonischer Hash.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_alignment_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.alignment.contract import (  # noqa: E402
    RESULT_CONTRACT_VERSION,
    build_result,
    coverage,
    derive_state,
)
from backend.alignment.text_map import build_word_contract  # noqa: E402

TEXT = "eins zwei drei vier"
DURATION_MS = 10000.0


def alignable_of(words):
    return [w for w in words if w["status"] != "not_applicable"]


def wof(words, label):
    return next(w for w in alignable_of(words) if w["text"] == label)


def assign_all(words, starts):
    """Deterministische Zuweisung je ausrichtbarem Wort i -> (start, end= start+300, score)."""
    out = {}
    for i, w in enumerate(alignable_of(words)):
        s = starts[i]
        if s is None:
            out[w["word_id"]] = None
        else:
            out[w["word_id"]] = (s, s + 300.0, 0.9)
    return out


def test_full_coverage_is_aligned():
    words = build_word_contract(TEXT)
    res = build_result(
        TEXT, words, assign_all(words, [0, 1000, 2000, 3000]),
        duration_ms=DURATION_MS, provider="fake", frame_ms=20.0,
    )
    assert res["status"] == "aligned"
    assert res["coverage_alignable"] == 4
    assert res["coverage_aligned"] == 4
    assert res["contract_version"] == RESULT_CONTRACT_VERSION


def test_partial_coverage_between_95_and_100():
    text = "a " * 20  # 20 ausrichtbare Woerter
    words = build_word_contract(text)
    starts = [i * 400.0 for i in range(20)]
    starts[0] = None  # 19/20 = 95 %
    res = build_result(
        text, words, assign_all(words, starts),
        duration_ms=DURATION_MS, provider="fake",
    )
    assert res["status"] == "partially_aligned"
    assert res["coverage_aligned"] == 19
    assert res["coverage_alignable"] == 20


def test_below_95_percent_is_failed():
    text = "a " * 20
    words = build_word_contract(text)
    starts = [None] * 20
    starts[0], starts[1] = 0.0, 1000.0  # 2/20 = 10 % -> failed
    res = build_result(
        text, words, assign_all(words, starts),
        duration_ms=DURATION_MS, provider="fake",
    )
    assert res["status"] == "failed"


def test_no_alignable_speech():
    text = "!!! ... ***"
    words = build_word_contract(text)
    res = build_result(
        text, words, {}, duration_ms=DURATION_MS, provider="fake",
    )
    assert res["status"] == "no_alignable_speech"
    assert res["coverage_alignable"] == 0
    assert "coverage_percent" not in res  # keine prozentuale Scheinabdeckung


def test_unaligned_words_have_no_times_and_reason():
    words = build_word_contract(TEXT)
    res = build_result(
        TEXT, words, assign_all(words, [0, 1000, None, 3000]),
        duration_ms=DURATION_MS, provider="fake",
    )
    un = [w for w in res["words"] if w["status"] == "unaligned"]
    assert len(un) == 1
    assert un[0]["start_ms"] is None and un[0]["end_ms"] is None
    assert un[0]["reason_code"]  # versionierter, inhaltsfreier Grundcode


def test_invalid_boundaries_downgrade_to_unaligned():
    words = build_word_contract(TEXT)
    asg = assign_all(words, [0, 1000, 2000, 3000])
    asg[wof(words, "zwei")["word_id"]] = (500.0, 400.0, 0.9)      # end < start
    asg[wof(words, "drei")["word_id"]] = (2000.0, 99999.0, 0.9)   # ende > Dauer
    res = build_result(TEXT, words, asg, duration_ms=DURATION_MS, provider="fake")
    by_label = {w["text"]: w for w in res["words"] if w["status"] != "not_applicable"}
    assert by_label["zwei"]["status"] == "unaligned"
    assert by_label["zwei"]["reason_code"] == "invalid_boundary"
    assert by_label["drei"]["status"] == "unaligned"
    # Vertrag: jeder aligned-Eintrag erfuellt 0 <= start < end <= Dauer.
    for w in res["words"]:
        if w["status"] == "aligned":
            assert 0 <= w["start_ms"] < w["end_ms"] <= DURATION_MS


def test_non_monotonic_or_overlap_downgrades_later_word():
    words = build_word_contract(TEXT)
    asg = assign_all(words, [0, 1000, 2000, 3000])
    asg[wof(words, "drei")["word_id"]] = (500.0, 800.0, 0.9)  # beginnt vor „zwei“
    res = build_result(TEXT, words, asg, duration_ms=DURATION_MS, provider="fake")
    by_label = {w["text"]: w for w in res["words"] if w["status"] != "not_applicable"}
    assert by_label["drei"]["status"] == "unaligned"
    assert by_label["drei"]["reason_code"] == "non_monotonic"
    aligned = [w for w in res["words"] if w["status"] == "aligned"]
    for a, b in zip(aligned, aligned[1:]):
        assert a["start_ms"] < b["start_ms"]
        assert a["end_ms"] <= b["start_ms"]  # nicht ueberlappend auf der Spur


def test_not_applicable_never_gets_times():
    text = "Hallo, Welt!"
    words = build_word_contract(text)
    asg = {w["word_id"]: (0.0, 100.0, 0.5) for w in words}
    res = build_result(text, words, asg, duration_ms=DURATION_MS, provider="fake")
    for w in res["words"]:
        if w["status"] == "not_applicable":
            assert w["start_ms"] is None and w["end_ms"] is None


def test_result_hash_is_stable_and_sensitive():
    words = build_word_contract(TEXT)
    asg = assign_all(words, [0, 1000, 2000, 3000])
    r1 = build_result(TEXT, words, asg, duration_ms=DURATION_MS, provider="fake")
    r2 = build_result(TEXT, words, asg, duration_ms=DURATION_MS, provider="fake")
    assert r1["result_hash"] == r2["result_hash"]
    asg2 = assign_all(words, [0, 1000, 2000, 3500])
    r3 = build_result(TEXT, words, asg2, duration_ms=DURATION_MS, provider="fake")
    assert r3["result_hash"] != r1["result_hash"]


def test_coverage_helper_counts_alignable_only():
    words = build_word_contract("ja, ja!")
    a, n = coverage(words)
    assert (a, n) == (2, 0)
    assert derive_state(words) == "failed"  # 0 von 2 ausgerichtet


def test_aligned_words_carry_quality_provenance():
    words = build_word_contract(TEXT)
    res = build_result(
        TEXT, words, assign_all(words, [0, 1000, 2000, 3000]),
        duration_ms=DURATION_MS, provider="fake", frame_ms=20.0,
    )
    w = wof(res["words"], "eins")
    assert w["quality"]["provider"] == "fake"
    assert w["quality"]["frame_ms"] == 20.0
