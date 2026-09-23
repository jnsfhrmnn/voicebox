"""JFW-7: Sprachwahl, Modelloutput-Provenienz und Transcribe-Riegel (I/O-frei).

Spec AC „Deutsch, Englisch und Sprachwahl":

* ``auto`` = Modellerkennung; eine erkannte Sprache ist Modelloutput mit
  Provenienz und **nie** eine sichere Nutzerabsicht.
* Die Auto-Erkennung aendert nie dauerhafte Einstellungen fuer spaetere Runs.
* Die Aufgabe bleibt ausdruecklich ``transcribe`` — ein ``translate``-Pfad ist
  im Produkt nicht erreichbar (JFW-9-/Uebersetzungsverbot).
"""
from __future__ import annotations

#: Zulaessige Spracheinstellungen (Automatik oder explizit Deutsch/Englisch).
LANGUAGE_SETTINGS = ("auto", "de", "en")


class TranslatePfadVerbotenError(RuntimeError):
    """Fail-closed: ``translate`` oder unbekannte Decode-Aufgabe ist verboten."""


def assert_task_transcribe(decode: dict) -> None:
    """Erzwingt ``task == "transcribe"``; ``translate`` ist unerreichbar."""
    task = (decode or {}).get("task")
    if task != "transcribe":
        raise TranslatePfadVerbotenError(f"translate_unerreichbar: task={task!r}")


def language_param(setting: str) -> str | None:
    """``auto`` → ``None`` (Modellerkennung), sonst der explizite Sprachcode."""
    if setting == "auto":
        return None
    if setting in ("de", "en"):
        return setting
    raise ValueError(f"spracheinstellung_unbekannt:{setting!r}")


def model_language(detected: str | None, confidence: float | None = None) -> dict:
    """Erkannte Sprache als Modelloutput — nie als sichere Nutzerabsicht."""
    return {
        "detected": detected,
        "confidence": confidence,
        "source": "model_output",
        "is_user_intent": False,
    }


def chosen_language(setting: str) -> dict:
    """Explizit gewaehlte Sprache = Nutzerabsicht."""
    if setting not in LANGUAGE_SETTINGS or setting == "auto":
        raise ValueError(f"spracheinstellung_unbekannt:{setting!r}")
    return {"setting": setting, "source": "user_setting", "is_user_intent": True}


def auto_detect_changes_settings(setting: str | None) -> bool:
    """Auto-Erkennung aendert nie dauerhafte Einstellungen fuer spaetere Runs."""
    return False


def record_language(
    setting: str, detected: str | None = None, confidence: float | None = None
) -> dict:
    """Faehrt gewaehlte und erkannte Sprache getrennt als Provenienz."""
    chosen = None if setting == "auto" else chosen_language(setting)
    det = None if detected is None else model_language(detected, confidence)
    return {"chosen": chosen, "detected": det}
