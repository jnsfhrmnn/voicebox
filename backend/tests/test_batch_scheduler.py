"""JFW-5: Ressourcenzulassung, Diktatvorrang, Preflight — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_scheduler.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.profile import resource_policy
from backend.batch.scheduler import admit_phase


def test_serial_policy_admits_exactly_one_ai_phase():
    rp = resource_policy()
    a = admit_phase(active_ai_phases=0, resource_policy=rp)
    assert a.admitted
    assert a.reason_code is None
    b = admit_phase(active_ai_phases=1, resource_policy=rp)
    assert not b.admitted
    assert b.reason_code == "ressource_belegt"
    assert b.state == "warten"


def test_dictation_hold_blocks_new_phases_visibly():
    rp = resource_policy()
    a = admit_phase(active_ai_phases=0, resource_policy=rp, dictation_active=True)
    assert not a.admitted
    assert a.reason_code == "dictat_vorrang"
    assert a.state == "warten"


def test_phase_without_safe_yield_needs_reserved_dictation_resources():
    rp = resource_policy()
    a = admit_phase(active_ai_phases=0, resource_policy=rp,
                    phase_has_safe_yield=False, dictation_reserved=False)
    assert not a.admitted
    assert a.reason_code == "diktatressourcen_nicht_reserviert"
    b = admit_phase(active_ai_phases=0, resource_policy=rp,
                    phase_has_safe_yield=False, dictation_reserved=True)
    assert b.admitted


def test_preflight_blocks_before_risky_write_but_keeps_existing_results():
    rp = dict(resource_policy(), min_free_bytes=1000)
    a = admit_phase(active_ai_phases=0, resource_policy=rp, free_bytes=10)
    assert not a.admitted
    assert a.reason_code == "speicher_gering"
    b = admit_phase(active_ai_phases=0, resource_policy=rp, free_bytes=5000)
    assert b.admitted


def test_waiting_reason_is_never_a_processing_error():
    rp = resource_policy()
    a = admit_phase(active_ai_phases=1, resource_policy=rp)
    assert a.state == "warten"
    assert not getattr(a, "is_error", False)
