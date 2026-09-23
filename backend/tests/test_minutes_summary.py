"""JFW-13: Zusammenfassung Kurz/Lang ohne neue Fakten — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_summary.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.summary import build_summary
from backend.tests.jfw13_sources import TEXT, names_doc, summary_proposals


def turns_of_doc():
    return names_doc()["turns"]


def build():
    items = summary_proposals()
    return build_summary(items, items, TEXT, turns_of_doc(), {})


def test_short_and_long_sections_exist():
    out = build()
    assert out["kurz"]
    assert out["lang"]


def test_evidenced_statements_are_kept():
    out = build()
    texts = [s["text"] for s in out["lang"]]
    assert "Die Muster GmbH meldet sich." in texts
    assert "Option A wird umgesetzt." in texts


def test_statement_without_evidence_is_dropped():
    out = build()
    texts = [s["text"] for s in out["lang"]]
    assert "Erfundene Aussage ohne Beleg." not in texts
    assert any(w["reason_code"] == "aussage_ohne_beleg_verworfen" for w in out["verworfen"])


def test_nonverbatim_quote_is_dropped_not_reworded():
    out = build()
    texts = [s["text"] for s in out["lang"]]
    assert "Aussage mit nicht belegtem Zitat." not in texts
    assert any(w["reason_code"] == "zitat_nicht_belegt" for w in out["verworfen"])


def test_verbatim_quote_survives_unchanged():
    out = build()
    stmt = next(s for s in out["lang"] if s["text"] == "Option A wird umgesetzt.")
    assert stmt["quotes"] == ["entscheiden uns fuer Option A"]


def test_unclear_status_is_marked():
    out = build()
    stmt = next(s for s in out["lang"] if s["status"] == "unklar")
    assert stmt["status_marker"] == "status_unklar"


def test_every_kept_statement_carries_evidence():
    out = build()
    for section in (out["kurz"], out["lang"]):
        for stmt in section:
            assert stmt["evidence"]
