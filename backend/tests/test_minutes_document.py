"""JFW-13: Protokolldokument `jfw13_minutes_v1` und Byte-Regel — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_document.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.document import (
    DOC_KEYS,
    build_document,
    canonical_bytes,
    nondeterminism_revision,
    verify_document,
)
from backend.minutes.provenance import MinutesRequest
from backend.minutes.pseudonym import confirm_register, label_map, propose_register
from backend.minutes.summary import build_summary
from backend.minutes.tasks import build_task_list
from backend.minutes.transcript import build_transcript
from backend.tests.jfw13_sources import (
    TURN_ORDER,
    candidates_names,
    names_doc,
    req13_kwargs,
    summary_proposals,
    tasks_proposals,
)

MODEL_PROVENANCE = {
    "model_id": "Qwen/Qwen2.5-7B-Instruct",
    "model_revision": "a09a35458c702b33eeacc393d103063234e8bc28",
    "model_sha256": "d" * 64,
    "model_license": "apache-2.0",
}


def build_all():
    doc = names_doc()
    request = MinutesRequest(**req13_kwargs(doc))
    register = confirm_register(propose_register(candidates_names(), TURN_ORDER))
    labels = label_map(register)
    tasks = build_task_list(tasks_proposals(), doc["turns"], labels)
    items = summary_proposals()
    summary = build_summary(items, items, doc["transcript"]["text"], doc["turns"], labels)
    tr = build_transcript(doc, register, None)
    built = build_document(
        request, doc, register,
        {"tasks": tasks, "summary": summary, "transcript": tr, "datum": None},
        MODEL_PROVENANCE, attempt_id="att-1",
    )
    return request, register, built


def test_document_structure_keys():
    _r, _reg, built = build_all()
    assert set(built["document"].keys()) == set(DOC_KEYS)


def test_header_contains_date_duration_roles():
    _r, _reg, built = build_all()
    header = built["document"]["header"]
    assert "datum" in header
    assert header["dauer_ms"] == 10_000
    assert set(header["teilnehmer_pseudonyme"]) == {"Person 1", "Person 2", "Organisation A"}


def test_three_sections_present():
    _r, _reg, built = build_all()
    doc = built["document"]
    assert doc["aufgaben"]
    assert doc["zusammenfassung"]["kurz"] is not None
    assert doc["zusammenfassung"]["lang"] is not None
    assert len(doc["transkript"]) == 3


def test_register_section_has_no_mapping():
    _r, reg, built = build_all()
    section = built["document"]["register"]
    assert set(section.keys()) == {"register_id", "revision_id", "revision", "status"}
    assert "entries" not in section
    assert section["revision"] == reg["revision"]


def test_result_hash_recomputable():
    _r, _reg, built = build_all()
    doc = built["document"]
    without = {k: v for k, v in doc.items() if k != "result_hash"}
    from backend.minutes.provenance import canonical_hash
    assert canonical_hash(without) == doc["result_hash"]


def test_provenance_binds_attempt_model_register():
    _r, reg, built = build_all()
    prov = built["document"]["provenance"]
    assert prov["attempt_id"] == "att-1"
    assert prov["model"] == MODEL_PROVENANCE
    assert prov["register_revision"] == reg["revision"]
    assert prov["contract_version"] == "jfw13_minutes_v1"


def test_byte_rule_same_input_same_register_is_byteidentical():
    _r1, _reg1, b1 = build_all()
    _r2, _reg2, b2 = build_all()
    assert canonical_bytes(b1["document"]) == canonical_bytes(b2["document"])


def test_canonical_bytes_end_with_single_newline():
    _r, _reg, built = build_all()
    data = canonical_bytes(built["document"])
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")


def test_nondeterminism_creates_separate_revision_not_second_version():
    _r, _reg, built = build_all()
    rerun_doc = dict(built["document"])
    rerun_doc["zusammenfassung"] = dict(rerun_doc["zusammenfassung"], kurz=[])
    from backend.minutes.provenance import canonical_hash
    without = {k: v for k, v in rerun_doc.items() if k != "result_hash"}
    rerun_doc["result_hash"] = canonical_hash(without)

    rev = nondeterminism_revision(built["document"], rerun_doc)
    assert rev is not None
    assert rev["reason_code"] == "modell_nicht_reproduzierbar"
    assert rev["result_hash"] != built["document"]["result_hash"]
    assert rev["authoritative_result_hash"] == built["document"]["result_hash"]
    # autoritative Fassung bleibt die einzige
    assert rev["authoritative_kept"] is True


def test_identical_rerun_creates_no_revision():
    _r, _reg, built = build_all()
    assert nondeterminism_revision(built["document"], built["document"]) is None


def test_verify_document_accepts_built_document():
    _r, _reg, built = build_all()
    assert verify_document(built["document"], names_doc()) == []
