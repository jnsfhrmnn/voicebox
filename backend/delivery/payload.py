"""JFW-8: Delivery-Payload aus dem JFW-7-Handoff (I/O-frei).

Spec AC „Rohtext-Payload und Idempotenz":

* Versioniertes Payload mit Run-, Audio-, Attempt-, Rohtranskript- und
  Delivery-Operation-ID sowie Text-/Payload-Hash und Zielbestätigungsflag.
* Verbotene jf-whisper-interne Textmodi ``refined|formatted|rewritten|
  bypassed|raw_fallback`` werden abgelehnt (JFW-9-Riegel).
* Identische erneute Zustellung = derselbe Zustand, niemals ein zweiter
  Versuch; dieselbe Operations-ID mit abweichendem Text, Hash oder Ziel ist
  ein fail-closed gespeicherter Konflikt — nichts wird geschrieben.

Der JFW-7-Vertrag (``dictation_handoff_v1``) wird reuse-konform KONSUMIERT
(``transcription.handoff``), nicht neu erfunden.
"""
from __future__ import annotations

from ..recording.manifest import canonical_hash
from ..transcription.handoff import validate_handoff_payload

DELIVERY_CONTRACT_VERSION = "delivery_operation_v1"

#: Einziger zulaessiger Textmodus ist der unveraenderte JFW-7-Rohtext.
FORBIDDEN_TEXT_MODES = ("refined", "formatted", "rewritten", "bypassed", "raw_fallback")

_REQUIRED_FIELDS = (
    "contract_version",
    "payload_kind",
    "delivery_operation_id",
    "handoff",
    "text_hash",
    "payload_hash",
    "target_confirmed",
)


class DeliveryVertragError(RuntimeError):
    """Fail-closed: abweichendes, verbotenes oder widerspruechliches Payload."""


def build_delivery_payload(
    *, delivery_operation_id: str, handoff: dict, target_snapshot: dict
) -> dict:
    """Baut das JFW-8-Payload aus einem gueltigen JFW-7-Handoff-Payload."""
    handoff = dict(handoff or {})
    errors = validate_handoff_payload(handoff)
    if errors:
        raise DeliveryVertragError("handoff_ungueltig:" + errors[0])
    _assert_no_forbidden_mode(handoff)
    payload_hash = canonical_hash(
        {
            "contract_version": DELIVERY_CONTRACT_VERSION,
            "delivery_operation_id": delivery_operation_id,
            "run_id": handoff["run_id"],
            "audio_hash": handoff["audio_hash"],
            "attempt_id": handoff["attempt_id"],
            "revision_id": handoff["revision_id"],
            "text_hash": handoff["text_hash"],
            "target_snapshot_hash": canonical_hash(dict(target_snapshot or {})),
            "target_confirmed": bool(handoff["target_confirmed"]),
        }
    )
    return {
        "contract_version": DELIVERY_CONTRACT_VERSION,
        "payload_kind": handoff["payload_kind"],
        "delivery_operation_id": delivery_operation_id,
        "handoff": handoff,
        "text_hash": handoff["text_hash"],
        "payload_hash": payload_hash,
        "target_confirmed": bool(handoff["target_confirmed"]),
    }


def _assert_no_forbidden_mode(payload: dict) -> None:
    for field in ("text_mode", "payload_kind"):
        mode = (payload or {}).get(field)
        if mode in FORBIDDEN_TEXT_MODES:
            raise DeliveryVertragError(f"abweichendes_payload_abgelehnt:{mode}")


def validate_delivery_payload(payload: dict) -> list[str]:
    """Fail-closed Validierung; leere Liste = Payload vertragsgemaess."""
    errors: list[str] = []
    payload = payload or {}
    for name in _REQUIRED_FIELDS:
        if name not in payload:
            errors.append(f"pflichtfeld_fehlt:{name}")
    for field in ("text_mode", "payload_kind"):
        if payload.get(field) in FORBIDDEN_TEXT_MODES:
            errors.append("textmodus_verboten")
            break
    errors.extend(validate_handoff_payload(payload.get("handoff") or {}))
    return errors


def classify_resubmission(stored: dict | None, payload: dict) -> str:
    """``created`` | ``existing`` | ``conflict`` — nie ein zweiter Versuch."""
    payload = payload or {}
    if stored is None:
        return "created"
    same_hash = stored.get("payload_hash") == payload.get("payload_hash")
    same_text = stored.get("text_hash") == payload.get("text_hash")
    same_flag = bool(stored.get("target_confirmed")) == bool(payload.get("target_confirmed"))
    if same_hash and same_text and same_flag:
        return "existing"
    return "conflict"
