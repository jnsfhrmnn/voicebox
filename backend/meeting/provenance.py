"""JFW-11: Identity-/Payload-Hash-Bindung des Meeting-Jobs (I/O-frei).

Muster JFW-2/JFW-3: ``identity_hash`` ist die stabile Identität des
Meeting-Laufs (Job + Meeting-Run + JFW-6-Run-Referenz + Manifest-Hash +
Vertragsversion — eine neue Manifest-Revision ist eine neue Identität);
``payload_hash`` bindet Track-Hashes und JFW-2-/JFW-3-Revisionen.
Identischer Payload bei erneuter Einreichung = idempotent (``existing``);
abweichender Payload bei gleicher Identität = fail-closed (``conflict``).
"""
from __future__ import annotations

from dataclasses import dataclass

from .contract import RESULT_CONTRACT_VERSION
from .manifest import canonical_hash


@dataclass(frozen=True)
class MeetingRequest:
    job_id: str
    meeting_run_id: str
    jfw6_run_reference: str
    track_hashes: tuple[str, ...]
    manifest_hash: str
    jfw2_result_hash: str | None = None
    jfw3_result_hash: str | None = None
    contract_version: str = RESULT_CONTRACT_VERSION

    def identity_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "meeting_run_id": self.meeting_run_id,
            "jfw6_run_reference": self.jfw6_run_reference,
            "manifest_hash": self.manifest_hash,
            "contract_version": self.contract_version,
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_dict(self) -> dict:
        return {
            "identity": self.identity_dict(),
            "track_hashes": sorted(self.track_hashes),
            "jfw2_result_hash": self.jfw2_result_hash,
            "jfw3_result_hash": self.jfw3_result_hash,
        }

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())
