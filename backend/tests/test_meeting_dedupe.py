"""JFW-11: Konservative Eigenstimmen-Deduplizierung — Vertragstests (TDD).

Vertrag (Spec „Deduplizierungsvertrag"): `duplikat_bestaetigt` nur bei belegter
Quellenäquivalenz; nie Inhalt entfernen bei Unsicherheit; nie die Remote-Spur
wegen Mikrofonaktivität unterdrücken (Fremdstimmenerhalt).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_dedupe.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.dedupe import (
    CONFIRMED,
    DEDUPE_CONTRACT_VERSION,
    KEPT_SEPARATE,
    UNCERTAIN,
    USER_RESOLVED,
    build_combined_contributions,
    evaluate_pair,
    make_decision,
    remote_suppression_forbidden,
)

REF_A = {"track_id": "track-mic", "turn_id": "turn-1", "word_ids": ["w1", "w2"]}
REF_B = {"track_id": "track-remote", "turn_id": "turn-9", "word_ids": ["w77"]}


def test_confirming_duplicate_only_with_proven_equivalence():
    assert evaluate_pair(corr=0.97, same_speaker_candidate=True, foreign_overlap=False) == CONFIRMED
    assert evaluate_pair(corr=0.97, same_speaker_candidate=False, foreign_overlap=False) == KEPT_SEPARATE
    assert evaluate_pair(corr=0.97, same_speaker_candidate=True, foreign_overlap=True) == KEPT_SEPARATE


def test_uncertain_cases_never_remove_content():
    assert evaluate_pair(corr=0.7, same_speaker_candidate=True, foreign_overlap=False) == UNCERTAIN
    assert evaluate_pair(corr=0.1, same_speaker_candidate=True, foreign_overlap=False) == KEPT_SEPARATE


def test_remote_is_never_suppressed_for_mic_activity():
    assert remote_suppression_forbidden(mic_active=True, foreign_overlap=True) is True
    assert remote_suppression_forbidden(mic_active=True, foreign_overlap=False) is True
    assert remote_suppression_forbidden(mic_active=False, foreign_overlap=False) is True


def test_decision_record_is_complete_and_content_free():
    d = make_decision(
        ref_a=REF_A,
        ref_b=REF_B,
        state=UNCERTAIN,
        corr=0.7,
        rule_id="rule_corr_v1",
        parent_revision="rev-1",
    )
    assert d["contract_version"] == DEDUPE_CONTRACT_VERSION
    assert d["decision_id"]
    assert d["ref_a"] == REF_A
    assert d["ref_b"] == REF_B
    assert d["state"] == UNCERTAIN
    assert 0.0 <= d["confidence"] <= 1.0
    assert d["evidence_class"] in ("quellenaequivalenz_belegt", "quellenaequivalenz_unsicher", "keine_aequivalenz")
    assert d["rule_id"] == "rule_corr_v1"
    assert d["parent_revision"] == "rev-1"
    assert d["user_action"] is None
    verboten = {"text", "transcript", "audio", "name", "speaker_name"}
    assert verboten.isdisjoint(d.keys())


def test_user_resolution_creates_new_decision_state():
    d = make_decision(
        ref_a=REF_A, ref_b=REF_B, state=UNCERTAIN, corr=0.7,
        rule_id="rule_corr_v1", parent_revision="rev-1",
    )
    resolved = make_decision(
        ref_a=REF_A, ref_b=REF_B, state=USER_RESOLVED, corr=0.7,
        rule_id="rule_corr_v1", parent_revision=d["decision_id"],
        user_action="bestaetigt",
    )
    assert resolved["state"] == USER_RESOLVED
    assert resolved["parent_revision"] == d["decision_id"]
    assert resolved["user_action"] == "bestaetigt"


def test_combined_result_shows_confirmed_duplicate_once_and_keeps_both_refs():
    contributions = [
        {"repr_id": "c-mic", "ref": REF_A, "decision_state": CONFIRMED, "decision_id": "d-1"},
        {"repr_id": "c-remote", "ref": REF_B, "decision_state": CONFIRMED, "decision_id": "d-1"},
    ]
    combined = build_combined_contributions(contributions)
    assert len(combined) == 1
    assert combined[0]["source_references"] == [REF_A, REF_B]
    assert combined[0]["canonical_repr_id"] == "c-mic"
    assert combined[0]["dedupe_state"] == CONFIRMED


def test_combined_result_keeps_uncertain_and_separate_contributions():
    contributions = [
        {"repr_id": "c-mic", "ref": REF_A, "decision_state": UNCERTAIN},
        {"repr_id": "c-remote", "ref": REF_B, "decision_state": UNCERTAIN},
        {"repr_id": "c-remote-2", "ref": REF_B, "decision_state": KEPT_SEPARATE},
    ]
    combined = build_combined_contributions(contributions)
    assert len(combined) == 3
    for c in combined:
        assert c["dedupe_state"] in (UNCERTAIN, KEPT_SEPARATE)
