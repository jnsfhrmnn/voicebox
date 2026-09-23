"""JFW-5: Retry- und Recovery-Vertrag — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_retry_recovery.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.recovery import mark_interrupted, resume_plan
from backend.batch.retry import plan_retry
from backend.tests.test_batch_profile import prof, profile_hash


def state(phases, statuses):
    return {
        "phases": dict(zip(phases, statuses, strict=False)),
        "commits": {
            p: {"result_hash": f"h_{p}"}
            for p, s in zip(phases, statuses, strict=False)
            if s in ("succeeded", "partial")
        },
    }


def test_retry_reuses_valid_commits_and_reruns_only_broken_phases():
    plan = plan_retry(
        item_state=state(("transcribe", "align", "diarize", "export"),
                         ("succeeded", "succeeded", "failed", "blocked")),
        profile=prof(),
        snapshot_profile_hash=profile_hash(prof()),
        source_ok=True,
    )
    assert plan.allowed
    assert plan.reused_phases == ("transcribe", "align")
    assert plan.rerun_phases == ("diarize", "export")


def test_retry_requires_new_revision_on_profile_change():
    plan = plan_retry(
        item_state=state(("transcribe",), ("failed",)),
        profile=prof(stt_model="small"),
        snapshot_profile_hash=profile_hash(prof()),
        source_ok=True,
    )
    assert not plan.allowed
    assert plan.reason_code == "profil_geaendert_neue_revision_notwendig"


def test_retry_after_source_change_is_refused():
    plan = plan_retry(
        item_state=state(("transcribe",), ("invalidated",)),
        profile=prof(),
        snapshot_profile_hash=profile_hash(prof()),
        source_ok=False,
    )
    assert not plan.allowed
    assert plan.reason_code == "quelle_veraendert"


def test_interrupted_marks_active_attempts_without_claiming_success():
    attempts = [
        {"attempt_id": "a1", "status": "active", "app_epoch": "old"},
        {"attempt_id": "a2", "status": "succeeded", "app_epoch": "old"},
    ]
    out = mark_interrupted(attempts, current_epoch="new")
    by_id = {a["attempt_id"]: a for a in out}
    assert by_id["a1"]["status"] == "interrupted"
    assert by_id["a2"]["status"] == "succeeded"  # gesichertes Ergebnis bleibt
    assert out[0]["resumable"] is True


def test_resume_needs_explicit_confirmation_and_restorable_contract():
    snapshot = {
        "contract_version": "jfw5_batch_v1",
        "profile": prof(),
        "profile_hash": profile_hash(prof()),
    }
    assert resume_plan(snapshot, confirmed_original=False)["ok"] is False
    assert resume_plan(snapshot, confirmed_original=True)["ok"] is True
    broken = dict(snapshot)
    broken["profile"] = dict(prof())
    del broken["profile"]["language_setting"]
    out = resume_plan(broken, confirmed_original=True)
    assert out["ok"] is False
    assert out["reason_code"] == "vertrag_nicht_wiederherstellbar"


def test_resume_never_reconstructs_from_current_defaults():
    snapshot = {"contract_version": "jfw5_batch_v1", "profile": prof(), "profile_hash": profile_hash(prof())}
    restored = resume_plan(snapshot, confirmed_original=True)["profile"]
    assert restored == prof()  # ausschliesslich aus dem Snapshot, nie aus Defaults
