"""JFW-2: Provenienz-/Identitaetsbindung (fail-closed).

Ein Alignment-Auftrag bindet Job, Audio, Audiodauer und Zeitbasis,
Transkriptions-Run, Transkriptrevision, Sprachbereiche, Alignment-Profil und
Ergebnisvertragsversion:

* ``identity_hash`` — kanonischer Hash der Identitaet
  ``(job_id, audio_asset_id, transcript_revision_id, alignment_profile,
  contract_version)``. UNIQUE in ``alignment_results``: hoechstens ein
  autoritatives Ergebnis je Identitaet.
* ``payload_hash`` — kanonischer Hash des VOLLSTAENDIGEN Auftrags inkl.
  ``audio_hash``, ``transcript_revision_hash``, Dauer, Zeitbasis und
  Sprachbereiche. Identische Resubmission ist idempotent; abweichender Payload
  bei gleicher Identitaet ist ein fail-closed Provenienzkonflikt.

``transcript_text_hash(text)`` ist der Inhaltshash der gebundenen Revision; die
Einreichung prueft ihn gegen den mitgelieferten ``transcript_revision_hash``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .contract import RESULT_CONTRACT_VERSION, canonical_hash


def transcript_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class AlignmentRequest:
    """Unveraenderlicher Alignment-Auftrag (vollstaendige Provenienzbinding)."""

    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int
    timebase: str
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    language_ranges: tuple = field(default_factory=tuple)
    alignment_profile: str = "precise_words_v1"
    contract_version: str = RESULT_CONTRACT_VERSION

    def identity_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "audio_asset_id": self.audio_asset_id,
            "transcript_revision_id": self.transcript_revision_id,
            "alignment_profile": self.alignment_profile,
            "contract_version": self.contract_version,
        }

    def payload_dict(self) -> dict:
        return {
            **self.identity_dict(),
            "audio_hash": self.audio_hash,
            "audio_duration_ms": int(self.audio_duration_ms),
            "timebase": self.timebase,
            "transcript_run_id": self.transcript_run_id,
            "transcript_revision_hash": self.transcript_revision_hash,
            "language_ranges": [list(r) for r in self.language_ranges],
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())
