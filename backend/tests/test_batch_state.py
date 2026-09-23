"""JFW-5: Batch-/Element-/Phasen-Zustandsmaschinen + Aggregation — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_state.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.state import (
    ITEM_STATES,
    ITEM_TERMINAL_STATES,
    PHASE_STATES,
    aggregate_batch,
    can_transition,
    end_state_for,
)


def test_batch_states_cover_the_contract():
    from backend.batch.state import BATCH_STATES

    for s in ("draft", "validating", "ready", "running", "pausing", "paused",
              "completed", "completed_with_issues", "canceled", "blocked"):
        assert s in BATCH_STATES


def test_legal_and_illegal_batch_transitions():
    assert can_transition("batch", "draft", "validating")
    assert can_transition("batch", "ready", "running")
    assert can_transition("batch", "running", "pausing")
    assert can_transition("batch", "pausing", "paused")
    assert can_transition("batch", "paused", "running")
    assert can_transition("batch", "running", "completed")
    assert can_transition("batch", "paused", "canceled")
    assert not can_transition("batch", "completed", "running")
    assert not can_transition("batch", "ready", "completed")
    assert not can_transition("batch", "canceled", "running")
    assert not can_transition("batch", "draft", "completed")


def test_item_states_and_terminality():
    for s in ("succeeded", "succeeded_with_warnings", "failed", "canceled",
              "blocked", "interrupted", "invalidated"):
        assert s in ITEM_TERMINAL_STATES
        assert s in ITEM_STATES
    assert "waiting" in ITEM_STATES
    assert "active" in ITEM_STATES
    assert not can_transition("item", "succeeded", "active")
    assert can_transition("item", "waiting", "active")
    assert can_transition("item", "active", "interrupted")
    # interrupted ist fortsetzbar, aber kein fachlicher Erfolg
    assert can_transition("item", "interrupted", "waiting")
    assert not can_transition("item", "interrupted", "succeeded")


def test_phase_states():
    for s in ("requested", "waiting", "active", "succeeded", "partial",
              "failed", "canceled", "blocked", "skipped"):
        assert s in PHASE_STATES
    assert not can_transition("phase", "succeeded", "active")
    assert can_transition("phase", "active", "partial")


def test_end_state_is_unique_and_explains_warnings():
    ok = {"transcribe": "succeeded", "align": "succeeded"}
    assert end_state_for(ok, []) == "succeeded"
    assert end_state_for(ok, ["hinweis"]) == "succeeded_with_warnings"
    assert end_state_for({"transcribe": "failed"}, []) == "failed"
    assert end_state_for({"transcribe": "succeeded", "align": "partial"}, ["t"]) == "succeeded_with_warnings"
    assert end_state_for({"transcribe": "succeeded", "align": "blocked"}, []) == "blocked"
    assert end_state_for({"transcribe": "canceled"}, []) == "canceled"
    assert end_state_for({"transcribe": "succeeded", "align": "skipped"}, []) == "succeeded_with_warnings"


def test_batch_aggregation_is_pure_element_aggregation():
    assert aggregate_batch(["succeeded", "succeeded"])["status"] == "completed"
    assert aggregate_batch(["succeeded", "failed"])["status"] == "completed_with_issues"
    assert aggregate_batch(["succeeded_with_warnings"])["status"] == "completed_with_issues"
    assert aggregate_batch(["canceled", "canceled"])["status"] == "canceled"
    assert aggregate_batch(["blocked", "blocked"])["status"] == "blocked"
    assert aggregate_batch(["invalidated", "blocked"])["status"] == "blocked"
    assert aggregate_batch(["canceled", "succeeded"])["status"] == "completed_with_issues"
    counts = aggregate_batch(["succeeded", "failed", "failed"])["counts"]
    assert counts == {"succeeded": 1, "failed": 2}
