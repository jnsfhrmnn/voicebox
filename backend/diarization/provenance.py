"""JFW-3: Provenienz-/Identitätsbindung und Sprecheranzahlmodus (fail-closed).

Ein Diarisierungsauftrag bindet Job, Audio, Audiodauer und Zeitbasis,
Transkriptions-Run, Transkriptrevision, die JFW-2-Referenz (Status und — falls
vorhanden — der autoritative Ergebnis-Hash), den Sprecheranzahlmodus, das
Diarisierungsprofil und die Ergebnisvertragsversion:

* ``identity_hash`` — kanonischer Hash der Identitaet
  ``(job_id, audio_asset_id, transcript_revision_id, jfw2_reference,
  diarization_profile, speaker_spec, contract_version)``. UNIQUE in
  ``diarization_results``: hoechstens ein autoritatives Ergebnis je Identitaet.
  Eine geaenderte Sprecheranzahlvorgabe erzeugt eine NEUE Ergebnisrevision;
  das fruehere Ergebnis bleibt revisionsgebunden erhalten.
* ``payload_hash`` — kanonischer Hash des VOLLSTAENDIGEN Auftrags inkl.
  ``audio_hash``, ``transcript_revision_hash``, ``transcript_run_id``, Dauer,
  Zeitbasis und ``jfw2_result_hash``. Identische Resubmission ist idempotent;
  abweichender Payload bei gleicher Identitaet ist ein fail-closed
  Provenienzkonflikt.

``SpeakerSpec`` validiert Auto-/Exakt-/Bereichsvorgabe fail-closed (1-8,
min <= max); unplausible Vorgaben werden konkret abgelehnt (AC „Eingabe
fail-closed abgelehnt") statt geraten oder verwässert.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .contract import RESULT_CONTRACT_VERSION, canonical_hash

SPEAKER_MODES = ("auto", "exact", "range")
MIN_SPEAKERS = 1
MAX_SPEAKERS = 8


class SpeakerSpecError(RuntimeError):
    """Fail-closed Ablehnung einer unplausiblen Sprecheranzahlvorgabe."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class SpeakerSpec:
    """Sprecheranzahlmodus: ``auto`` | ``exact`` (1-8) | ``range`` (min/max 1-8)."""

    mode: str = "auto"
    count: int | None = None
    minimum: int | None = None
    maximum: int | None = None

    @staticmethod
    def parse(mode, count=None, minimum=None, maximum=None) -> SpeakerSpec:
        if mode not in SPEAKER_MODES:
            raise SpeakerSpecError("speaker_spec_invalid")
        if mode == "auto":
            if count is not None or minimum is not None or maximum is not None:
                raise SpeakerSpecError("speaker_spec_invalid")
            return SpeakerSpec(mode="auto")
        if mode == "exact":
            if minimum is not None or maximum is not None:
                raise SpeakerSpecError("speaker_spec_invalid")
            if not _is_int(count) or not (MIN_SPEAKERS <= count <= MAX_SPEAKERS):
                raise SpeakerSpecError("speaker_spec_invalid")
            return SpeakerSpec(mode="exact", count=int(count))
        # range
        if count is not None:
            raise SpeakerSpecError("speaker_spec_invalid")
        if not _is_int(minimum) or not _is_int(maximum):
            raise SpeakerSpecError("speaker_spec_invalid")
        if not (
            MIN_SPEAKERS <= minimum <= maximum <= MAX_SPEAKERS
        ):
            raise SpeakerSpecError("speaker_spec_invalid")
        return SpeakerSpec(mode="range", minimum=int(minimum), maximum=int(maximum))

    def as_dict(self) -> dict:
        return {
            "mode": self.mode,
            "count": self.count,
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


def transcript_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DiarizationRequest:
    """Unveraenderlicher Diarisierungsauftrag (vollstaendige Provenienzbinding)."""

    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int
    timebase: str
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    jfw2_reference_status: str
    jfw2_result_hash: str | None = None
    speaker_spec: SpeakerSpec = field(default_factory=SpeakerSpec)
    diarization_profile: str = "meeting_speakers_v1"
    contract_version: str = RESULT_CONTRACT_VERSION

    def identity_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "audio_asset_id": self.audio_asset_id,
            "transcript_revision_id": self.transcript_revision_id,
            "jfw2_reference_status": self.jfw2_reference_status,
            "jfw2_result_hash": self.jfw2_result_hash,
            "diarization_profile": self.diarization_profile,
            "speaker_spec": self.speaker_spec.as_dict(),
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
        }

    def identity_hash(self) -> str:
        return canonical_hash(self.identity_dict())

    def payload_hash(self) -> str:
        return canonical_hash(self.payload_dict())
