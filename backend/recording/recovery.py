"""JFW-6: Recovery-Grenzen (I/O-frei).

Spec „Recovery und Fehler":

* Kein unvollstaendiges Artefakt wird als vollstaendig ausgegeben.
* Das gemessene Verlustfenster ueberschreitet die Architekturgrenze
  **250 ms** (bei Flush-Intervall <= 100 ms) nicht — Übernahme der
  JFW-11-Spike-Messung §6.2 als Regression (Werte 30,0/68,6/88,4/64,4 ms).
* Jedes als angenommen bestaetigte vollstaendige Frame liegt entweder im
  finalen Audio oder ist als konkrete Luecke sichtbar (Frame-Bilanz,
  fail-closed).
* Hash-, Format- oder Manifestbindung, die nicht stimmt, laesst den Run
  ``recovery_fehlgeschlagen`` — kein Transkript, keine Rekonstruktion.
"""
from __future__ import annotations

from .manifest import canonical_hash, validate_manifest

LOSS_WINDOW_LIMIT_MS = 250.0
FLUSH_INTERVAL_LIMIT_MS = 100.0

STATUS_FULL = "vollstaendig"
STATUS_PARTIAL = "teilweise"
STATUS_MISSING = "fehlend"

RECOVERY_FAILED = "recovery_fehlgeschlagen"


def account_frames(*, accepted_frames: int, final_frames: int, gap_frames: int) -> dict:
    """Frame-Bilanz: angenommen = final + markierte Luecke (fail-closed)."""
    unaccounted = int(accepted_frames) - int(final_frames) - int(gap_frames)
    balanced = unaccounted == 0 and int(final_frames) >= 0 and int(gap_frames) >= 0
    return {
        "accepted_frames": int(accepted_frames),
        "final_frames": int(final_frames),
        "gap_frames": int(gap_frames),
        "unaccounted_frames": unaccounted,
        "bilanz_ok": balanced,
    }


def reconcile(*, raw_bytes: int, bytes_per_frame: int, journal_frames: int) -> dict:
    """Vergleicht Rohdateilaenge gegen Journal-Fortschritt; der jeweils hoehere
    Stand ist verwertbar (Muster JFW-11, Spike §6.2)."""
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
    return max(0.0, (int(fail_100ns) - int(last_recoverable_100ns)) / 1e4)


def run_recovery(
    *,
    raw_bytes: int,
    bytes_per_frame: int,
    journal_frames: int,
    fail_100ns: int,
    last_sample_100ns: int | None,
    gap_count: int,
) -> dict:
    """Recovery-Status des Runs: vollstaendig / teilweise / fehlend."""
    rec = reconcile(
        raw_bytes=raw_bytes, bytes_per_frame=bytes_per_frame, journal_frames=journal_frames
    )
    recoverable = rec["recoverable_frames"]
    if recoverable == 0 or last_sample_100ns is None:
        return {
            "recoverable_frames": recoverable,
            "loss_window_ms": None,
            "gap_count": int(gap_count),
            "status": STATUS_MISSING,
            "meets_loss_window": False,
        }
    window = loss_window_ms(fail_100ns, last_sample_100ns)
    if window == 0.0 and int(gap_count) == 0:
        status = STATUS_FULL
    else:
        status = STATUS_PARTIAL
    return {
        "recoverable_frames": recoverable,
        "loss_window_ms": window,
        "gap_count": int(gap_count),
        "status": status,
        "meets_loss_window": window <= LOSS_WINDOW_LIMIT_MS,
    }


def verify_binding(
    *,
    manifest: dict,
    audio_hash: str,
    opened_format: dict,
    device_stable_id_hash: str,
    manifest_hash: str,
) -> dict:
    """Fail-closed Bindungspruefung fuer die Recovery eines alten Runs.

    Stimmt Hash-, Format- oder Manifestbindung nicht, bleibt der Run
    ``recovery_fehlgeschlagen`` — er wird nicht transkribiert oder
    rekonstruiert.
    """
    errors: list[str] = list(validate_manifest(manifest))
    if manifest.get("audio_hash") != audio_hash:
        errors.append("audio_hash_abweichend")
    if manifest.get("format") != dict(opened_format):
        errors.append("format_abweichend")
    if canonical_hash(manifest) != manifest_hash:
        errors.append("manifest_hash_abweichend")
    device = manifest.get("device_snapshot") or {}
    if device.get("stable_id_hash") != device_stable_id_hash:
        errors.append("geraete_identitaet_abweichend")
    if errors:
        return {"ok": False, "reason": RECOVERY_FAILED, "errors": errors}
    return {"ok": True, "reason": None, "errors": []}


def is_complete(*, recovery: dict, bilanz: dict) -> bool:
    """Nur ein vollstaendiger Run mit geschlossener Frame-Bilanz darf als
    vollstaendig ausgegeben werden."""
    return recovery.get("status") == STATUS_FULL and bool(bilanz.get("bilanz_ok"))
