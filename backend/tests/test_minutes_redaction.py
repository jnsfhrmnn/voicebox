"""JFW-13: Redaktion und Re-Identifizierungshinweise — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_redaction.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.pseudonym import (
    apply_action,
    confirm_register,
    entry_by_candidate,
    propose_register,
)
from backend.minutes.redaction import (
    REMOVED_MARKER,
    build_replacement_plan,
    redact_text,
    reident_hints,
)
from backend.tests.jfw13_sources import TEXT, candidates_names

TURN_ORDER = {"t1": 0, "t2": 1, "t3": 2}


def confirmed(**over):
    return confirm_register(propose_register(
        over.pop("candidates", None) or candidates_names(), TURN_ORDER))


def test_plan_replaces_persons_and_orgs():
    r = confirmed()
    plan = build_replacement_plan(r)
    out = redact_text(TEXT, plan)
    assert "Erika Muster" not in out
    assert "Max Beispiel" not in out
    assert "Person 1" in out
    assert "Organisation A" in out
    assert "Person 2" in out


def test_kontakt_removed_with_marker():
    r = confirmed()
    out = redact_text(TEXT, build_replacement_plan(r))
    assert "erika@muster.de" not in out
    assert REMOVED_MARKER in out


def test_text_outside_spans_unchanged():
    r = confirmed()
    out = redact_text(TEXT, build_replacement_plan(r))
    assert out.startswith("Guten Tag, hier spricht ")
    assert out.endswith("Wir entscheiden uns fuer Option A.")


def test_overlapping_detections_are_marked_not_guessed():
    cands = [
        {"candidate_id": "o1", "kind": "person", "text": "Erika Muster",
         "occurrences": [{"turn_id": "t1", "start": 24, "end": 36}]},
        {"candidate_id": "o2", "kind": "organisation", "text": "Muster",
         "occurrences": [{"turn_id": "t1", "start": 30, "end": 36}]},
    ]
    r = confirmed(candidates=cands)
    out = redact_text(TEXT, build_replacement_plan(r))
    # kein Pseudonym wird geraten: ueberlappende Stellen tragen sichtbare Marker
    assert "Erika Muster" not in out
    assert "Person (unklar)" in out or REMOVED_MARKER in out
    assert out.count("Person 1") + out.count("Organisation A") == 0


def test_not_replaceable_stays_and_is_hinted():
    r = confirmed()
    r = apply_action(r, {"kind": "nicht_ersetzbar",
                         "entry_id": entry_by_candidate(r, "c1")["entry_id"]})
    out = redact_text(TEXT, build_replacement_plan(r))
    assert "Erika Muster" in out  # Originalstelle bleibt
    hints = reident_hints(r)
    assert any(h["reason"] == "nicht_ersetzbar" for h in hints)


def test_unclar_entries_produce_hints():
    cands = [{"candidate_id": "u1", "kind": "person", "text": "Unklar", "ambiguous": True,
              "occurrences": [{"turn_id": "t1", "start": 0, "end": 6}]}]
    r = confirmed(candidates=cands)
    hints = reident_hints(r)
    assert any(h["reason"] == "unklar" for h in hints)


def test_clean_register_has_no_hints():
    assert reident_hints(confirmed()) == []
