"""JFW-13: fail-closed Eingangsbindung an den JFW-4-Snapshot — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_input.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.input import bind_input, verify_snapshot_unchanged
from backend.minutes.provenance import MinutesRequest
from backend.tests.jfw4_sources import req, sources_partial
from backend.tests.jfw13_sources import jfw4_doc, names_doc, req13_kwargs


def req13(doc, **over) -> MinutesRequest:
    return MinutesRequest(**req13_kwargs(doc, **over))


def test_binds_valid_document():
    doc = names_doc()
    inp = bind_input(req13(doc), doc)
    assert inp.readiness["state"] == "ready"
    assert inp.binding_status == "gebunden"
    assert inp.reason_code is None
    assert inp.text.startswith("Guten Tag")


def test_missing_snapshot_is_blocked():
    doc = names_doc()
    inp = bind_input(req13(doc), None)
    assert inp.readiness["state"] == "blocked"
    assert inp.reason_code == "jfw4_snapshot_fehlt"


def test_wrong_contract_version_is_blocked():
    doc = dict(names_doc())
    doc["contract_version"] = "jfw3_export_v0"
    inp = bind_input(req13(doc), doc)
    assert inp.readiness["state"] == "blocked"
    assert inp.reason_code == "eingang_nicht_jfw4_export"


def test_tampered_document_hash_is_blocked():
    doc = dict(names_doc())
    doc["transcript"] = dict(doc["transcript"], text="manipuliert")
    inp = bind_input(req13(names_doc()), doc)
    assert inp.readiness["state"] == "blocked"
    assert inp.reason_code == "eingang_hash_inkonsistent"


def test_binding_conflict_is_blocked():
    doc = names_doc()
    inp = bind_input(req13(doc, jfw4_result_hash="9" * 64), doc)
    assert inp.readiness["state"] == "blocked"
    assert inp.reason_code == "bindung_inkonsistent"


def test_text_hash_mismatch_is_blocked():
    doc = names_doc()
    inp = bind_input(req13(doc, transcript_text_hash="9" * 64), doc)
    assert inp.readiness["state"] == "blocked"
    assert inp.reason_code == "bindung_inkonsistent"


def test_partial_sources_carry_warnings():
    doc = jfw4_doc(sources_partial(),
                   req(jfw2_status="partially_aligned", jfw3_status="partially_diarized"))
    inp = bind_input(req13(doc), doc)
    assert inp.readiness["state"] == "ready_with_warnings"
    kinds = {w["kind"] for w in inp.warnings}
    assert "partially_aligned" in kinds
    assert "partially_diarized" in kinds


def test_verify_snapshot_unchanged_detects_tampering():
    doc = names_doc()
    assert verify_snapshot_unchanged(doc) == []
    bad = dict(doc)
    bad["turns"] = [*doc["turns"], {"turn_id": "tx"}]
    assert verify_snapshot_unchanged(bad) != []
