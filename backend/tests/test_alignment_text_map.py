"""JFW-2: Wort-/Zeichenvertrag (No-text-change-Gate) — Vertragstests.

Rein zeichenbasierte Abbildung einer unveraenderten Transkriptrevision auf den
Wortvertrag. Der sichtbare Text ist und bleibt exakt das Substring der Revision.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_alignment_text_map.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.alignment.text_map import (  # noqa: E402
    build_word_contract,
    verify_no_text_change,
)


def texts_of(words):
    return [w["text"] for w in words]


def test_empty_text_yields_no_tokens():
    assert build_word_contract("") == []


def test_reconstruction_is_exact_no_text_change():
    text = "Hallo Welt! Das sind 42 Wörter, oder?"
    words = build_word_contract(text)
    # Lueckenlose, monoton steigende Zeichenpositionen + exakte Substrings.
    pos = 0
    for w in words:
        assert w["char_start"] == pos
        assert w["text"] == text[w["char_start"]:w["char_end"]]
        pos = w["char_end"]
    assert pos == len(text)
    verify_no_text_change(text, words)  # wirft nicht


def test_punctuation_whitespace_markup_are_not_applicable():
    text = "Hi,  Welt!\n**fett**"
    words = build_word_contract(text)
    na = [w for w in words if w["status"] == "not_applicable"]
    alignable = [w for w in words if w["status"] != "not_applicable"]
    # Satzzeichen-/Leerraum-/Markdown-Rohmaterial ist nicht akustisch ausrichtbar.
    assert "," in "".join(w["text"] for w in na)
    assert "!" in "".join(w["text"] for w in na)
    assert texts_of(alignable) == ["Hi", "Welt", "fett"]


def test_numbers_urls_paths_stay_visible_unchanged():
    text = "Siehe C:\\tmp\\a.txt und https://example.com/x?y=1 — Version 1.2.3!"
    words = build_word_contract(text)
    joined = " ".join(w["text"] for w in words if w["status"] != "not_applicable")
    for token in ("C:\\tmp\\a.txt", "https://example.com/x?y=1", "1.2.3"):
        assert token in joined  # sichtbare Form bleibt exakt erhalten


def test_repeated_words_are_separate_records():
    text = "ja ja und ja"
    words = build_word_contract(text)
    ja = [w for w in words if w["text"] == "ja"]
    assert len(ja) == 3
    starts = [w["char_start"] for w in ja]
    assert len(set(starts)) == 3  # ueber Reihenfolge UND Zeichenposition getrennt
    ids = [w["word_id"] for w in ja]
    assert len(set(ids)) == 3


def test_word_ids_are_stable_and_ordered():
    text = "eins zwei drei"
    words = build_word_contract(text)
    # Alle Tokens (inkl. Leerraum) tragen durchgehende, stabile IDs und Reihenfolge.
    assert [w["order"] for w in words] == list(range(len(words)))
    assert [w["word_id"] for w in words] == [f"w-{i:04d}" for i in range(len(words))]
    alignable = [w for w in words if w["status"] != "not_applicable"]
    assert texts_of(alignable) == ["eins", "zwei", "drei"]
    # Stabilitaet: gleicher Text -> identische Abbildung.
    assert build_word_contract(text) == words


def test_unicode_and_code_switch_kept_verbatim():
    text = "Das war really schön — naïve Café-Übung"
    words = build_word_contract(text)
    alignable = [w for w in words if w["status"] != "not_applicable"]
    assert "really" in texts_of(alignable)
    assert "schön" in texts_of(alignable)
    assert "naïve" in texts_of(alignable)


def test_verify_detects_text_tampering():
    text = "Hallo Welt"
    words = build_word_contract(text)
    words[0]["text"] = "Hallo!"  # Mutation des Anzeigetexts
    with pytest.raises(ValueError):
        verify_no_text_change(text, words)


def test_verify_detects_span_gap():
    text = "Hallo Welt"
    words = build_word_contract(text)
    words[-1]["char_start"] += 1  # Luecke in der Abbildung
    with pytest.raises(ValueError):
        verify_no_text_change(text, words)
