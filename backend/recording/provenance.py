"""JFW-6: Identity-/Payload-Hash-Bindung des Aufnahme-Runs (I/O-frei).

Muster JFW-2/JFW-3/JFW-11: ``identity_hash`` ist die stabile Identität des
Runs (Run-ID + Vertragsversion); ``payload_hash`` bindet Geraete-Hash,
tatsaechlich geoeffnetes Format und Startpunkt. Identischer Payload bei
identischer Identität = idempotent (``existing``); abweichender Payload =
fail-closed (``conflict``) — auch beim erneut zugestellten JFW-7-Handoff.
"""
from __future__ import annotations

from dataclasses import dataclass

from .manifest import RUN_CONTRACT_VERSION, canonical_hash


@dataclass(frozen=True)
class RecordingRequest:
    run_id: str
    device_stable_id_hash: str
    format: dict
    started_at_100ns: int = 0
    contract_version: str = RUN_CONTRACT_VERSION

    def identity_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "contract_version": self.contract_version,
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_dict(self) -> dict:
        return {
            "identity": self.identity_dict(),
            "device_stable_id_hash": self.device_stable_id_hash,
            "format": dict(self.format),
            "started_at_100ns": int(self.started_at_100ns),
        }

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())
