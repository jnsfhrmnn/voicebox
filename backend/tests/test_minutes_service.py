"""JFW-13: Persistenz `minutes_results`/`pseudonym_registers` — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_service.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database.models import Base
from backend.minutes.document import build_document
from backend.minutes.provenance import MinutesRequest, minutes_key
from backend.minutes.pseudonym import (
    confirm_register,
    delete_register,
    label_map,
    propose_register,
)
from backend.minutes.summary import build_summary
from backend.minutes.tasks import build_task_list
from backend.minutes.transcript import build_transcript
from backend.services import minutes_contract as store
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


@pytest.fixture
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def request_and_doc(**over):
    doc = names_doc()
    return MinutesRequest(**req13_kwargs(doc, **over)), doc


def built_document(doc, request, register):
    labels = label_map(register)
    tasks = build_task_list(tasks_proposals(), doc["turns"], labels)
    items = summary_proposals()
    summary = build_summary(items, items, doc["transcript"]["text"], doc["turns"], labels)
    tr = build_transcript(doc, register, None)
    return build_document(
        request, doc, register,
        {"tasks": tasks, "summary": summary, "transcript": tr, "datum": None},
        MODEL_PROVENANCE, attempt_id="att-1",
    )


READY = {"state": "ready", "warnings": [], "reason_code": None}


def submit(s, request):
    return store.submit_minutes(s, request, READY, [], register_id="reg")


def test_submit_creates_order(session):
    request, _doc = request_and_doc()
    out = submit(session, request)
    assert out["outcome"] == "created"
    assert out["minutes_key"] == minutes_key(request)


def test_submit_idempotent_existing(session):
    request, _doc = request_and_doc()
    submit(session, request)
    assert submit(session, request)["outcome"] == "existing"


def test_submit_conflict_on_different_payload(session):
    request, _doc = request_and_doc()
    submit(session, request)
    other, _ = request_and_doc(target_dir="D:/other")
    assert submit(session, other)["outcome"] == "conflict"


def test_begin_generation_exactly_once(session):
    request, _doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    assert store.begin_generation(session, key, "epoch-1") is not None
    assert store.begin_generation(session, key, "epoch-1") is None


def test_commit_is_terminal(session):
    request, doc = request_and_doc()
    submit(session, request)
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    outcome = store.commit_result(
        session, key, built["document"]["result_hash"],
        built["document"], built["warnings"], MODEL_PROVENANCE,
    )
    assert outcome == "committed"
    row = store.get_minutes(session, key)
    assert row["status"] == "generated"
    assert row["result_hash"] == built["document"]["result_hash"]


def test_cancel_beats_commit_race(session):
    request, doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    assert store.cancel_generation(session, key) == "canceled"
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    outcome = store.commit_result(
        session, key, built["document"]["result_hash"],
        built["document"], built["warnings"], MODEL_PROVENANCE,
    )
    assert outcome == "canceled"
    assert store.get_minutes(session, key)["result_hash"] is None


def test_cancel_after_commit_is_too_late(session):
    request, doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    store.commit_result(session, key, built["document"]["result_hash"],
                        built["document"], built["warnings"], MODEL_PROVENANCE)
    assert store.cancel_generation(session, key) == "too_late"


def test_fail_allows_identical_retry(session):
    request, _doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    assert store.fail_attempt(session, key, "modellfehler") is True
    assert store.begin_generation(session, key, "epoch-1") is not None


def test_recover_interrupted_marks_failed_never_authoritative(session):
    request, _doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-alt")
    assert store.recover_interrupted(session, "epoch-neu") == 1
    row = store.get_minutes(session, key)
    assert row["status"] == "failed"
    assert row["result_hash"] is None


def test_invalidate_on_revision_change(session):
    request, doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    store.commit_result(session, key, built["document"]["result_hash"],
                        built["document"], built["warnings"], MODEL_PROVENANCE)
    n = store.invalidate_other_revisions(
        session, request.job_id, request.transcript_revision_id,
        request.jfw2_result_hash, request.jfw3_result_hash,
        request.jfw4_result_hash, "andere-jfw11-hash",
    )
    assert n == 1
    assert store.get_minutes(session, key)["status"] == "invalidated"


def test_nondeterminism_revision_appends_without_second_version(session):
    request, doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    store.commit_result(session, key, built["document"]["result_hash"],
                        built["document"], built["warnings"], MODEL_PROVENANCE)
    store.add_nondeterminism_revision(session, key, {
        "revision_no": 1,
        "result_hash": "f" * 64,
        "authoritative_result_hash": built["document"]["result_hash"],
        "reason_code": "modell_nicht_reproduzierbar",
    })
    row = store.get_minutes(session, key)
    assert row["result_hash"] == built["document"]["result_hash"]  # autoritativ bleibt
    assert len(row["nondeterminism_revisions"]) == 1


def test_register_lifecycle_and_export_gate(session):
    reg = propose_register(candidates_names(), TURN_ORDER, register_id="reg")
    store.save_register(session, reg, minutes_key_value="key-1")
    confirmed = confirm_register(reg)
    store.save_register(session, confirmed, minutes_key_value="key-1")

    assert store.export_gate(session, "reg", confirmed["revision_id"]) == "ok"

    deleted = delete_register(confirmed)
    store.save_register(session, deleted, minutes_key_value="key-1")

    # offener Exportdialog an die beim Oeffnen gebundene (geloeschte) Registerrevision
    assert store.export_gate(session, "reg", confirmed["revision_id"]) == "register_geloescht"
    head = store.latest_register(session, "reg")
    assert head["status"] == "geloescht"
    for entry in head["entries"]:
        assert entry.get("original_text") is None


def test_delete_for_job_removes_minutes_and_register(session):
    """AC 31: Job-Loeschung loescht Protokollrevision, Register und Zuordnung mit."""
    request, doc = request_and_doc()
    submit(session, request)
    key = minutes_key(request)
    store.begin_generation(session, key, "epoch-1")
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    built = built_document(doc, request, register)
    store.commit_result(session, key, built["document"]["result_hash"],
                        built["document"], built["warnings"], MODEL_PROVENANCE)
    store.save_register(session, register, minutes_key_value=key)

    n = store.delete_minutes_for_job(session, request.job_id)
    assert n["minutes_deleted"] == 1
    assert n["registers_deleted"] >= 1
    assert store.get_minutes(session, key) is None
    assert store.latest_register(session, "reg") is None
