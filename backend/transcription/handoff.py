"""JFW-7: JFW-8-Handoff-Payload und Ableitungsbindung (I/O-frei).

Spec AC „Übergabe und Datenschutz":

* Genau ein versioniertes Payload mit Run-/Audio-/Attempt-/Revision-ID, Rohtext
  und Hash, Sprache, Backend-/Modellprofil, Stopgrund und Zielbestätigungsflag.
* Ein Downstream-Payload mit ``refined``/``formatted``/``rewritten`` oder
  semantisch abweichendem Text wird nie als JFW-7-Rohtranskript akzeptiert.
* JFW-2/JFW-3/JFW-11-Ableitungen binden sich an Revision-ID + Hash; der Rohtext
  bleibt feldweise unverändert.
"""
from __future__ import annotations

from .raw_transcript import FORBIDDEN_KINDS, RAW_KIND, text_hash

HANDOFF_CONTRACT_VERSION = "dictation_handoff_v1"

_REQUIRED_FIELDS = (
    "contract_version",
    "payload_kind",
    "run_id",
    "audio_hash",
    "attempt_id",
    "revision_id",
    "raw_text",
    "text_hash",
    "language",
    "backend_model_profile",
    "stop_reason",
    "target_confirmed",
)


class HandoffVertragError(RuntimeError):
    """Fail-closed: abweichendes oder verbotenes Payload."""


def build_handoff_payload(
    *,
    run_id: str,
    audio_hash: str,
    attempt_id: str,
    revision_id: str,
    raw_text: str,
    text_hash: str,
    language: dict,
    backend_model_profile: dict,
    stop_reason: str,
    target_confirmed: bool = False,
) -> dict:
    return {
        "contract_version": HANDOFF_CONTRACT_VERSION,
        "payload_kind": RAW_KIND,
        "run_id": run_id,
        "audio_hash": audio_hash,
        "attempt_id": attempt_id,
        "revision_id": revision_id,
        "raw_text": raw_text,
        "text_hash": text_hash,
        "language": dict(language or {}),
        "backend_model_profile": dict(backend_model_profile or {}),
        "stop_reason": stop_reason,
        "target_confirmed": bool(target_confirmed),
    }


def assert_payload_kind(payload: dict) -> None:
    """Nur ``raw_transcript`` ist ein JFW-7-Rohtranskript-Payload."""
    kind = (payload or {}).get("payload_kind")
    if kind in FORBIDDEN_KINDS:
        raise HandoffVertragError(f"abweichendes_payload_abgelehnt:{kind}")
    if kind != RAW_KIND:
        raise HandoffVertragError(f"abweichendes_payload_abgelehnt:{kind!r}")


def validate_handoff_payload(payload: dict) -> list[str]:
    """Fail-closed Validierung; leere Liste = Payload vertragsgemaess."""
    errors: list[str] = []
    payload = payload or {}
    for name in _REQUIRED_FIELDS:
        if name not in payload:
            errors.append(f"pflichtfeld_fehlt:{name}")
    kind = payload.get("payload_kind")
    if kind in FORBIDDEN_KINDS:
        errors.append("payload_kind_verboten")
    elif kind != RAW_KIND:
        errors.append("abweichendes_payload")
    if (
        "raw_text" in payload
        and "text_hash" in payload
        and payload.get("text_hash") != text_hash(payload.get("raw_text") or "")
    ):
        errors.append("abweichender_rohtext")
    return errors


def build_derivation_binding(*, revision_id: str, text_hash: str, source: str) -> dict:
    """Bindung einer Ableitung (JFW-2/JFW-3/JFW-11) an Revision-ID + Hash."""
    return {
        "revision_id": revision_id,
        "text_hash": text_hash,
        "source": source,
        "contract_version": HANDOFF_CONTRACT_VERSION,
    }


def verify_derivation_text(binding: dict, text: str) -> bool:
    """Der abgeleitete Text muss zum gebundenen Rohtext-Hash passen (unverändert)."""
    return (binding or {}).get("text_hash") == text_hash(text)
