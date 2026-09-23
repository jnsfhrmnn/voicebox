"""JFW-6: Run-Identität und monotone Zeitpunkte — Vertragstests (TDD, RED zuerst).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_recording_run_identity.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.recording.run_identity import (
    RUN_ID_PREFIX,
    RunClock,
    RunIdentityError,
    is_valid_run_id,
    new_run_id,
)


def test_new_run_id_has_stable_format():
    run_id = new_run_id()
    assert run_id.startswith(f"{RUN_ID_PREFIX}-")
    assert is_valid_run_id(run_id)
    tail = run_id[len(RUN_ID_PREFIX) + 1 :]
    assert len(tail) == 32
    assert all(c in "0123456789abcdef" for c in tail)


def test_run_ids_are_unique_per_run():
    assert new_run_id() != new_run_id()


@pytest.mark.parametrize(
    "run_id",
    [
        "",
        "jfw6-run-",
        "jfw6-run-XYZ",
        "jfw6-run-abc",
        "andere-run-0" * 2 + "0" * 4,
        "jfw6-run-" + "A" * 32,  # Grossschreibung ist keine zulaessige Hex-Form
    ],
)
def test_invalid_run_ids_rejected(run_id):
    assert not is_valid_run_id(run_id)


def test_clock_points_are_strictly_monotonic():
    clock = RunClock()
    assert clock.point(1_000) == 1_000
    assert clock.point(2_000) == 2_000


def test_clock_rejects_repeat_and_backwards_points():
    clock = RunClock()
    clock.point(1_000)
    with pytest.raises(RunIdentityError):
        clock.point(1_000)  # Auto-Repeat: identischer Punkt
    with pytest.raises(RunIdentityError):
        clock.point(500)  # Ruecklauf
    assert clock.point(1_001) == 1_001


def test_clock_first_point_must_be_positive():
    clock = RunClock()
    with pytest.raises(RunIdentityError):
        clock.point(0)
