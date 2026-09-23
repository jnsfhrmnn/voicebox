"""JFW-8: Verifizierter Paste-Pfad und Clipboard-Konflikt (TDD, RED zuerst).

Spec AC: Snapshot ALLER verfügbaren Clipboard-Items/Repräsentationen,
temporäre Rohtextschreibung, genau eine Paste-Auslösung, Restore NUR bei
konfliktfreiem Zustand; fremde Änderung zwischen Snapshot und Restore wird nie
überschrieben — der eigene Restore gilt als ``nicht_ausgefuehrt``. Der
Clipboard-Snapshot lebt nur in RAM und wird nie persistiert oder geloggt.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_clipboard.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.clipboard import (
    PasteGuard,
    assert_snapshot_not_persisted,
    evaluate_restore,
    snapshot_clipboard,
)
from backend.delivery.payload import DeliveryVertragError

TEXT = "Rohtext nur temporaer in der Zwischenablage."

def _snapshot():
    return snapshot_clipboard(
        change_count=10,
        representations={
            "text/plain": b"alter Inhalt",
            "text/html": b"<b>alter Inhalt</b>",
            "image/png": b"\x89PNG-altes Bild",
        },
        item_count=3,
    )

def test_snapshot_erfasst_alle_repraesentationen_ohne_inhalt():
    snap = _snapshot()
    assert snap["change_count"] == 10
    assert snap["formats"] == ["image/png", "text/html", "text/plain"]
    assert snap["item_count"] == 3
    # Ausschließlich Hashes — niemals Clipboard-Inhalte.
    assert set(snap["representation_hashes"]) == {"text/plain", "text/html", "image/png"}
    assert all(len(h) == 64 for h in snap["representation_hashes"].values())
    assert "alter Inhalt" not in repr(snap)
    assert "PNG" not in repr(snap)

def test_snapshot_wird_nie_persistiert():
    snap = _snapshot()
    assert_snapshot_not_persisted(snap)  # inhaltsfrei = harmlos
    poisoned = dict(snap, contents={"text/plain": TEXT})
    with pytest.raises(DeliveryVertragError):
        assert_snapshot_not_persisted(poisoned)

def test_restore_nur_bei_konfliktfreiem_zustand():
    snap = _snapshot()
    out = evaluate_restore(
        snapshot=snap,
        our_write_change_count=11,
        current_change_count=11,
    )
    assert out == {
        "restore_status": "vorgesehen",
        "reason": "konfliktfrei",
        "overwrite": True,
    }

def test_fremde_aenderung_wird_nie_ueberschrieben():
    snap = _snapshot()
    out = evaluate_restore(
        snapshot=snap,
        our_write_change_count=11,
        current_change_count=12,  # fremder Prozess dazwischen
    )
    assert out == {
        "restore_status": "nicht_ausgefuehrt",
        "reason": "clipboard_conflict",
        "overwrite": False,
    }

def test_genau_eine_paste_ausloesung():
    guard = PasteGuard()
    calls = []
    guard.paste_once(lambda: calls.append("paste"))
    assert calls == ["paste"]
    with pytest.raises(DeliveryVertragError):
        guard.paste_once(lambda: calls.append("paste"))
    assert calls == ["paste"]  # genau EINE Auslösung

def test_paste_guard_zaehlt_auch_fehlgeschlagene_ausloesungen():
    guard = PasteGuard()

    def boom():
        raise RuntimeError("paste fehlgeschlagen")

    with pytest.raises(RuntimeError):
        guard.paste_once(boom)
    # Auch nach Fehler darf kein zweiter Versuch automatisch folgen.
    with pytest.raises(DeliveryVertragError):
        guard.paste_once(lambda: None)
