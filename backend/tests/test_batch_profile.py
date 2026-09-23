"""JFW-5: eingefrorene gemeinsame Profilrevision — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_profile.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.profile import (
    RESOURCE_POLICY_ID,
    change_reasons,
    phases_for,
    profile_hash,
    resource_policy,
    validate_profile,
)


def prof(**over):
    base = {
        "language_setting": "auto",
        "stt_model": "turbo",
        "model_revision": "a" * 40,
        "decode_profile": "jfw7-transcribe-greedy-v1",
        "alignment_enabled": True,
        "alignment_profile": "precise_words_v1",
        "diarization_enabled": True,
        "speaker_mode": "auto",
        "speaker_count": None,
        "speaker_min": None,
        "speaker_max": None,
        "diarization_profile": "meeting_speakers_v1",
        "export_enabled": True,
        "export_formats": ["json"],
        "export_profile": "lesbare_untertitel_v1",
        "name_policy": "neutral",
        "minutes_enabled": False,
        "minutes_profile": "jfw13_minutes_v1",
        "partial_failure_policy": "mit_belegten_daten_fortfahren",
        "fail_fast": False,
        "output_policy": {
            "target_root": "C:/out",
            "structure": "relative_source",
            "conflict_rule": "blockieren",
        },
    }
    base.update(over)
    return base


def test_valid_profile_has_no_errors():
    assert validate_profile(prof()) == []


def test_incomplete_profile_fails_closed():
    p = prof()
    del p["language_setting"]
    assert validate_profile(p)  # Fehlerliste nicht leer
    bad = prof(language_setting="martian")
    assert validate_profile(bad)


def test_partial_failure_policy_is_exactly_two_values():
    assert validate_profile(prof(partial_failure_policy="was_anderes"))
    assert validate_profile(prof(partial_failure_policy="element_blockieren")) == []


def test_minutes_requires_export_export_requires_alignment_and_diarization():
    assert validate_profile(prof(export_enabled=True, alignment_enabled=False))
    assert validate_profile(prof(export_enabled=True, diarization_enabled=False))
    assert validate_profile(prof(minutes_enabled=True, export_enabled=False))
    assert validate_profile(prof(minutes_enabled=True)) == []


def test_phases_follow_confirmed_order():
    assert phases_for(prof()) == ("transcribe", "align", "diarize", "export")
    assert phases_for(prof(minutes_enabled=True))[-1] == "minutes"
    assert phases_for(prof(export_enabled=False, alignment_enabled=False,
                           diarization_enabled=False)) == ("transcribe",)


def test_profile_hash_is_stable_and_change_sensitive():
    a, b = prof(), prof()
    assert profile_hash(a) == profile_hash(b)
    assert profile_hash(a) != profile_hash(prof(stt_model="small"))
    assert profile_hash(a) != profile_hash(prof(partial_failure_policy="element_blockieren"))


def test_change_reasons_names_the_changed_fields():
    assert change_reasons(prof(), prof()) == []
    reasons = change_reasons(prof(), prof(stt_model="small", fail_fast=True))
    assert "stt_model" in reasons
    assert "fail_fast" in reasons


def test_resource_policy_is_serial_v1():
    rp = resource_policy()
    assert rp["policy_id"] == RESOURCE_POLICY_ID
    assert rp["max_concurrent_ai_phases"] == 1
    assert validate_profile(prof()) == []  # eingebettete Ressourcenpolitik gueltig
