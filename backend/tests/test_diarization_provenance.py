"""JFW-3: Provenienz-/Identitätsbindung + Sprecheranzahlmodus — Vertragstests.

Identity-/Payload-Hash-Bindung (inkl. JFW-2-Referenz und Sprecheranzahlmodus),
fail-closed Sprecheranzahl-Validierung (exakt 1-8, Bereich 1-8, min <= max) und
sichtbare Vorgabe in Provenienz.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_diarization_provenance.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.diarization.provenance import (
    DiarizationRequest,
    SpeakerSpec,
    SpeakerSpecError,
    transcript_text_hash,
)

TEXT = "hallo welt"

def req(**kw):
    defaults = dict(
        job_id="job-1",
        audio_asset_id="audio-1",
        audio_hash="sha256:audio",
        audio_duration_ms=60000,
        timebase="audio_ms_v1",
        transcript_run_id="run-1",
        transcript_revision_id="rev-1",
        transcript_revision_hash=transcript_text_hash(TEXT),
        jfw2_reference_status="aligned",
        jfw2_result_hash="abc123",
        speaker_spec=SpeakerSpec.parse("auto"),
    )
    defaults.update(kw)
    return DiarizationRequest(**defaults)

# --- SpeakerSpec (fail-closed) ---------------------------------------------

def test_auto_is_default_and_needs_no_count():
    spec = SpeakerSpec.parse("auto")
    assert spec.mode == "auto"
    assert spec.as_dict() == {
        "mode": "auto", "count": None, "minimum": None, "maximum": None
    }

def test_exact_1_to_8_accepted():
    for n in (1, 8):
        spec = SpeakerSpec.parse("exact", count=n)
        assert spec.count == n

@pytest.mark.parametrize("n", [0, 9, -1, 100])
def test_exact_outside_1_to_8_rejected(n):
    with pytest.raises(SpeakerSpecError) as exc:
        SpeakerSpec.parse("exact", count=n)
    assert exc.value.reason_code == "speaker_spec_invalid"

def test_range_accepted_when_sane():
    spec = SpeakerSpec.parse("range", minimum=2, maximum=4)
    assert (spec.minimum, spec.maximum) == (2, 4)

def test_range_min_greater_max_rejected():
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("range", minimum=4, maximum=2)

def test_range_outside_1_to_8_rejected():
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("range", minimum=0, maximum=3)
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("range", minimum=2, maximum=9)

def test_unknown_mode_rejected():
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("irgendwas")

def test_auto_with_values_is_fail_closed():
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("auto", count=2)
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("exact", count=2, minimum=1)

def test_exact_with_min_is_rejected():
    with pytest.raises(SpeakerSpecError):
        SpeakerSpec.parse("exact", count=2, minimum=1, maximum=3)

# --- Identity / Payload -----------------------------------------------------

def test_identity_stable_and_includes_speaker_spec():
    a, b = req(), req()
    assert a.identity_hash() == b.identity_hash()
    forced = req(speaker_spec=SpeakerSpec.parse("exact", count=3))
    assert forced.identity_hash() != a.identity_hash()  # neue Ergebnisrevision

def test_identity_binds_jfw2_reference():
    failed_ref = req(jfw2_reference_status="failed", jfw2_result_hash=None)
    assert failed_ref.identity_hash() != req().identity_hash()
    other_hash = req(jfw2_result_hash="XYZ")
    assert other_hash.identity_hash() != req().identity_hash()

def test_identity_binds_transcript_revision():
    assert req(transcript_revision_id="rev-2").identity_hash() != req().identity_hash()

def test_payload_binds_hashes_and_duration():
    assert req().payload_hash() == req().payload_hash()
    assert req(audio_hash="sha256:x").payload_hash() != req().payload_hash()
    # Gleiche Identität, abweichender Payload -> fail-closed Provenienzkonflikt:
    assert req(audio_hash="sha256:x").identity_hash() == req().identity_hash()
    assert (
        req(transcript_revision_hash="f" * 64).payload_hash() != req().payload_hash()
    )
    assert req(audio_duration_ms=60001).payload_hash() != req().payload_hash()

def test_identity_payload_dicts_carry_all_required_fields():
    d = req().identity_dict()
    for key in (
        "job_id", "audio_asset_id", "transcript_revision_id",
        "jfw2_reference_status", "jfw2_result_hash", "diarization_profile",
        "speaker_spec", "contract_version",
    ):
        assert key in d
    p = req().payload_dict()
    for key in (
        "audio_hash", "audio_duration_ms", "timebase", "transcript_run_id",
        "transcript_revision_hash",
    ):
        assert key in p

def test_speaker_spec_translates_to_provider_arguments():
    # Rein und ohne pyannote: die Vorgabe wird exakt auf Pipeline-Argumente abgebildet.
    from backend.diarization.providers.pyannote_pipeline import speaker_params

    assert speaker_params(SpeakerSpec.parse("auto")) == {}
    assert speaker_params(SpeakerSpec.parse("exact", count=3)) == {"num_speakers": 3}
    assert speaker_params(SpeakerSpec.parse("range", minimum=2, maximum=4)) == {
        "min_speakers": 2,
        "max_speakers": 4,
    }

def test_transcript_text_hash_matches_sha256():
    assert transcript_text_hash(TEXT) == transcript_text_hash(TEXT)
    assert len(transcript_text_hash(TEXT)) == 64
    assert transcript_text_hash(TEXT) != transcript_text_hash("x")
