"""JFW-8: Recovery-Queue mit klaren Grenzen (TDD, RED zuerst beobachtet).

Spec AC „Recovery und manuelle Wiederverwendung": benutzergebundener,
zeitlich begrenzter Rohtext-Eintrag; ``Kopieren`` zeigt die Zwischenablage als
externe Trust Boundary und läuft nur ausdrücklich; ``Erneut einfügen`` =
Kindoperation mit neuem Ziel-Snapshot und eigenem Einmalbudget (alte Operation
nie zurückgesetzt); Nutzerbearbeitung ist eine neue Textwahrheit (nie still
unter dem alten Hash); gemeinsame Löschung mit inhaltsfreiem Tombstone;
fehlende/beschädigte/nicht entschlüsselbare Recovery-Daten: KEINE
Rekonstruktion aus Logs, Hashes, Zielinhalt oder anderen Revisionen.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_recovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.delivery.payload import DeliveryVertragError
from backend.delivery.recovery import (
    build_recovery_entry,
    evaluate_deletion,
    evaluate_expiry,
    evaluate_missing,
    evaluate_reuse,
    manual_copy_intent,
)
from backend.transcription.raw_transcript import text_hash

TEXT = "wiederherzustellender Rohtext."

def _entry(**overrides) -> dict:
    base = dict(
        operation_id="jfw8-op-" + "1" * 32,
        raw_text=TEXT,
        text_hash=text_hash(TEXT),
        revision_id="rev-1",
        created_at_100ns=1_000_000_000,
        ttl_100ns=1_000_000_000_000,
    )
    base.update(overrides)
    return build_recovery_entry(**base)

def test_recovery_eintrag_ist_benutzergebunden_und_begrenzt():
    entry = _entry()
    assert entry["user_bound"] is True
    assert entry["expires_at_100ns"] == 1_001_000_000_000
    assert entry["text_hash"] == text_hash(TEXT)

def test_ablauf_und_loeschung_gemeinsam_mit_tombstone():
    lapsed = evaluate_expiry(_entry(), now_100ns=1_001_000_000_001)
    assert lapsed == {"lapsed": True, "action": "delete_together"}
    kept = evaluate_expiry(_entry(), now_100ns=500)
    assert kept == {"lapsed": False, "action": "keep"}
    # Gemeinsame Löschung: Text UND Zwischenstände, Tombstone inhaltsfrei.
    out = evaluate_deletion(_entry())
    assert out["delete"] == ["raw_text", "recovery_intermediates"]
    assert out["tombstone"]["text_hash"] == text_hash(TEXT)
    assert "raw_text" not in out["tombstone"]
    assert out["tombstone"]["content_free"] is True

def test_nutzerbearbeitung_ist_neue_textwahrheit():
    entry = _entry()
    edited = evaluate_reuse(entry, candidate_text="bearbeiteter Text.")
    assert edited["same_revision"] is False
    assert edited["kind"] == "neue_textwahrheit"
    assert edited["text_hash"] == text_hash("bearbeiteter Text.")
    # Unbearbeitete Wiederverwendung bleibt unter dem ursprünglichen Hash.
    same = evaluate_reuse(entry, candidate_text=TEXT)
    assert same["same_revision"] is True
    assert same["text_hash"] == entry["text_hash"]

def test_fehlende_recovery_daten_werden_nie_rekonstruiert():
    out = evaluate_missing(reason="nicht_entschluesselbar")
    assert out == {
        "status": "recovery_nicht_verfuegbar",
        "reason": "nicht_entschluesselbar",
        "reconstructed": False,
        "sources_used": [],
    }
    with pytest.raises(DeliveryVertragError):
        evaluate_missing(reason="beschädigt", reconstruct_from=["logs", "hashes"])

def test_kopieren_ist_explizit_und_zeigt_trust_boundary():
    out = manual_copy_intent(confirmed=True)
    assert out == {
        "executed": True,
        "trust_boundary": "clipboard_external",
        "notice": "Zwischenablage ist eine externe Trust Boundary",
    }
    out = manual_copy_intent(confirmed=False)
    assert out["executed"] is False
    assert out["trust_boundary"] == "clipboard_external"
