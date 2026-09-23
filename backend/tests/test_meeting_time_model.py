"""JFW-11: Zeitmodell `qpc_offset_drift_v1` — Vertragstests (TDD).

Reproduziert die Spike-Messwerte (``features/evidence/JFW-11-capture-spike-report.md``
§5/§9.1) als Regression: Offset-only-Alignment verfehlt die JFW-11-Zielwerte ueber
45 min (Median 30,7 ms), Offset + linearer Drift hält sie (0,326 ms).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_time_model.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.meeting.time_model import (
    QUALITY_OK,
    QUALITY_UNCERTAIN,
    TIME_MODEL_VERSION,
    PacketRecord,
    fit_pair_alignment,
    fit_track_model,
    is_real_packet,
)


def _packets(rate=48000, seconds=600.0, drift_ppm=0.0, packet_ms=10.0, start_qpc=99999):
    fpp = int(rate * packet_ms / 1000)  # Frames je Paket
    n = int(seconds * 1000 / packet_ms)
    nominal_frame_100ns = 1e7 / rate
    packets = []
    frame = 0
    for i in range(n):
        qpc = start_qpc + round(frame * nominal_frame_100ns * (1 + drift_ppm * 1e-6))
        packets.append(
            PacketRecord(seq=i, qpc_100ns=qpc, devpos_frames=frame, frames=fpp, bytes=fpp * 8)
        )
        frame += fpp
    return packets


def test_version_is_pinned():
    assert TIME_MODEL_VERSION == "qpc_offset_drift_v1"


def test_shadow_callback_is_not_a_real_packet():
    # Spike §4.1: NAudio feuert leere Schatten-Callbacks (bytes=0, qpc=0, devpos=0).
    shadow = PacketRecord(seq=1, qpc_100ns=0, devpos_frames=0, frames=0, bytes=0)
    real = PacketRecord(seq=2, qpc_100ns=10, devpos_frames=480, frames=480, bytes=3840)
    assert is_real_packet(shadow) is False
    assert is_real_packet(real) is True


def test_fit_recovers_drift_and_offset():
    packets = _packets(seconds=600.0, drift_ppm=41.0)
    model = fit_track_model(packets, rate_hz=48000)
    assert model.version == TIME_MODEL_VERSION
    assert model.quality == QUALITY_OK
    assert abs(model.drift_ppm - 41.0) < 0.5
    assert model.residual_max_ms < 0.5
    # Frame 0 liegt bei start_qpc (99999 * 100ns)
    assert abs(model.to_shared_100ns(0.0) - 99999.0) < 1.0


def test_too_few_packets_is_sync_unsicher():
    # 50 ms Lauf = 5 Pakete — unterhalb min_packets keine belastbare Regression.
    model = fit_track_model(_packets(seconds=0.05), rate_hz=48000, min_packets=20)
    assert model.quality == QUALITY_UNCERTAIN
    assert model.reason_code == "zu_wenig_pakete"


def test_offset_only_fails_spike_targets_but_drift_meets_them():
    # 10 Marker-Paare ueber 45 min, delta 26.9 -> 138.6 ms (Spike §9.1, 148.1 ms/h).
    pairs = []
    for i in range(10):
        t_s = 360.0 + 300.0 * i
        delta_ms = 26.9 + (138.6 - 26.9) * (t_s - 360.0) / 2700.0
        a = t_s * 1e7
        pairs.append((a, a + delta_ms * 1e4))

    offset_only = fit_pair_alignment(pairs, include_drift=False)
    s = offset_only.summary_ms()
    # Spike-Gemessen: median 30.70 / p95 55.86 / max 55.87 ms -> Ziel verfehlt.
    assert 29.5 <= s["median"] <= 32.0
    assert 55.0 <= s["p95"] <= 56.5
    assert 55.0 <= s["max"] <= 56.5
    assert offset_only.meets_targets() is False

    with_drift = fit_pair_alignment(pairs, include_drift=True)
    s2 = with_drift.summary_ms()
    # Spike-Gemessen: 0.326 / 0.410 / 0.419 ms -> Ziele 20/50/100 ms klar erfuellt.
    assert s2["median"] < 1.0
    assert s2["max"] < 1.0
    assert with_drift.meets_targets() is True


def test_short_run_both_variants_under_one_ms():
    # Spike §5: 40-s-Kurzlauf, 7 Marker — beide Varianten < 1 ms.
    pairs = []
    for i in range(7):
        t_s = 5.0 + 5.0 * i
        delta_ms = 15.7 + 0.2 * i
        a = t_s * 1e7
        pairs.append((a, a + delta_ms * 1e4))
    for include_drift in (False, True):
        al = fit_pair_alignment(pairs, include_drift=include_drift)
        assert al.summary_ms()["max"] < 1.0
        assert al.meets_targets() is True


def test_pair_alignment_quality_flagged_unsicher_below_min_pairs():
    al = fit_pair_alignment([(1e7, 1e7 + 1e4)], include_drift=True, min_pairs=2)
    assert al.quality == QUALITY_UNCERTAIN
