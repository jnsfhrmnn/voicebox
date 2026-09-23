"""JFW-4: Export-Snapshot-Bindung — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_export_snapshot.py
"""
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.export.provenance import transcript_text_hash
from backend.export.snapshot import build_snapshot, display_speakers
from backend.tests.jfw4_sources import (
    req,
    req_jfw11,
    sources_full,
    sources_jfw11,
    sources_no_cover,
)


def test_build_snapshot_ok():
    snap = build_snapshot(req(), sources_full())
    assert snap.binding_status == "ok"
    assert snap.readiness["state"] == "ready"
    assert len(snap.words) == 6


def test_binding_conflict_on_jfw2_hash():
    snap = build_snapshot(req(jfw2_result_hash="9" * 64), sources_full())
    assert snap.binding_status == "binding_conflict"
    assert snap.readiness["state"] == "blocked"
    assert snap.readiness["reason_code"] == "binding_conflict"


def test_binding_missing_when_jfw11_expected():
    snap = build_snapshot(req_jfw11(), sources_full())
    assert snap.binding_status == "binding_missing"
    assert snap.readiness["state"] == "blocked"


def test_jfw11_conflict_on_commit_hash():
    src = sources_jfw11()
    snap = build_snapshot(req_jfw11(jfw11_commit_hash="f" * 64), src)
    assert snap.binding_status == "binding_conflict"


def test_file_job_without_jfw11_is_not_a_partial_error():
    snap = build_snapshot(req(), sources_full())
    assert snap.jfw11 is None
    assert snap.readiness["quality_dimensions"]["sources"] == "nicht_betroffen"
    assert snap.readiness["state"] == "ready"
    assert snap.readiness["warnings"] == []


def test_jfw11_binds_and_keeps_annotations():
    snap = build_snapshot(req_jfw11(), sources_jfw11())
    assert snap.binding_status == "ok"
    assert snap.jfw11["name_mappings"][0]["state"] == "confirmed"
    assert snap.readiness["quality_dimensions"]["sources"] == "teilweise"
    assert snap.readiness["quality_dimensions"]["dedupe"] == "sauber"


def test_text_hash_mismatch_is_binding_conflict():
    r = req(transcript_text_hash=transcript_text_hash("anderer text"))
    snap = build_snapshot(r, sources_full())
    assert snap.binding_status == "binding_conflict"


def test_invalid_words_block_snapshot():
    src = sources_full()
    src["jfw2"]["words"][0]["start_ms"] = None
    snap = build_snapshot(req(), src)
    assert snap.readiness["state"] == "blocked"
    assert snap.readiness["reason_code"] == "wortdaten_ungueltig"


def test_display_names_policy_neutral():
    snap = build_snapshot(req_jfw11(), sources_jfw11())
    disp = {d["cluster_id"]: d for d in snap.speakers_display}
    assert disp["speaker_01"]["display_name"] is None
    assert disp["speaker_01"]["display_label"] == "Sprecher 1"


def test_display_names_confirmed_policy_shows_only_confirmed():
    snap = build_snapshot(req_jfw11(name_policy="confirmed_names"), sources_jfw11())
    disp = {d["cluster_id"]: d for d in snap.speakers_display}
    assert disp["speaker_01"]["display_name"] == "Erika"
    assert disp["speaker_01"]["display_name_source"] == "confirmed"
    # suggested bleibt nie Anzeigename
    assert disp["speaker_02"]["display_name"] is None
    unauthorized = disp["speaker_02"]["unauthorized_name_annotations"]
    assert unauthorized
    assert unauthorized[0]["name"] == "Max"
    assert unauthorized[0]["state"] == "suggested"


def test_display_speakers_without_jfw11_is_neutral():
    disp = display_speakers([{"cluster_id": "speaker_01", "display_label": "Sprecher 1"}],
                            None, "confirmed_names")
    assert disp[0]["display_name"] is None
    assert disp[0]["mapping_state"] == "neutral"


def test_snapshot_does_not_mutate_sources():
    src = sources_jfw11()
    frozen = copy.deepcopy(src)
    build_snapshot(req_jfw11(), src)
    assert src == frozen


def test_no_cover_still_keeps_words_for_json():
    snap = build_snapshot(req(jfw3_status="partially_diarized"), sources_no_cover())
    assert len(snap.words) == 6  # JSON bleibt verlustfrei
