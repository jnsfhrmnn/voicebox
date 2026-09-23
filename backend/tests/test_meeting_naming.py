"""JFW-11: Reversible Name-Mapping-Revisionen — Vertragstests (TDD).

Vertrag (Spec „Speaker Assignment Contract"): Name-Mappings sind Annotationen auf
einer gebundenen JFW-3-Clusterrevision; Vorschlaege bleiben Vorschlaege; jede
Aktion erzeugt eine neue Revision; Clusterrevisions-Wechsel invalidiert ohne
Auto-Transfer.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_naming.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.naming import (
    CALIBRATION_UNCALIBRATED,
    STATE_CONFIRMED,
    STATE_CONFLICT,
    STATE_INVALIDATED,
    STATE_MANUAL,
    STATE_NEUTRAL,
    STATE_REJECTED,
    STATE_SUGGESTED,
    authorized_display_name,
    confirm,
    invalidate_for_cluster_revision,
    reject,
    reset_to_neutral,
    set_manual,
    suggest,
)

EVIDENCE = [
    {"track_id": "track-remote", "turn_id": "turn-3", "word_ids": ["w5", "w6"],
     "span_100ns": [1000, 2000]},
]


def test_suggestion_keeps_name_candidate_unchanged_and_stays_a_proposal():
    s = suggest(
        cluster_id="speaker_01",
        name_candidate="Maria",
        evidence=EVIDENCE,
        confidence=0.9,
        rule_id="rule_intro_v1",
        cluster_revision="dia-rev-1",
    )
    assert s["status"] == "vorgeschlagen"
    assert s["state"] == STATE_SUGGESTED
    assert s["name_candidate"] == "Maria"
    assert s["cluster_id"] == "speaker_01"
    assert s["evidence"] == EVIDENCE
    assert s["rule_id"] == "rule_intro_v1"
    assert s["calibration"] == CALIBRATION_UNCALIBRATED
    assert authorized_display_name([s]) is None  # neutral bis Bestaetigung


def test_two_clusters_claiming_same_name_is_conflict():
    a = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1")
    b = suggest(cluster_id="speaker_02", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1",
                existing=[a])
    assert b["state"] == STATE_CONFLICT
    assert authorized_display_name([a, b]) is None


def test_low_confidence_is_visible_conflict_not_auto_adoption():
    s = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.2, rule_id="r", cluster_revision="dia-rev-1",
                min_confidence=0.8)
    assert s["state"] == STATE_CONFLICT
    assert authorized_display_name([s]) is None


def test_confirm_and_manual_create_new_revisions_with_provenance():
    s = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1")
    c = confirm(s, user="nutzer")
    assert c["state"] == STATE_CONFIRMED
    assert c["parent_revision"] == s["mapping_id"]
    assert authorized_display_name([s, c]) == "Maria"

    m = set_manual(cluster_id="speaker_02", display_name="Hans", user="nutzer",
                   cluster_revision="dia-rev-1")
    assert m["state"] == STATE_MANUAL
    assert m["origin"] == "manuell"
    assert authorized_display_name([m]) == "Hans"


def test_reject_and_reset_stay_reversible_with_history():
    s = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1")
    r = reject(s, user="nutzer")
    assert r["state"] == STATE_REJECTED
    assert r["parent_revision"] == s["mapping_id"]
    assert authorized_display_name([s, r]) is None

    m = set_manual(cluster_id="speaker_01", display_name="Maria M.", user="nutzer",
                   cluster_revision="dia-rev-1")
    back = reset_to_neutral(m, user="nutzer")
    assert back["state"] == STATE_NEUTRAL
    assert back["parent_revision"] == m["mapping_id"]
    assert authorized_display_name([m, back]) is None


def test_cluster_revision_change_invalidates_without_transfer():
    s = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1")
    c = confirm(s, user="nutzer")
    inv = invalidate_for_cluster_revision(c, new_cluster_revision="dia-rev-2")
    assert inv["state"] == STATE_INVALIDATED
    assert inv["cluster_revision"] == "dia-rev-2"
    assert inv["parent_revision"] == c["mapping_id"]
    assert inv["transferred_to"] is None
    assert authorized_display_name([s, c, inv]) is None


def test_no_cross_job_identity_fields():
    s = suggest(cluster_id="speaker_01", name_candidate="Maria", evidence=EVIDENCE,
                confidence=0.9, rule_id="r", cluster_revision="dia-rev-1")
    verboten = {"voiceprint", "biometric_id", "person_id", "global_name", "audio", "text"}
    assert verboten.isdisjoint(s.keys())
