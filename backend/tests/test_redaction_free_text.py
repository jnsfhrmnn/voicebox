# -*- coding: utf-8 -*-
"""USCRX-2026-16006 (RL-04): wortgrenzengenaue Freitext-Redaktion.

Negativtest wortgleich aus dem Review-Befund: „Herr Mustermann kommt, Muster
auch." mit Register Muster -> Person 1 darf weder verstümmeln noch leaken.
"""
from backend.minutes.redaction import RESTE_MARKER, redact_free_text


def _register(*pairs: tuple[str, str]) -> dict:
    return {
        "entries": [
            {
                "entry_id": f"e{i}",
                "original_text": original,
                "pseudonym": pseudonym,
                "kind": "person",
                "state": "bestaetigt",
                "occurrences": [],
            }
            for i, (original, pseudonym) in enumerate(pairs)
        ]
    }


def test_teilwort_verstuemmelt_nicht_und_leakt_nicht():
    out = redact_free_text(
        "Herr Mustermann kommt, Muster auch.",
        _register(("Muster", "Person 1")),
    )
    assert "Person 1mann" not in out, "Teilwort-Fund darf nicht verstümmeln"
    assert "muster" not in out.lower(), "Registerbegriff darf nicht leaken"
    assert "Person 1 auch." in out, "ganzes Wort muss ersetzt werden"
    assert RESTE_MARKER in out, "Reststelle muss sichtbar markiert sein"
    assert "Mustermann" not in out, "Teilwort-Träger wird markiert statt offen gelassen"


def test_schreibvarianten_werden_ersetzt_oder_sichtbar_markiert():
    out = redact_free_text(
        "muster und Musters sind sensible Woerter.",
        _register(("Muster", "Person 1")),
    )
    assert "muster" not in out.lower(), "Schreibvarianten dürfen nicht leaken"
    assert "Person 1" in out
    assert RESTE_MARKER in out


def test_laengste_zuerst():
    out = redact_free_text(
        "Erika Muster kommt.",
        _register(("Muster", "Person 2"), ("Erika Muster", "Person 1")),
    )
    assert out == "Person 1 kommt."


def test_mehrwort_ohne_verstuemmelung():
    out = redact_free_text(
        "Mit Erika Muster wurde gesprochen.",
        _register(("Erika Muster", "Person 1")),
    )
    assert out == "Mit Person 1 wurde gesprochen."


def test_leerer_und_unvollstaendiger_input_stabil():
    assert redact_free_text("", _register(("Muster", "Person 1"))) == ""
    assert redact_free_text("Kein Treffer hier.", {"entries": []}) == "Kein Treffer hier."
