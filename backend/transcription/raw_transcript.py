"""JFW-7: Unverändertes Rohtranskript — heiliger Unveränderlichkeits-Vertrag (I/O-frei).

Spec AC „Rohtranskript und Inhaltstreue":

* Genau eine unveraenderte ``raw_transcript``-Revision (Text, kanonischer Hash,
  erkannte/gewaehlte Sprache, Segmente, Backend-/Modellprovenienz, Audio-Bindung).
* Keine Nachbereinigung: Satzzeichen, Gross-/Kleinschreibung, Fuellwoerter,
  Wiederholungen und Selbstkorrekturen der Modellausgabe bleiben unangetastet.
* ``user_edited`` ist eine getrennte Kindrevision mit Elternbezug; die Rohrevision
  und ihr Hash bleiben unveraendert.
* Retranskription erzeugt eine neue Revision eigener Provenienz — nie stiller Ersatz.
* ``no_speech`` erzeugt keinen leeren oder halluzinierten Erfolgstext.
* Beschaedigtes/hash-veraendertes Audio endet konkret — keine Rekonstruktion aus
  Cache, Log oder frueherer Revision.

**JFW-9-Verbot (Vertragsbestandteil, NIE):** KEIN Text-LLM, KEIN Refinement am
Rohtranskript. Verbotene Kinds (``refined``/``formatted``/``rewritten``) sind
fail-closed blockiert; dieses Modul besitzt keine Transformationsfunktion.
"""
from __future__ import annotations

import copy
import hashlib

RAW_KIND = "raw_transcript"
USER_EDITED_KIND = "user_edited"
#: JFW-9-verbotene Ergebnis-Kinds — nie als JFW-7-Rohtranskript akzeptiert.
FORBIDDEN_KINDS = ("refined", "formatted", "rewritten")

_ALLOWED_SOURCES = ("model_output", "user_setting")


class RawVertragError(RuntimeError):
    """Fail-closed: Unveraendert-/Revisionsvertrag verletzt."""


def text_hash(text: str) -> str:
    """Kanonischer Hash des Rohtextes — byteexakt ueber UTF-8, keine Normalisierung."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def has_speech(text: str, segments: list | None = None) -> bool:
    """Kein sicherer Sprachinhalt = kein Erfolgstext (``no_speech``)."""
    if segments:
        return True
    return bool((text or "").strip())


def verify_unmodified(stored_text: str, candidate_text: str) -> bool:
    """Byte-Gleichheit des Rohtextes (JFW-9-Riegel)."""
    return text_hash(stored_text) == text_hash(candidate_text)


def verify_audio_binding(expected_hash: str, actual_hash: str) -> bool:
    """Audio-Bindung: Abweichung = beschaedigt/veraendert, keine Rekonstruktion."""
    return bool(expected_hash) and expected_hash == actual_hash


def assert_raw_kind(kind: str | None) -> None:
    """Kinderlaubnis der Ergebnisart; verbotene/unbekannte Kinds sind blockiert."""
    if kind is None:
        return  # Altzeilen gelten als raw_transcript
    if kind in FORBIDDEN_KINDS:
        raise RawVertragError(f"jfw9_verboten:{kind}")
    if kind not in (RAW_KIND, USER_EDITED_KIND):
        raise RawVertragError(f"kind_unbekannt:{kind}")


def _check_language_output(language_output: dict) -> dict:
    out = dict(language_output or {})
    source = out.get("source")
    if source not in _ALLOWED_SOURCES:
        raise RawVertragError(f"sprachquelle_unbekannt:{source!r}")
    is_intent = out.get("is_user_intent")
    if source == "model_output" and is_intent is not False:
        raise RawVertragError("modelloutput_als_nutzerabsicht")
    if source == "user_setting" and is_intent is not True:
        raise RawVertragError("nutzerabsicht_ohne_bekenntnis")
    return out


def build_raw_revision(
    *,
    text: str,
    segments: list | None,
    language_output: dict,
    audio_hash: str,
    attempt_id: str,
    model_provenance: dict,
) -> dict:
    """Die genau-eine unveraenderte Rohrevision (Inhaltstreue inklusive)."""
    assert_raw_kind(RAW_KIND)
    lang = _check_language_output(language_output)
    return {
        "revision_kind": RAW_KIND,
        "parent_revision_id": None,
        "text": text,
        "text_hash": text_hash(text),
        "segments": [dict(s) for s in (segments or [])],
        "language": lang,
        "provenance": {
            "audio_hash": audio_hash,
            "attempt_id": attempt_id,
            "model": dict(model_provenance or {}),
            "unmodified": True,
        },
    }


def user_edited_revision(parent: dict, new_text: str) -> dict:
    """Getrennte ``user_edited``-Kindrevision; die Rohrevision bleibt unveraendert."""
    if not isinstance(parent, dict) or parent.get("revision_kind") not in (RAW_KIND, None):
        raise RawVertragError("elter_ist_keine_rawrevision")
    return {
        "revision_kind": USER_EDITED_KIND,
        "parent_revision_id": parent.get("revision_id"),
        "text": new_text,
        "text_hash": text_hash(new_text),
        "segments": [],
        "language": copy.deepcopy(parent.get("language")) or {},
        "provenance": {
            "edit_of_text_hash": parent.get("text_hash"),
            "audio_hash": (parent.get("provenance") or {}).get("audio_hash"),
            "unmodified": False,
        },
    }
