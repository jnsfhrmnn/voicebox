"""JFW-8: Identity-/Payload-Hash-Bindung der Delivery-Operation (I/O-frei).

Muster JFW-2/JFW-3/JFW-6/JFW-7/JFW-11: ``identity_hash`` ist die stabile
Identitaet (Delivery-Operation-ID + JFW-7-Revision + Vertragsversion);
``payload_hash`` bindet Text-Hash, Ziel-Snapshot und Zielbestaetigungsflag.
Identischer Payload bei identischer Identitaet = idempotent (``existing``);
abweichender Payload = fail-closed (``conflict``). Die Run-ID wird im
JFW-6-Format konsumiert (``recording.run_identity``), der kanonische Hash aus
``recording.manifest.canonical_hash``.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from ..recording.manifest import canonical_hash
from ..recording.run_identity import is_valid_run_id
from ..transcription.raw_transcript import text_hash as raw_text_hash
from .payload import DELIVERY_CONTRACT_VERSION


def new_operation_id() -> str:
    """Stabile Operations-ID ``jfw8-op-<uuid4hex>`` — genau einmal je Operation."""
    return "jfw8-op-" + uuid.uuid4().hex


@dataclass(frozen=True)
class DeliveryRequest:
    delivery_operation_id: str
    run_id: str
    audio_hash: str
    attempt_id: str
    revision_id: str
    raw_text: str
    text_hash: str
    target_snapshot: dict
    target_confirmed: bool = False
    parent_operation_id: str | None = None
    contract_version: str = DELIVERY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not is_valid_run_id(self.run_id):
            raise ValueError("run_id_unzulaessig")
        if self.text_hash != raw_text_hash(self.raw_text or ""):
            raise ValueError("abweichender_rohtext")

    def identity_dict(self) -> dict:
        return {
            "delivery_operation_id": self.delivery_operation_id,
            "revision_id": self.revision_id,
            "contract_version": self.contract_version,
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_dict(self) -> dict:
        return {
            "identity": self.identity_dict(),
            "run_id": self.run_id,
            "audio_hash": self.audio_hash,
            "attempt_id": self.attempt_id,
            "text_hash": self.text_hash,
            "target_snapshot_hash": canonical_hash(dict(self.target_snapshot or {})),
            "target_confirmed": bool(self.target_confirmed),
        }

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())
