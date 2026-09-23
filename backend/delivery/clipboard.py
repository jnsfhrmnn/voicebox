"""JFW-8: Verifizierter Paste-Pfad und Clipboard-Konflikt (I/O-frei).

Spec AC: ausschliesslich ein verifizierter Paste-Pfad snapshotet ALLE
verfuegbaren Clipboard-Items und Repraesentationen, schreibt temporaer den
Rohtext, loest GENAU EINE Paste-Ausloesung aus und stellt NUR bei
konfliktfreiem Clipboard-Zustand wieder her. Erkennt JFW-8 eine fremde
Zwischenablage-Aenderung zwischen Snapshot und Restore, wird der neue Inhalt
nie ueberschrieben und der eigene Restore als ``nicht_ausgefuehrt`` gemeldet.

Der Clipboard-Snapshot lebt ausschliesslich in RAM der Adapter-Ausfuehrung:
er wird nie persistiert und nie geloggt (``assert_snapshot_not_persisted``).
"""
from __future__ import annotations

import hashlib

from .payload import DeliveryVertragError

#: Schluessel, die Clipboard-/Textinhalte tragen duerften — im Snapshot verboten.
_CONTENT_KEYS = ("contents", "content", "items", "raw_text", "text", "representations_raw")


def snapshot_clipboard(*, change_count: int, representations: dict, item_count: int) -> dict:
    """Vollstaendiger Snapshot als inhaltsfreie Repraesentations-Hashes."""
    hashes = {}
    for fmt, content in (representations or {}).items():
        raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
        hashes[fmt] = hashlib.sha256(raw).hexdigest()
    return {
        "change_count": int(change_count),
        "formats": sorted(hashes),
        "representation_hashes": hashes,
        "item_count": int(item_count),
    }


def assert_snapshot_not_persisted(snapshot: dict) -> None:
    """Fail-closed: ein inhaltstragender Snapshot darf nie persistiert werden."""
    for key in _CONTENT_KEYS:
        if key in (snapshot or {}):
            raise DeliveryVertragError(f"clipboard_snapshot_inhaltstragend:{key}")


def evaluate_restore(
    *, snapshot: dict, our_write_change_count: int, current_change_count: int
) -> dict:
    """Restore-Entscheidung: nur konfliktfreier Zustand wird wiederhergestellt.

    ``our_write_change_count`` ist die Aenderungszaehlung NACH unserer eigenen
    temporaeren Rohtextschreibung. Eine abweichende aktuelle Zaehlung bedeutet:
    ein fremder Prozess hat die Zwischenablage veraendert.
    """
    if int(current_change_count) != int(our_write_change_count):
        return {
            "restore_status": "nicht_ausgefuehrt",
            "reason": "clipboard_conflict",
            "overwrite": False,
        }
    return {
        "restore_status": "vorgesehen",
        "reason": "konfliktfrei",
        "overwrite": True,
    }


class PasteGuard:
    """Erzwingt GENAU EINE Paste-Ausloesung — auch nach Fehlversuchen."""

    def __init__(self) -> None:
        self._used = False

    def paste_once(self, action):
        if self._used:
            raise DeliveryVertragError("mehrfach_paste_verboten")
        self._used = True
        return action()
