"""JFW-6: Recovery-Grenzen — Vertragstests (TDD, RED zuerst).

Spec „Recovery und Fehler": kein unvollstaendiges Artefakt als vollstaendig,
gemessenes Verlustfenster innerhalb der Architekturgrenze (<= 250 ms bei Flush
<= 100 ms, JFW-11-Spike §6.2 als Regression), Frame-Bilanz fail-closed
(angenommen = final + markierte Luecke) und fail-closed Bindungspruefung
(Hash/Format/Manifest -> ``recovery_fehlgeschlagen``, kein Transkript).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_recovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.recording.manifest import build_run_manifest, canonical_hash
from backend.recording.recovery import (
    LOSS_WINDOW_LIMIT_MS,
    STATUS_FULL,
    STATUS_MISSING,
    STATUS_PARTIAL,
    account_frames,
    is_complete,
    loss_window_ms,
    reconcile,
    run_recovery,
    verify_binding,
)
from backend.recording.run_identity import new_run_id


def test_frame_accounting_balanced_is_ok():
    out = account_frames(accepted_frames=100, final_frames=100, gap_frames=0)
    assert out["bilanz_ok"] is True
    assert out["unaccounted_frames"] == 0


def test_frame_accounting_gap_marked_frames_count():
    # Jedes als angenommen bestaetigte Frame liegt entweder im finalen Audio
    # oder ist als konkrete Luecke sichtbar.
    out = account_frames(accepted_frames=100, final_frames=80, gap_frames=20)
    assert out["bilanz_ok"] is True
    assert out["gap_frames"] == 20


def test_frame_accounting_unaccounted_frames_fail_closed():
    out = account_frames(accepted_frames=100, final_frames=80, gap_frames=10)
    assert out["bilanz_ok"] is False
    assert out["unaccounted_frames"] == 10


def test_reconcile_prefers_higher_of_raw_and_journal():
    raw_ahead = reconcile(raw_bytes=1_600, bytes_per_frame=2, journal_frames=500)
    assert raw_ahead["recoverable_frames"] == 800
    assert raw_ahead["source"] == "raw"
    journal_ahead = reconcile(raw_bytes=400, bytes_per_frame=2, journal_frames=500)
    assert journal_ahead["recoverable_frames"] == 500
    assert journal_ahead["source"] == "journal"


def test_loss_window_spike_values_are_within_target():
    # JFW-11-Spike §6.2: 30.0 / 68.6 / 88.4 / 64.4 ms — Regression gegen die
    # JFW-6-Architekturgrenze.
    for window_ms in (30.0, 68.6, 88.4, 64.4):
        last = 10_000_000
        fail = last + int(window_ms * 1e4)
        assert loss_window_ms(fail, last) <= LOSS_WINDOW_LIMIT_MS


def test_run_recovery_full_partial_missing():
    full = run_recovery(
        raw_bytes=2_000, bytes_per_frame=2, journal_frames=1_000,
        fail_100ns=1_000_000, last_sample_100ns=1_000_000, gap_count=0,
    )
    assert full["status"] == STATUS_FULL
    assert full["meets_loss_window"] is True

    partial = run_recovery(
        raw_bytes=2_000, bytes_per_frame=2, journal_frames=1_000,
        fail_100ns=1_300_000, last_sample_100ns=1_000_000, gap_count=2,
    )
    assert partial["status"] == STATUS_PARTIAL
    assert partial["loss_window_ms"] == 30.0

    missing = run_recovery(
        raw_bytes=0, bytes_per_frame=2, journal_frames=0,
        fail_100ns=1_000_000, last_sample_100ns=None, gap_count=0,
    )
    assert missing["status"] == STATUS_MISSING
    assert missing["meets_loss_window"] is False


def test_loss_window_over_limit_is_flagged():
    out = run_recovery(
        raw_bytes=2_000, bytes_per_frame=2, journal_frames=1_000,
        fail_100ns=3_600_000, last_sample_100ns=1_000_000, gap_count=1,
    )
    assert out["loss_window_ms"] == 260.0
    assert out["meets_loss_window"] is False


def _manifest():
    return build_run_manifest(
        run_id=new_run_id(),
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        started_at_100ns=1_000_000,
        ended_at_100ns=9_000_000,
        stop_reason="toggle",
        device_snapshot={"stable_id_hash": "b" * 64},
        frames={"accepted_frames": 10, "final_frames": 10, "gap_frames": 0},
        gaps=[],
        sound_cue_marks=[],
        recovery_procedure={"ablauf": "max(raw, journal)"},
    )


def test_verify_binding_ok():
    manifest = _manifest()
    out = verify_binding(
        manifest=manifest,
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        device_stable_id_hash="b" * 64,
        manifest_hash=canonical_hash(manifest),
    )
    assert out["ok"] is True
    assert out["reason"] is None


def test_verify_binding_hash_mismatch_is_recovery_failed():
    manifest = _manifest()
    out = verify_binding(
        manifest=manifest,
        audio_hash="c" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        device_stable_id_hash="b" * 64,
        manifest_hash=canonical_hash(manifest),
    )
    assert out["ok"] is False
    assert out["reason"] == "recovery_fehlgeschlagen"
    assert "audio_hash_abweichend" in out["errors"]


def test_verify_binding_format_change_is_recovery_failed():
    manifest = _manifest()
    out = verify_binding(
        manifest=manifest,
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 2, "rate_hz": 48_000},
        device_stable_id_hash="b" * 64,
        manifest_hash=canonical_hash(manifest),
    )
    assert out["ok"] is False
    assert "format_abweichend" in out["errors"]


def test_verify_binding_manifest_hash_mismatch_is_recovery_failed():
    manifest = _manifest()
    out = verify_binding(
        manifest=manifest,
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        device_stable_id_hash="b" * 64,
        manifest_hash="f" * 64,
    )
    assert out["ok"] is False
    assert "manifest_hash_abweichend" in out["errors"]


def test_verify_binding_device_mismatch_is_recovery_failed():
    manifest = _manifest()
    out = verify_binding(
        manifest=manifest,
        audio_hash="a" * 64,
        opened_format={"sample_format": "pcm_s16le", "channels": 1, "rate_hz": 48_000},
        device_stable_id_hash="e" * 64,
        manifest_hash=canonical_hash(manifest),
    )
    assert out["ok"] is False
    assert "geraete_identitaet_abweichend" in out["errors"]


def test_incomplete_artifact_is_never_complete():
    full_ok = run_recovery(
        raw_bytes=2_000, bytes_per_frame=2, journal_frames=1_000,
        fail_100ns=1_000_000, last_sample_100ns=1_000_000, gap_count=0,
    )
    balanz_ok = account_frames(accepted_frames=10, final_frames=10, gap_frames=0)
    balanz_kaputt = account_frames(accepted_frames=10, final_frames=8, gap_frames=0)
    partial = run_recovery(
        raw_bytes=2_000, bytes_per_frame=2, journal_frames=1_000,
        fail_100ns=1_300_000, last_sample_100ns=1_000_000, gap_count=1,
    )
    assert is_complete(recovery=full_ok, bilanz=balanz_ok) is True
    assert is_complete(recovery=full_ok, bilanz=balanz_kaputt) is False
    assert is_complete(recovery=partial, bilanz=balanz_ok) is False
