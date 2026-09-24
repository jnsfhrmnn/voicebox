# -*- coding: utf-8 -*-
"""USCRX-2026-16007 — Regressionstests P-01/P-02/P-04 + RL-16-Validierung.

P-01: fensteruebergreifende Ersetzungsspruenge (start < 0) in redact_text.
P-02: Idempotenz Doppellauf mark_interrupted (current_epoch implementiert).
P-04: Listen-Nestung assert_content_free -> ValueError.
RL-16: apply_action-Vertragsvalidierung (fail-closed statt stiller No-Ops).
"""
import pytest

from backend.batch.recovery import mark_interrupted
from backend.minutes.pseudonym import apply_action, assert_content_free
from backend.minutes.redaction import redact_text


# ── P-01: fensteruebergreifende Ersetzungsspruenge ──────────────────────────


def test_p01_start_kleiner_null_wird_geklammert():
    out = redact_text("abcdef", [{"start": -2, "end": 2, "replacement": "X"}], offset=0)
    assert out == "Xcdef", "Fenster-Anfang darf nicht negativ indizieren"


def test_p01_vor_dem_fenster_keine_wirkung():
    out = redact_text("abcdef", [{"start": -5, "end": -1, "replacement": "X"}], offset=0)
    assert out == "abcdef"


def test_p01_mehrere_praege_spruenge_stabil():
    plan = [
        {"start": -3, "end": 2, "replacement": "A"},
        {"start": 3, "end": 5, "replacement": "B"},
    ]
    assert redact_text("abcdef", plan, offset=0) == "AcBf"


# ── P-02: Idempotenz mark_interrupted ──────────────────────────────────────


def test_p02_doppellauf_idempotent():
    attempts = [
        {"attempt_id": "a1", "status": "active"},
        {"attempt_id": "a2", "status": "done", "resumable": True},
    ]
    once = mark_interrupted(attempts, current_epoch="epoch-2")
    twice = mark_interrupted(once, current_epoch="epoch-2")
    assert once == twice, "Doppellauf muss byteidentisch sein"
    assert once[0]["status"] == "interrupted"
    assert once[0]["interrupted_at_epoch"] == "epoch-2"
    assert once[1]["resumable"] is False


def test_p02_epoch_wird_auf_markierten_versuchen_vermerkt():
    out = mark_interrupted([{"status": "running"}], current_epoch="e1")
    assert out[0]["interrupted_at_epoch"] == "e1"


# ── P-04: Listen-Nestung im Content-Guard ──────────────────────────────────


def test_p04_listen_nestung_wird_abgelehnt():
    with pytest.raises(ValueError):
        assert_content_free({"entries": [{"name": "Klarname"}]})


def test_p04_inhaltsfreie_struktur_bleibt_erlaubt():
    assert_content_free({"entries": [{"entry_id": "e1", "state": "bestaetigt"}]}) is None


# ── RL-16: apply_action Vertrags-Validierung ───────────────────────────────


def _register():
    return {
        "contract_version": "pseudonym_v1",
        "register_id": "reg-1",
        "status": "vorgeschlagen",
        "revision": 0,
        "revision_id": "0",
        "parent_revision_id": None,
        "entries": [
            {"entry_id": "e1", "kind": "person", "state": "vorgeschlagen",
             "pseudonym": "Person 1", "occurrences": [], "candidate_id": "c1"},
            {"entry_id": "e2", "kind": "person", "state": "vorgeschlagen",
             "pseudonym": "Person 2", "occurrences": [], "candidate_id": "c2"},
        ],
    }


def test_rl16_fremde_entry_id_ist_fehler():
    with pytest.raises(ValueError):
        apply_action(_register(), {"kind": "bestaetigen", "entry_id": "gibtsnicht"})


def test_rl16_umbenennen_ohne_pseudonym_ist_fehler():
    with pytest.raises(ValueError):
        apply_action(_register(), {"kind": "umbenennen", "entry_id": "e1"})


def test_rl16_zusammenlegen_mit_fremden_ids_ist_fehler():
    with pytest.raises(ValueError):
        apply_action(_register(), {"kind": "zusammenlegen", "entry_ids": ["e1", "xxx"]})


def test_rl16_gueltige_aktionen_bleiben_moeglich():
    out = apply_action(_register(), {"kind": "bestaetigen", "entry_id": "e1"})
    assert out["entries"][0]["state"] == "bestaetigt"
    out2 = apply_action(_register(), {"kind": "umbenennen", "entry_id": "e1", "pseudonym": "Frau A"})
    assert out2["entries"][0]["pseudonym"] == "Frau A"
