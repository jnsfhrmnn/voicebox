"""JFW-11: Recovery-Vertrag — Verlustfenster je Spur ≤ 250 ms (I/O-frei).

Spec „Quellentrennung"/„Recovery": Bei abruptem Ausfall beträgt das reale
Capture-Verlustfenster je Spur höchstens 250 ms; der Zustand jeder Spur wird
unabhängig als vollständig, teilweise oder fehlend ausgewiesen.

Gemessen (Spike §6.2): Verlustfenster ohne Final-Flush 30 bis 100 ms (Flush 100 ms)
bzw. ≤ 88 ms (Flush 250 ms); begrenzend ist der Journal-/Flush-Pfad. Die
Rohdatei liegt dem Journal oft 512 bis 7 680 Frames vorn — der jeweils höhere Stand
gilt als verwertbar.
"""
from __future__ import annotations

LOSS_WINDOW_LIMIT_MS = 250.0
FLUSH_INTERVAL_LIMIT_MS = 100.0

STATUS_FULL = "vollstaendig"
STATUS_PARTIAL = "teilweise"
STATUS_MISSING = "fehlend"


def reconcile_track(
    *, raw_bytes: int, bytes_per_frame: int, journal_frames: int
) -> dict:
    """Vergleicht Rohdateilänge gegen Journal-Fortschritt (Spike §6.2).

    Der jeweils höhere Stand ist verwertbar; ``source`` benennt den begrenzenden
    beziehungsweise führenden Pfad.
    """
    raw_frames = raw_bytes // bytes_per_frame if bytes_per_frame > 0 else 0
    if raw_frames >= journal_frames:
        return {
            "raw_frames": raw_frames,
            "journal_frames": journal_frames,
            "recoverable_frames": raw_frames,
            "source": "raw",
        }
    return {
        "raw_frames": raw_frames,
        "journal_frames": journal_frames,
        "recoverable_frames": journal_frames,
        "source": "journal",
    }


def loss_window_ms(fail_100ns: int, last_recoverable_100ns: int) -> float:
    """Reales Verlustfenster zwischen instrumentiertem Ausfallzeitpunkt und dem
    letzten recoverbaren Sample (in Millisekunden)."""
    return max(0.0, (fail_100ns - last_recoverable_100ns) / 1e4)


def track_recovery(
    *,
    track_id: str,
    raw_frames: int,
    journal_frames: int,
    fail_100ns: int,
    last_sample_100ns: int | None,
    gap_count: int,
    rate_hz: int,
) -> dict:
    """Unabhängiger Spurstatus: vollstaendig / teilweise / fehlend."""
    del rate_hz
    recoverable = max(raw_frames, journal_frames)
    if recoverable == 0 or last_sample_100ns is None:
        return {
            "track_id": track_id,
            "recoverable_frames": recoverable,
            "loss_window_ms": None,
            "gap_count": gap_count,
            "status": STATUS_MISSING,
            "meets_loss_window": False,
        }
    window = loss_window_ms(fail_100ns, last_sample_100ns)
    if window == 0.0 and gap_count == 0:
        status = STATUS_FULL
    else:
        status = STATUS_PARTIAL
    return {
        "track_id": track_id,
        "recoverable_frames": recoverable,
        "loss_window_ms": window,
        "gap_count": gap_count,
        "status": status,
        "meets_loss_window": window <= LOSS_WINDOW_LIMIT_MS,
    }
