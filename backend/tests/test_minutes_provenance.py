"""JFW-13: Protokoll-Identitaet und Byte-Regel-Basis — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_provenance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from dataclasses import replace

from backend.minutes.provenance import (
    CONTRACT_VERSION,
    MinutesRequest,
    canonical_bytes,
    canonical_hash,
    canonical_json,
    minutes_key,
    payload_hash,
)
from backend.tests.jfw13_sources import names_doc, req13_kwargs


def req13(**over) -> MinutesRequest:
    return MinutesRequest(**req13_kwargs(names_doc(), **over))


def test_contract_version():
    assert CONTRACT_VERSION == "jfw13_minutes_v1"


def test_minutes_key_deterministic():
    assert minutes_key(req13()) == minutes_key(req13())


def test_minutes_key_binds_register_revision():
    assert minutes_key(req13()) != minutes_key(req13(register_revision="reg-2"))


def test_minutes_key_binds_input_revisions():
    assert minutes_key(req13()) != minutes_key(req13(jfw4_result_hash="9" * 64))
    assert minutes_key(req13()) != minutes_key(req13(jfw2_result_hash="9" * 64))
    assert minutes_key(req13()) != minutes_key(req13(transcript_revision_id="rev-2"))
    assert minutes_key(req13()) != minutes_key(req13(job_id="job-2"))


def test_minutes_key_ignores_target():
    assert minutes_key(req13()) == minutes_key(req13(target_dir="C:/ziel"))


def test_payload_hash_binds_target():
    assert payload_hash(req13()) != payload_hash(req13(target_dir="C:/ziel"))


def test_canonical_bytes_rules():
    a = {"b": 1, "a": {"y": 2, "x": 3}}
    b = {"a": {"x": 3, "y": 2}, "b": 1}
    assert canonical_json(a) == canonical_json(b)
    data = canonical_bytes(a)
    assert data.endswith(b"\n")
    assert not data.endswith(b"\n\n")
    assert data.decode("utf-8") == canonical_json(a) + "\n"
    assert canonical_hash(a) == canonical_hash(b)


def test_request_is_frozen():
    r = req13()
    try:
        replace(r, job_id="x")  # dataclass frozen ersetzt, direktes Setzen muss scheitern
        r.job_id = "y"
    except Exception:
        return
    raise AssertionError("MinutesRequest ist nicht unveraenderlich")
