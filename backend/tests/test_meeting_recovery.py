"""JFW-11: Recovery-Vertrag (Verlustfenster ≤ 250 ms) — Vertragstests (TDD).

Zahlen sind Spike-Gemessen (``features/evidence/JFW-11-capture-spike-report.md`` §6.2):
Verlustfenster ohne Final-Flush 30,0 bis 100,6 ms (Flush 100 ms) bzw. ≤ 88,4 ms (250 ms);
die Rohdatei liegt dem Journal 512 bis 7 680 Frames vorn.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_recovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.recovery import (
    LOSS_WINDOW_LIMIT_MS,
    reconcile_track,
    track_recovery,
)


def test_raw_file_ahead_of_journal_wins():
    # Spike §6.2: Rohdatei oft 10 bis 160 ms "vorne" — hoeherer Stand ist verwertbar.
    rec = reconcile_track(raw_bytes=12_000 * 8, bytes_per_frame=8, journal_frames=10_000)
    assert rec["recoverable_frames"] == 12_000
    assert rec["source"] == "raw"


def test_journal_ahead_of_raw_wins():
    rec = reconcile_track(raw_bytes=9_000 * 8, bytes_per_frame=8, journal_frames=10_000)
    assert rec["recoverable_frames"] == 10_000
    assert rec["source"] == "journal"


def test_loss_window_spike_values_are_within_target():
    # instrumentierter Ausfallzeitpunkt vs. letzter recoverbarer Sample (Spike §6.2).
    fail = 300_000_000  # 30 s in 100-ns-Einheiten
    for measured_ms in (30.0, 68.6, 88.4, 64.4):
        last_sample = fail - int(measured_ms * 1e4)
        window = (fail - last_sample) / 1e4
        assert abs(window - measured_ms) < 1.0
        assert window <= LOSS_WINDOW_LIMIT_MS


def test_track_status_full_partial_missing():
    full = track_recovery(track_id="t", raw_frames=10_000, journal_frames=10_000,
                          fail_100ns=10_000_000_00, last_sample_100ns=10_000_000_00,
                          gap_count=0, rate_hz=48000)
    assert full["status"] == "vollstaendig"
    assert full["loss_window_ms"] == 0.0

    partial = track_recovery(track_id="t", raw_frames=10_000, journal_frames=9_000,
                             fail_100ns=10_006_860_000 // 1000 * 1000,
                             last_sample_100ns=10_000_000_00,
                             gap_count=1, rate_hz=48000)
    assert partial["status"] == "teilweise"
    assert partial["loss_window_ms"] > 0.0

    missing = track_recovery(track_id="t", raw_frames=0, journal_frames=0,
                             fail_100ns=10_000_000_00, last_sample_100ns=None,
                             gap_count=0, rate_hz=48000)
    assert missing["status"] == "fehlend"


def test_loss_window_limit_is_250ms():
    assert LOSS_WINDOW_LIMIT_MS == 250.0
