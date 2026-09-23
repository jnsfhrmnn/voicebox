"""JFW-5: Element-Pipeline (Serie, Teilfehlerpolitik, Commit-Erhalt) — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_pipeline.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.pipeline import PhaseOutcome, run_item
from backend.tests.test_batch_profile import prof


def item():
    return {"item_id": "jfw5-item-1", "source": {"path": "C:/data/clip.wav"}}


def executors(results, calls=None):
    calls = calls if calls is not None else []

    def make(phase):
        def run(phase_name, ctx):
            calls.append(phase_name)
            if phase_name in results:
                out = results[phase_name]
                return out() if callable(out) else out
            return ok({"result_hash": f"h_{phase_name}"})
        return run

    return {p: make(p) for p in ("transcribe", "align", "diarize", "export", "minutes")}


def ok(ref):
    return PhaseOutcome(status="succeeded", commit_ref=ref, reason_code=None, warnings=[])


def test_phases_run_in_confirmed_order_with_same_bindings():
    calls = []
    out = run_item(
        item=item(),
        profile=prof(),
        executors=executors(
            {p: ok({"result_hash": f"h_{p}"}) for p in ("transcribe", "align", "diarize", "export")},
            calls,
        ),
    )
    assert calls == ["transcribe", "align", "diarize", "export"]
    assert out["end_state"] == "succeeded"
    # dieselbe Element-, Quellen- und Profilrevision in jeder Phase
    for phase in calls:
        assert out["phases"][phase]["item_id"] == "jfw5-item-1"
        assert out["phases"][phase]["profile_hash"] == out["profile_hash"]


def test_failure_without_partial_blocks_dependent_phases_but_keeps_commits():
    calls = []
    out = run_item(
        item=item(),
        profile=prof(),
        executors=executors(
            {
                "transcribe": ok({"result_hash": "h1"}),
                "align": PhaseOutcome(status="failed", commit_ref=None,
                                      reason_code="modell_fehlt", warnings=[]),
            },
            calls,
        ),
    )
    assert calls == ["transcribe", "align"]
    assert out["phases"]["diarize"]["status"] == "blocked"
    assert out["phases"]["export"]["status"] == "blocked"
    assert out["commits"]["transcribe"] == {"result_hash": "h1"}  # gesichert bleibt gesichert
    assert out["end_state"] == "failed"


def test_valid_partial_follows_confirmed_policy():
    partial = PhaseOutcome(status="partial", commit_ref={"result_hash": "h2p"},
                           reason_code="teilweise_ausgerichtet", warnings=["partially_aligned"])
    cont = run_item(
        item=item(),
        profile=prof(partial_failure_policy="mit_belegten_daten_fortfahren"),
        executors=executors({"transcribe": ok({"result_hash": "h1"}), "align": partial}),
    )
    assert cont["commits"]["align"] == {"result_hash": "h2p"}
    assert cont["phases"]["diarize"]["status"] in ("succeeded", "active", "requested")
    assert cont["end_state"] == "succeeded_with_warnings"

    stop = run_item(
        item=item(),
        profile=prof(partial_failure_policy="element_blockieren"),
        executors=executors({"transcribe": ok({"result_hash": "h1"}), "align": partial}),
    )
    assert stop["phases"]["diarize"]["status"] == "blocked"
    assert stop["commits"]["align"] == {"result_hash": "h2p"}  # Teilergebnis bleibt erhalten
    assert stop["end_state"] == "failed"


def test_cancel_between_phases_stops_at_safe_point_and_keeps_commits():
    state = {"n": 0}

    def align():
        state["n"] += 1
        return ok({"result_hash": "h2"})

    out = run_item(
        item=item(),
        profile=prof(),
        executors=executors({"transcribe": ok({"result_hash": "h1"}), "align": align}),
        cancel_requested=lambda: state["n"] >= 1,
    )
    assert out["commits"]["transcribe"] == {"result_hash": "h1"}
    assert out["commits"]["align"] == {"result_hash": "h2"}
    assert out["phases"]["diarize"]["status"] == "canceled"
    assert out["end_state"] == "canceled"


def test_executor_without_commit_ref_cannot_report_success():
    out = run_item(
        item=item(),
        profile=prof(export_enabled=False, alignment_enabled=False, diarization_enabled=False),
        executors=executors({"transcribe": PhaseOutcome(status="succeeded", commit_ref=None,
                                                        reason_code=None, warnings=[])}),
    )
    assert out["end_state"] == "failed"
    assert out["phases"]["transcribe"]["reason_code"] == "kein_autoritativer_commit"


def test_optional_minutes_profile_adds_minutes_phase():
    calls = []
    run_item(
        item=item(),
        profile=prof(minutes_enabled=True),
        executors=executors(
            {p: ok({"result_hash": f"h_{p}"}) for p in
             ("transcribe", "align", "diarize", "export", "minutes")},
            calls,
        ),
    )
    assert calls[-1] == "minutes"
