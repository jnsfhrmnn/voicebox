"""JFW-11: Atomarer Capture-Manifest (I/O-frei, kanonischer Hash).

Spec „Quellentrennung" (Manifest-AC): Der Manifest bindet Meeting-Run-ID,
JFW-6-Run-Referenz, Start/Ende, Stop-Grund, gemeinsame Zeitbasis, beide
erwarteten Quellenrollen, Track-/Abschnitts-IDs und -Hashes, Geräte-/Formatdaten,
Lücken, Sound-Cue-Markierungen, Sync-Version/-Qualität, Recovery-Status und
Vertragsversion.
"""
from __future__ import annotations

import hashlib
import json

CAPTURE_CONTRACT_VERSION = "dual_source_capture_v1"

TIMEBASE = "qpc_100ns"

REQUIRED_FIELDS = (
    "meeting_run_id",
    "jfw6_run_reference",
    "started_at_100ns",
    "ended_at_100ns",
    "stop_reason",
    "timebase",
    "source_roles",
    "tracks",
    "gaps",
    "sound_cue_marks",
    "sync",
    "recovery_status",
    "contract_version",
)


def canonical_hash(payload: dict) -> str:
    """SHA-256 ueber sortiertes kanonisches JSON (CPU/CUDA-Builds identisch)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_capture_manifest(
    *,
    meeting_run_id: str,
    jfw6_run_ref: str,
    started_at_100ns: int,
    ended_at_100ns: int,
    stop_reason: str,
    tracks: list,
    gaps: list,
    sound_cue_marks: list,
    sync: dict,
    recovery_status: dict,
) -> dict:
    roles = sorted({t.role for t in tracks}, key=lambda r: (r != "mic", r))
    return {
        "contract_version": CAPTURE_CONTRACT_VERSION,
        "meeting_run_id": meeting_run_id,
        "jfw6_run_reference": jfw6_run_ref,
        "started_at_100ns": int(started_at_100ns),
        "ended_at_100ns": int(ended_at_100ns),
        "stop_reason": stop_reason,
        "timebase": TIMEBASE,
        "source_roles": roles,
        "tracks": [t.to_dict() for t in tracks],
        "gaps": [dict(g) for g in gaps],
        "sound_cue_marks": [dict(m) for m in sound_cue_marks],
        "sync": dict(sync),
        "recovery_status": dict(recovery_status),
    }


def validate_manifest(manifest: dict) -> list[str]:
    """Fail-closed Validierung; leere Liste = Manifest vollständig."""
    errors: list[str] = []
    for name in REQUIRED_FIELDS:
        if name not in manifest:
            errors.append(f"pflichtfeld_fehlt:{name}")
    roles = list(manifest.get("source_roles", []))
    if sorted(roles) != ["mic", "remote"]:
        errors.append("quellenrollen_unvollstaendig")
    sync = manifest.get("sync") or {}
    if not sync.get("version") or not sync.get("quality"):
        errors.append("sync_unvollstaendig")
    if manifest.get("contract_version") != CAPTURE_CONTRACT_VERSION:
        errors.append("vertragsversion_unbekannt")
    return errors


def is_cue_window(manifest: dict, role: str, t_100ns: int) -> bool:
    """Start-/Endton-Fenster sind markiert und nie Teilnehmer- oder Namensevidenz."""
    for mark in manifest.get("sound_cue_marks", []):
        if mark.get("role") == role and int(mark["start_100ns"]) <= t_100ns <= int(mark["end_100ns"]):
            return True
    return False
