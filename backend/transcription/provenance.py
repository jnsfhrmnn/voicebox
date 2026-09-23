"""JFW-7: Identity-/Payload-Hash-Bindung des Transkriptions-Runs (I/O-frei).

Muster JFW-2/JFW-3/JFW-6/JFW-11: ``identity_hash`` ist die stabile Identität des
Runs (Run-ID + Quellart + Ergebnisvertragsversion); ``payload_hash`` bindet
Audio-/Manifest-Hash und den eingefrorenen Snapshot. Identischer Payload bei
identischer Identität = idempotent (``existing``); abweichender Payload =
fail-closed (``conflict``). Uploads und Retranskriptionen erhalten ohne
JFW-6-Live-Run dieselbe stabile Identität.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from ..recording.manifest import canonical_hash
from .snapshot import RESULT_CONTRACT_VERSION, snapshot_hash, validate_snapshot

#: Zulaessige Quellarten eines Runs.
SOURCE_KINDS = ("jfw6_handoff", "upload", "retranscribe")


def new_run_id(kind: str = "run") -> str:
    """Stabile Run-ID (``jfw7-run-``/``jfw7-upload-``/``jfw7-retranscribe-`` + 32 Hex)."""
    if kind not in ("run", "upload", "retranscribe"):
        raise ValueError(f"run_id_art_unbekannt:{kind!r}")
    return f"jfw7-{kind}-" + uuid.uuid4().hex


@dataclass(frozen=True)
class TranscriptionRequest:
    run_id: str
    source_kind: str
    audio_hash: str
    manifest_hash: str | None = None
    capture_id: str | None = None
    snapshot: dict = field(default_factory=dict)
    contract_version: str = RESULT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"quelle_unbekannt:{self.source_kind!r}")

    def identity_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "source_kind": self.source_kind,
            "contract_version": self.contract_version,
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_dict(self) -> dict:
        return {
            "identity": self.identity_dict(),
            "audio_hash": self.audio_hash,
            "manifest_hash": self.manifest_hash,
            "snapshot_hash": snapshot_hash(self.snapshot),
        }

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())

    def snapshot_errors(self) -> list[str]:
        errors = validate_snapshot(self.snapshot)
        if self.snapshot.get("audio_hash") != self.audio_hash:
            errors.append("audio_hash_abweichung")
        if self.snapshot.get("manifest_hash") != self.manifest_hash:
            errors.append("manifest_hash_abweichung")
        return errors
