"""JFW-2: Provenienz-/Identitaetsbindung — Vertragstests.

Identity- und Payload-Hash sind deterministisch; identische Resubmission ist
idempotent; abweichender Payload bei gleicher Identitaet ist ein fail-closed
Provenienzkonflikt.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest tests/test_alignment_provenance.py
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.alignment.provenance import (  # noqa: E402
    AlignmentRequest,
    transcript_text_hash,
)


def req(**kw):
    defaults = dict(
        job_id="job-1",
        audio_asset_id="audio-1",
        audio_hash="sha256:audio",
        audio_duration_ms=60000,
        timebase="audio_ms_v1",
        transcript_run_id="run-1",
        transcript_revision_id="rev-1",
        transcript_revision_hash="sha256:text",
        language_ranges=(("de", 0, 5),),
        alignment_profile="precise_words_v1",
    )
    defaults.update(kw)
    return AlignmentRequest(**defaults)


def test_identity_hash_deterministic_and_binding():
    a, b = req(), req()
    assert a.identity_hash() == b.identity_hash()
    # Revision oder Profil aendern -> neue Identitaet.
    assert a.identity_hash() != req(transcript_revision_id="rev-2").identity_hash()
    assert a.identity_hash() != req(alignment_profile="other").identity_hash()
    assert a.identity_hash() != req(job_id="job-2").identity_hash()
    assert a.identity_hash() != req(audio_asset_id="audio-2").identity_hash()


def test_payload_hash_covers_audio_and_text():
    a = req()
    assert a.payload_hash() == req().payload_hash()
    assert a.payload_hash() != req(audio_hash="sha256:anders").payload_hash()
    assert a.payload_hash() != req(transcript_revision_hash="sha256:anders").payload_hash()
    assert a.payload_hash() != req(audio_duration_ms=60001).payload_hash()
    assert a.payload_hash() != req(language_ranges=(("en", 0, 5),)).payload_hash()
    assert a.payload_hash() != req(timebase="audio_ms_v2").payload_hash()
    # Payload bindet die Identitaet mit.
    assert a.payload_hash() != req(transcript_revision_id="rev-2").payload_hash()


def test_language_ranges_roundtrip():
    a = req(language_ranges=(("de", 0, 5), ("en", 6, 11), ("uncertain", 12, 20)))
    assert a.language_ranges == (("de", 0, 5), ("en", 6, 11), ("uncertain", 12, 20))


def test_transcript_text_hash_is_content_bound():
    assert transcript_text_hash("hallo welt") == transcript_text_hash("hallo welt")
    assert transcript_text_hash("hallo welt") != transcript_text_hash("hallo Welt")


def test_request_requires_contract_version():
    assert req().contract_version  # Version ist Teil des Vertrags
    with pytest.raises(TypeError):
        AlignmentRequest()  # unvollstaendige Binds sind unzulaessig
