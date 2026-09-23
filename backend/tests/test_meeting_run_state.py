"""JFW-11: Run-Zustandsmaschine (Dual-Source Capture Contract) — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_meeting_run_state.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.meeting.run_state import (
    CANCELED,
    FAILED,
    PREPARING,
    RECORDING_DEGRADED,
    RECORDING_DUAL,
    SECURED_DUAL,
    SECURED_PARTIAL,
    RunStateError,
    cancel_run,
    invalidate,
    secure_run,
    source_lost,
    source_returned,
    start_dual,
)

ALL_OK = {"vollstaendig": 2, "teilweise": 0, "fehlend": 0}
ONE_MISSING = {"vollstaendig": 1, "teilweise": 0, "fehlend": 1}
ONE_PARTIAL = {"vollstaendig": 1, "teilweise": 1, "fehlend": 0}
NONE = {"vollstaendig": 0, "teilweise": 0, "fehlend": 2}


def test_happy_path_to_secured_dual():
    st = start_dual(PREPARING, mic_ready=True, remote_ready=True,
                    recovery_ready=True, first_blocks_mapped=True)
    assert st == RECORDING_DUAL
    assert secure_run(st, ALL_OK) == SECURED_DUAL


def test_start_requires_all_readiness():
    for kw in ("mic_ready", "remote_ready", "recovery_ready", "first_blocks_mapped"):
        args = dict(mic_ready=True, remote_ready=True,
                    recovery_ready=True, first_blocks_mapped=True)
        args[kw] = False
        with pytest.raises(RunStateError):
            start_dual(PREPARING, **args)


def test_start_only_from_preparing():
    with pytest.raises(RunStateError):
        start_dual(RECORDING_DUAL, mic_ready=True, remote_ready=True,
                   recovery_ready=True, first_blocks_mapped=True)


def test_source_loss_is_degraded_and_silence_without_error_flag():
    st = source_lost(RECORDING_DUAL, source_error=False)
    assert st == RECORDING_DEGRADED
    # Geraeteverlust ist dagegen ein echter Fehler (Spike §6.6) — ebenfalls degraded.
    assert source_lost(RECORDING_DUAL, source_error=True) == RECORDING_DEGRADED


def test_source_return_requires_explicit_confirmation():
    with pytest.raises(RunStateError):
        source_returned(RECORDING_DEGRADED, confirmed=False)
    assert source_returned(RECORDING_DEGRADED, confirmed=True) == RECORDING_DUAL


def test_secure_outcomes_follow_track_recovery():
    assert secure_run(RECORDING_DUAL, ALL_OK) == SECURED_DUAL
    assert secure_run(RECORDING_DEGRADED, ONE_PARTIAL) == SECURED_PARTIAL
    assert secure_run(RECORDING_DEGRADED, ONE_MISSING) == SECURED_PARTIAL
    assert secure_run(RECORDING_DUAL, NONE) == FAILED


def test_cancel_is_only_confirmed():
    assert cancel_run(RECORDING_DUAL, confirmed=True) == CANCELED
    with pytest.raises(RunStateError):
        cancel_run(RECORDING_DUAL, confirmed=False)


def test_invalidate_only_secured():
    assert invalidate(SECURED_DUAL) == "invalidated"
    assert invalidate(SECURED_PARTIAL) == "invalidated"
    with pytest.raises(RunStateError):
        invalidate(RECORDING_DUAL)


def test_degraded_stays_degraded_on_second_loss():
    st = source_lost(RECORDING_DEGRADED, source_error=False)
    assert st == RECORDING_DEGRADED
