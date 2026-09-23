"""JFW-6: Identity-/Payload-Hash-Bindung des Aufnahme-Runs — Vertragstests (TDD, RED zuerst).

Muster JFW-2/JFW-3/JFW-11: ``identity_hash`` ist die stabile Identitaet des
Runs (Run-ID + Vertragsversion); ``payload_hash`` bindet Geraete-Hash und
tatsaechlich geoeffnetes Format. Identischer Payload = idempotent, abweichender
Payload bei gleicher Identitaet = fail-closed.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_provenance.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.recording.provenance import RecordingRequest
from backend.recording.run_identity import new_run_id


def _request(**overrides) -> RecordingRequest:
    base = dict(
        run_id=new_run_id(),
        device_stable_id_hash="b" * 64,
        format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
    )
    base.update(overrides)
    return RecordingRequest(**base)


def test_identity_and_payload_hashes_are_deterministic():
    a, b = _request(), None
    b = _request(run_id=a.run_id)
    assert a.identity_hash() == b.identity_hash()
    assert a.payload_hash() == b.payload_hash()


def test_identity_changes_with_run_id():
    assert _request().identity_hash() != _request().identity_hash()


def test_payload_changes_with_format_and_device():
    base = _request()
    fmt = _request(
        run_id=base.run_id,
        format={"sample_format": "pcm_s16le", "channels": 2, "rate_hz": 48_000},
    )
    dev = _request(run_id=base.run_id, device_stable_id_hash="c" * 64)
    assert fmt.identity_hash() == base.identity_hash()
    assert fmt.payload_hash() != base.payload_hash()
    assert dev.payload_hash() != base.payload_hash()


def test_identity_ignores_format_so_a_format_conflict_is_a_payload_conflict():
    base = _request()
    fmt = _request(
        run_id=base.run_id,
        format={"sample_format": "pcm_f32le", "channels": 1, "rate_hz": 44_100},
    )
    assert fmt.identity_hash() == base.identity_hash()
    assert fmt.payload_hash() != base.payload_hash()
