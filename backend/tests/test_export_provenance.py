"""JFW-4: Export-Schluessel- und Provenienz-Vertrag — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_provenance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.provenance import (
    CONTRACT_VERSION,
    expected_file_names,
    export_key,
    payload_hash,
    transcript_text_hash,
)
from backend.tests.jfw4_sources import req


def test_export_key_deterministic():
    assert export_key(req()) == export_key(req())
    assert len(export_key(req())) == 64


def test_export_key_binds_result_hashes():
    assert export_key(req()) != export_key(req(jfw2_result_hash="9" * 64))
    assert export_key(req()) != export_key(req(jfw3_result_hash="9" * 64))
    assert export_key(req()) != export_key(req(transcript_revision_hash="9" * 64))


def test_export_key_binds_content_options():
    assert export_key(req()) != export_key(req(name_policy="confirmed_names"))
    assert export_key(req()) != export_key(req(formats=("json", "srt")))
    assert export_key(req()) != export_key(req(partial_mode="timing_only"))
    assert export_key(req()) != export_key(req(export_profile="x"))


def test_export_key_ignores_target_dir():
    """Dateiziel darf fachliche Inhalte nicht veraendern (Byte-Identitaet)."""
    assert export_key(req(target_dir="C:/a")) == export_key(req(target_dir="D:/b"))


def test_payload_hash_binds_target_dir():
    assert payload_hash(req(target_dir="C:/a")) != payload_hash(req(target_dir="D:/b"))


def test_expected_files_always_include_json_with_srt():
    names = expected_file_names(req(formats=("srt",)))
    assert any(n.endswith(".json") for n in names)
    assert any(n.endswith(".srt") for n in names)


def test_expected_file_names_deterministic_and_sanitized():
    r1 = req(job_id="job/1:frag*?", formats=("json", "vtt"))
    r2 = req(job_id="job/1:frag*?", formats=("json", "vtt"))
    names1 = expected_file_names(r1)
    names2 = expected_file_names(r2)
    assert names1 == names2
    for n in names1:
        assert not any(c in n for c in '<>:"/\\|?*')


def test_transcript_text_hash_stable_and_text_sensitive():
    assert transcript_text_hash("äöüß") == transcript_text_hash("äöüß")
    assert transcript_text_hash("ä") != transcript_text_hash("a")
    assert CONTRACT_VERSION == "jfw4_export_v1"
