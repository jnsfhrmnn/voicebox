"""JFW-13: Zustandsmaschine (Protokoll Result Contract) — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_contract.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.minutes.contract import (
    NON_TERMINAL_STATES,
    STATES,
    can_transition,
    outcome_state,
)


def test_states_match_spec_table():
    assert set(STATES) == {
        "queued", "waiting_for_local_artifact", "preparing", "generating",
        "generated", "generated_with_warnings", "failed", "canceled", "invalidated",
    }


def test_non_terminal_states():
    assert set(NON_TERMINAL_STATES) == {
        "queued", "waiting_for_local_artifact", "preparing", "generating",
    }


def test_valid_transitions():
    assert can_transition("queued", "preparing")
    assert can_transition("queued", "waiting_for_local_artifact")
    assert can_transition("waiting_for_local_artifact", "preparing")
    assert can_transition("preparing", "generating")
    assert can_transition("generating", "generated")
    assert can_transition("generating", "generated_with_warnings")
    assert can_transition("generating", "failed")
    assert can_transition("generating", "canceled")
    assert can_transition("queued", "canceled")
    assert can_transition("queued", "failed")


def test_invalidated_on_revision_change():
    assert can_transition("generated", "invalidated")
    assert can_transition("generated_with_warnings", "invalidated")


def test_terminal_states_do_not_restart():
    for state in ("generated", "generated_with_warnings", "failed", "canceled"):
        for target in ("generating", "preparing", "generated"):
            assert not can_transition(state, target)


def test_no_transition_to_self_or_backwards():
    assert not can_transition("generating", "preparing")
    assert not can_transition("generating", "queued")
    assert not can_transition("queued", "generated")


def test_outcome_state_by_warnings():
    assert outcome_state([]) == "generated"
    assert outcome_state([{"kind": "partially_aligned"}]) == "generated_with_warnings"
