"""JFW-6: Run-Manifest mit JFW-7-Handoff-Pflichtfeldern (I/O-frei, kanonischer Hash).

Spec AC „Stop, Verwerfen und Handoff": Das Handoff enthaelt mindestens
Run-ID, Audio-/Manifest-Hash, Format, Start-/Stopzeit, Stopgrund,
Geraete-Snapshot, Luecken-/Cue-Marker und Recovery-Ablauf. Der Geraete-Snapshot
traegt die stabile Geraeteidentität ausschliesslich als ``stable_id_hash`` —
Logs und Evidenz bleiben inhaltsfrei (keine volle Geraeteidentität).
"""
from __future__ import annotations

import hashlib
import json

from .run_identity import is_valid_run_id

RUN_CONTRACT_VERSION = "dictation_run_v1"
TIMEBASE = "qpc_100ns"

REQUIRED_FIELDS = (
    "run_id",
    "audio_hash",
    "format",
    "started_at_100ns",
    "ended_at_100ns",
    "stop_reason",
    "device_snapshot",
    "frames",
    "gaps",
    "sound_cue_marks",
    "recovery_procedure",
    "contract_version",
    "timebase",
)


def canonical_hash(payload: dict) -> str:
    """SHA-256 ueber sortiertes kanonisches JSON (CPU/CUDA-Builds identisch)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_run_manifest(
    *,
    run_id: str,
    audio_hash: str,
    opened_format: dict,
    started_at_100ns: int,
    ended_at_100ns: int,
    stop_reason: str,
    device_snapshot: dict,
    frames: dict,
    gaps: list,
    sound_cue_marks: list,
    recovery_procedure: dict,
) -> dict:
    return {
        "contract_version": RUN_CONTRACT_VERSION,
        "timebase": TIMEBASE,
        "run_id": run_id,
        "audio_hash": audio_hash,
        "format": dict(opened_format),
        "started_at_100ns": int(started_at_100ns),
        "ended_at_100ns": int(ended_at_100ns),
        "stop_reason": stop_reason,
        "device_snapshot": dict(device_snapshot),
        "frames": dict(frames),
        "gaps": [dict(g) for g in gaps],
        "sound_cue_marks": [dict(m) for m in sound_cue_marks],
        "recovery_procedure": dict(recovery_procedure),
    }


def validate_manifest(manifest: dict) -> list[str]:
    """Fail-closed Validierung; leere Liste = Manifest vollstaendig."""
    errors: list[str] = []
    for name in REQUIRED_FIELDS:
        if name not in manifest:
            errors.append(f"pflichtfeld_fehlt:{name}")
    if manifest.get("contract_version") != RUN_CONTRACT_VERSION:
        errors.append("vertragsversion_unbekannt")
    if manifest.get("timebase") != TIMEBASE:
        errors.append("zeitbasis_unbekannt")
    if not is_valid_run_id(manifest.get("run_id")):
        errors.append("run_id_unzulaessig")
    device = manifest.get("device_snapshot") or {}
    stable = device.get("stable_id_hash")
    if not isinstance(stable, str) or len(stable) != 64:
        errors.append("geraete_snapshot_unvollstaendig")
    procedure = manifest.get("recovery_procedure")
    if not isinstance(procedure, dict) or not procedure:
        errors.append("recovery_ablauf_fehlt")
    try:
        if int(manifest.get("ended_at_100ns", 0)) < int(manifest.get("started_at_100ns", 0)):
            errors.append("zeitgrenzen_unzulaessig")
    except (TypeError, ValueError):
        errors.append("zeitgrenzen_unzulaessig")
    return errors
