"""JFW-8: Recovery-Queue mit klaren Grenzen (I/O-frei).

Spec AC „Recovery und manuelle Wiederverwendung":

* Der Rohtext wird benutzergebunden geschuetzt und unabhaengig vom externen
  Zielzustand lokal recoverbar gespeichert (zeitlich begrenzt).
* ``Kopieren`` zeigt die Zwischenablage als externe Trust Boundary und wird
  nur ausdruecklich ausgefuehrt.
* ``Erneut einfügen`` erzeugt eine Kindoperation (siehe ``contract``); die alte
  Operation wird nie zurueckgesetzt.
* Nutzerbearbeitung ist eine neue Textwahrheit und wird nie still unter dem
  Hash der Rohtranskriptrevision gefuehrt.
* Ablauf oder Loeschung: Text UND zugehoerige sensible Zwi­schenstaende werden
  gemeinsam geloescht; ein inhaltsfreier Tombstone sichert die Idempotenz.
* Fehlende, beschaedigte oder nicht entschluesselbare Recovery-Daten: es wird
  KEIN Text aus Logs, Hashes, Zielinhalt oder anderen Revisionen rekonstruiert.
"""
from __future__ import annotations

from ..transcription.raw_transcript import text_hash as raw_text_hash
from .payload import DeliveryVertragError


def build_recovery_entry(
    *,
    operation_id: str,
    raw_text: str,
    text_hash: str,
    revision_id: str,
    created_at_100ns: int,
    ttl_100ns: int,
) -> dict:
    """Benutzergebundener, begrenzter Recovery-Eintrag."""
    if text_hash != raw_text_hash(raw_text or ""):
        raise DeliveryVertragError("abweichender_rohtext")
    return {
        "operation_id": operation_id,
        "raw_text": raw_text,
        "text_hash": text_hash,
        "revision_id": revision_id,
        "user_bound": True,
        "created_at_100ns": int(created_at_100ns),
        "expires_at_100ns": int(created_at_100ns) + int(ttl_100ns),
    }


def evaluate_expiry(entry: dict, now_100ns: int) -> dict:
    """Ablauf: gemeinsame Loeschung, sobald der Zeitraum abgelaufen ist."""
    entry = entry or {}
    if int(now_100ns) >= int(entry.get("expires_at_100ns") or 0):
        return {"lapsed": True, "action": "delete_together"}
    return {"lapsed": False, "action": "keep"}


def evaluate_reuse(entry: dict, candidate_text: str) -> dict:
    """Wiederverwendung: unbearbeitet bleibt unter dem urspruenglichen Hash;
    jede Bearbeitung ausserhalb von jf-whisper ist eine neue Textwahrheit."""
    entry = entry or {}
    candidate_hash = raw_text_hash(candidate_text or "")
    if candidate_hash == entry.get("text_hash"):
        return {"same_revision": True, "kind": "unveraendert", "text_hash": candidate_hash}
    return {"same_revision": False, "kind": "neue_textwahrheit", "text_hash": candidate_hash}


def evaluate_deletion(entry: dict) -> dict:
    """Gemeinsame Loeschung von Text und sensiblen Zwi­schenstaenden; der
    Tombstone bleibt inhaltsfrei und sichert die Idempotenz."""
    entry = entry or {}
    return {
        "delete": ["raw_text", "recovery_intermediates"],
        "tombstone": {
            "operation_id": entry.get("operation_id"),
            "text_hash": entry.get("text_hash"),
            "content_free": True,
        },
    }


def evaluate_missing(*, reason: str, reconstruct_from: list[str] | None = None) -> dict:
    """Fehlende Recovery-Daten: fail-closed, KEINE Rekonstruktion aus Logs,
    Hashes, Zielinhalt oder anderen Revisionen."""
    if reconstruct_from:
        raise DeliveryVertragError("rekonstruktion_verboten")
    return {
        "status": "recovery_nicht_verfuegbar",
        "reason": reason,
        "reconstructed": False,
        "sources_used": [],
    }


def manual_copy_intent(*, confirmed: bool) -> dict:
    """``Kopieren``: explizite Aktion; die Zwischenablage bleibt sichtbar
    externe Trust Boundary."""
    return {
        "executed": bool(confirmed),
        "trust_boundary": "clipboard_external",
        "notice": "Zwischenablage ist eine externe Trust Boundary",
    }
