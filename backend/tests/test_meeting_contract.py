"""JFW-11: Ergebnis-/Provenienz-Vertrag — Vertragstests (TDD).

Annotations-only-Vertrag: JFW-11 ergaenzt ausschliesslich Quellen-, Deduplizierungs-
und Namenszuordnungen; Text, Wortreihenfolge/-grenzen, neutrale Cluster-IDs und
Overlap-/Unsicherheitsstatus aus JFW-2/JFW-3 bleiben unveraendert.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_contract.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.contract import (
    RESULT_CONTRACT_VERSION,
    derive_result_status,
    result_hash,
    verify_annotations_only,
)
from backend.meeting.provenance import MeetingRequest
from backend.meeting.providers.wasapi_capture_helper import (
    ProviderRuntimeMissingError,
    probe_capture_helper,
    start_capture,
)

BASE = {
    "text": "hallo welt",
    "words": [
        {"word_id": "w1", "text": "hallo", "start_ms": 0.0, "end_ms": 400.0},
        {"word_id": "w2", "text": "welt", "start_ms": 400.0, "end_ms": 800.0},
    ],
    "clusters": [{"cluster_id": "speaker_01", "display_label": "Sprecher 1"}],
    "turns": [{"turn_id": "t1", "overlap_status": "nicht_ueberlappend", "start_ms": 0.0, "end_ms": 800.0}],
}


def _combined():
    c = dict(BASE)
    c["dedupe"] = [{"decision_id": "d-1", "state": "getrennt_behalten"}]
    c["name_mappings"] = [{"mapping_id": "m-1", "state": "neutral"}]
    return c


def test_contract_version_pinned():
    assert RESULT_CONTRACT_VERSION == "meeting_result_v1"


def test_annotations_only_accepts_pure_additions():
    assert verify_annotations_only(BASE, _combined()) == []


def test_text_change_is_blocked():
    c = _combined()
    c["text"] = "hallo welt!!"
    assert "text_geaendert" in verify_annotations_only(BASE, c)


def test_word_boundary_change_is_blocked():
    c = _combined()
    c["words"] = [dict(w) for w in BASE["words"]]
    c["words"][0]["end_ms"] = 500.0
    assert "wortgrenzen_geaendert" in verify_annotations_only(BASE, c)


def test_cluster_id_change_is_blocked():
    c = _combined()
    c["clusters"] = [{"cluster_id": "speaker_99", "display_label": "Sprecher 1"}]
    assert "cluster_geaendert" in verify_annotations_only(BASE, c)


def test_overlap_status_change_is_blocked():
    c = _combined()
    c["turns"] = [dict(t) for t in BASE["turns"]]
    c["turns"][0]["overlap_status"] = "ueberlappend"
    assert "overlap_geaendert" in verify_annotations_only(BASE, c)


def test_result_status_derivation():
    full = derive_result_status("secured_dual", {"m": "vollstaendig", "r": "vollstaendig"}, "sync_ok")
    assert full["status"] == "dual_source"
    assert full["flags"] == []

    partial = derive_result_status("secured_partial", {"m": "vollstaendig", "r": "teilweise"}, "sync_unsicher")
    assert partial["status"] == "dual_source_partial"
    assert "sync_unsicher" in partial["flags"]

    single = derive_result_status("secured_partial", {"m": "vollstaendig", "r": "fehlend"}, "sync_ok")
    assert single["status"] == "single_source"
    assert "fehlende_quelle" in single["flags"]


def test_result_hash_is_canonical():
    a = {"x": 1, "y": [1, 2]}
    assert result_hash(a) == result_hash({"y": [1, 2], "x": 1})
    assert result_hash(a) != result_hash({"x": 1, "y": [1, 3]})


def test_meeting_request_identity_and_payload_hashes():
    req = MeetingRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=("a" * 64, "b" * 64),
        manifest_hash="c" * 64,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )
    same = MeetingRequest(
        job_id="job-1",
        meeting_run_id="mr-1",
        jfw6_run_reference="jfw6-1",
        track_hashes=("b" * 64, "a" * 64),  # Reihenfolge egal
        manifest_hash="c" * 64,
        jfw2_result_hash="1" * 64,
        jfw3_result_hash="2" * 64,
    )
    assert req.identity_hash() == same.identity_hash()
    assert req.payload_hash() == same.payload_hash()
    other_manifest = MeetingRequest(
        job_id="job-1", meeting_run_id="mr-1", jfw6_run_reference="jfw6-1",
        track_hashes=("a" * 64, "b" * 64), manifest_hash="d" * 64,
        jfw2_result_hash="1" * 64, jfw3_result_hash="2" * 64,
    )
    # Neue Manifest-Revision (z. B. nach Rohspur-Entfernung) = neue Identitaet,
    # damit die alte Revision revisionsgebunden erhalten und invalidierbar bleibt.
    assert other_manifest.identity_hash() != req.identity_hash()
    assert other_manifest.payload_hash() != req.payload_hash()

    track_deviation = MeetingRequest(
        job_id="job-1", meeting_run_id="mr-1", jfw6_run_reference="jfw6-1",
        track_hashes=("a" * 64, "e" * 64), manifest_hash="c" * 64,
        jfw2_result_hash="1" * 64, jfw3_result_hash="2" * 64,
    )
    # Gleiche Identitaet, abweichende Track-Hashes: fail-closed als Konflikt.
    assert track_deviation.identity_hash() == req.identity_hash()
    assert track_deviation.payload_hash() != req.payload_hash()


def test_capture_helper_seam_is_fail_closed_until_bundled():
    probe = probe_capture_helper()
    assert probe["available"] is False
    assert probe["reason_code"] == "provider_runtime_missing"
    with pytest.raises(ProviderRuntimeMissingError) as excinfo:
        start_capture(role="mic")
    assert excinfo.value.reason_code == "provider_runtime_missing"
