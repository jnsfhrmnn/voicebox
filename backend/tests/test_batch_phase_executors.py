"""JFW-5: fail-closed Phasen-Binding, Artefakt-/Runtime-Mapping, transienter Text — TDD.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_batch_phase_executors.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.batch.pipeline import run_item
from backend.batch.providers.phase_executors import (
    ProviderRuntimeMissingError,
    WaitingForLocalArtifactError,
    default_executors,
)
from backend.tests.test_batch_profile import prof

PHASES = ("transcribe", "align", "diarize", "export", "minutes")


def execs(hooks):
    return default_executors(session=object(), hooks=hooks)


def all_hooks(fn):
    return {p: fn for p in PHASES}


def ctx(**over):
    base = {"item": {"item_id": "jfw5-item-0123456789abcdef",
                     "source": {"path": "C:/data/clip.wav"}},
            "profile": {}, "commits": {}, "carry": {}}
    base.update(over)
    return base


def test_missing_upstream_commit_blocks_downstream_phase_fail_closed():
    def ok_hook(c):
        return {"commit_ref": {"result_hash": "h"}, "status": "succeeded"}

    ex = execs(all_hooks(ok_hook))
    out = ex["align"]("align", ctx())
    assert out.status == "blocked"
    assert out.reason_code == "upstream_fehlt:transcribe"


def test_missing_local_artifact_blocks_without_download_or_fallback():
    def hook(c):
        raise WaitingForLocalArtifactError("modell_fehlt:stt")

    ex = execs(all_hooks(hook))
    out = ex["transcribe"]("transcribe", ctx())
    assert out.status == "blocked"
    assert out.reason_code == "waiting_for_local_artifact"


def test_missing_provider_runtime_blocks_and_never_downloads():
    def hook(c):
        raise ProviderRuntimeMissingError("provider_runtime_fehlt")

    ex = execs(all_hooks(hook))
    out = ex["diarize"]("diarize", ctx(commits={"transcribe": {"result_hash": "h"}}))
    assert out.status == "blocked"
    assert out.reason_code == "provider_runtime_missing"


def test_success_without_authoritative_commit_is_not_a_success():
    def hook(c):
        return {"status": "succeeded", "commit_ref": None}

    ex = execs(all_hooks(hook))
    out = ex["transcribe"]("transcribe", ctx())
    assert out.status == "failed"
    assert out.reason_code == "kein_autoritativer_commit"


def test_text_flows_transient_and_never_lands_in_persisted_commits():
    seen = {}

    def transcribe(c):
        return {"commit_ref": {"result_hash": "h_t"}, "status": "succeeded",
                "carry": {"transcript_text": "GEHEIM"}}

    def passthrough(c):
        return {"commit_ref": {"result_hash": "h_x"}, "status": "succeeded"}

    ex = execs({**all_hooks(passthrough), "transcribe": transcribe})

    def watch(phase):
        inner = ex[phase]

        def run(phase_name, c):
            seen[phase_name] = dict(c.get("carry") or {})
            return inner(phase_name, c)
        return run

    profile = prof()
    out = run_item(
        item=ctx()["item"],
        profile=profile,
        executors={p: watch(p) for p in ("transcribe", "align", "diarize", "export")},
    )
    # Carry erreicht die Folgephase ...
    assert seen["align"]["transcript_text"] == "GEHEIM"
    # ... aber die autoritativen Commits bleiben reine Referenzen.
    assert out["commits"]["transcribe"] == {"result_hash": "h_t"}
    assert "GEHEIM" not in str(out["commits"])
