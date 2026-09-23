"""JFW-8: Ziel-Snapshot und Trust Boundary — Vertragstests (TDD, RED zuerst).

Spec AC „Ziel-Snapshot und Trust Boundary": gebundener Ziel-Snapshot mit
Run-ID, Prozess-/Fenster-/Control-Identität, Session-/Desktopgrenze, Capability
und Erfassungszeit; geändertes/beendetes/neu erzeugtes Ziel wird nie durch ein
anderes ersetzt; erhöhte/geschützte/passwortartige/Secure-Desktop-/andere-
Sitzungs-Ziele sind fail-closed blockiert; Titel, Prozessname oder
Bildschirmposition allein genügen nie als Bindung.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_delivery_target.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.delivery.target import (
    build_target_snapshot,
    revalidate_target,
    validate_target,
)
from backend.recording.run_identity import new_run_id


def _snapshot(**overrides) -> dict:
    base = dict(
        run_id=new_run_id(),
        process_identity={
            "pid": 4242,
            "started_at_100ns": 13000000000,
            "exe_hash": "e" * 64,
        },
        window_handle=123456,
        control_path="window/edit[2]",
        session_id="s-1",
        desktop="WinSta0\\Default",
        capability="direct_text",
        captured_at_100ns=14000000000,
        title="Dokument1 — Editor",
        binding_basis="composite",
        blocking_flags=[],
    )
    base.update(overrides)
    return build_target_snapshot(**base)

def test_vollstaendiger_snapshot_ist_vertragsgemaess():
    assert validate_target(_snapshot()) == []

def test_schwache_identitaet_allein_genuegt_nie():
    weak = _snapshot(binding_basis="title")
    assert "schwache_identitaet" in validate_target(weak)
    weak2 = _snapshot(binding_basis="process_name")
    assert "schwache_identitaet" in validate_target(weak2)
    weak3 = _snapshot(binding_basis="screen_position")
    assert "schwache_identitaet" in validate_target(weak3)

def test_unvollstaendige_prozessidentitaet_ist_unzureichend():
    snap = _snapshot(process_identity={"pid": 1, "started_at_100ns": 2})
    assert "identitaet_unzureichend" in validate_target(snap)

def test_blockierte_ziele_sind_fail_closed():
    for flag in (
        "elevated",
        "protected",
        "not_editable",
        "password_like",
        "secure_desktop",
        "other_session",
    ):
        snap = _snapshot(blocking_flags=[flag], capability="blocked")
        errors = validate_target(snap)
        assert f"ziel_blocked:{flag}" in errors, flag
    # Blockierende Flags mit behaupteter Auto-Capability sind widersprüchlich.
    snap = _snapshot(blocking_flags=["elevated"], capability="direct_text")
    errors = validate_target(snap)
    assert "blocked_muss_blocked_sein" in errors

def test_unzulaessige_run_id_und_capability():
    assert "run_id_unzulaessig" in validate_target(_snapshot(run_id="rando-1"))
    assert "capability_unbekannt" in validate_target(_snapshot(capability="magic"))
    assert "zeitpunkt_unzulaessig" in validate_target(_snapshot(captured_at_100ns=0))

def test_revalidierung_unveraendertes_ziel():
    snap = _snapshot()
    assert revalidate_target(snap, dict(snap)) == {"ok": True, "reason": None}

def test_revalidierung_geaendertes_ziel_wird_nie_ersetzt():
    snap = _snapshot()
    # Anderes Fenster/Control: kein anderes Ziel, fail-closed.
    changed = dict(snap, window_handle=999)
    out = revalidate_target(snap, changed)
    assert out["ok"] is False
    assert out["reason"] == "target_changed"
    # Prozess neu erzeugt (gleiche PID, andere Erzeugungszeit): sichtbar geändert.
    recreated = dict(snap, process_identity=dict(snap["process_identity"], started_at_100ns=15000000000))
    out = revalidate_target(snap, recreated)
    assert out["ok"] is False
    assert out["reason"] == "target_changed"
    # Dokument/Editorinstanz-Wechsel über den Control-Pfad sichtbar.
    doc = dict(snap, control_path="window/edit[3]")
    out = revalidate_target(snap, doc)
    assert out["ok"] is False
    assert out["reason"] == "target_changed"

def test_revalidierung_session_und_capability():
    snap = _snapshot()
    out = revalidate_target(snap, dict(snap, session_id="s-2"))
    assert out["ok"] is False
    assert out["reason"] == "session_geaendert"
    out = revalidate_target(snap, dict(snap, capability="verified_paste"))
    assert out["ok"] is False
    assert out["reason"] == "capability_geaendert"

def test_revalidierung_verschwindendes_oder_blockiertes_ziel():
    snap = _snapshot()
    out = revalidate_target(snap, None)
    assert out["ok"] is False
    assert out["reason"] == "target_changed"
    out = revalidate_target(snap, dict(snap, blocking_flags=["elevated"]))
    assert out["ok"] is False
    assert out["reason"] == "ziel_blocked"
