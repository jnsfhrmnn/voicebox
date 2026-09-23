"""JFW-13: Engine-Orchestrierung und JFW-9-Riegel — Vertragstests (TDD).

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_minutes_engine.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.minutes.artifacts import ArtifactGateError
from backend.minutes.engine import (
    DerivationError,
    chunk_turns,
    generate_sections,
    verify_derivation_only,
)
from backend.minutes.input import bind_input
from backend.minutes.provenance import MinutesRequest
from backend.minutes.providers.local_llm import LocalLLMProvider
from backend.minutes.pseudonym import confirm_register, propose_register
from backend.tests.jfw13_sources import (
    TURN_ORDER,
    candidates_names,
    names_doc,
    req13_kwargs,
    summary_proposals,
    tasks_proposals,
)


class FakeProviders:
    def detect_candidates(self, text):
        return candidates_names()

    def propose_tasks(self, text, turns):
        return tasks_proposals()

    def propose_summary(self, text, turns):
        return summary_proposals()

    def propose_datum(self, text):
        return None


def setup_input():
    doc = names_doc()
    request = MinutesRequest(**req13_kwargs(doc))
    inp = bind_input(request, doc)
    register = confirm_register(
        propose_register(candidates_names(), TURN_ORDER, register_id="reg"))
    return request, inp, register


def test_generate_sections_with_injected_providers():
    _request, inp, register = setup_input()
    sections = generate_sections(inp, register, FakeProviders())
    assert sections["tasks"]["tasks"]
    assert sections["summary"]["lang"]
    assert len(sections["transkript"]["entries"]) == 3
    assert sections["datum"] is None


def test_generation_requires_confirmed_register():
    from backend.minutes.pseudonym import propose_register as propose
    _request, inp, _register = setup_input()
    unconfirmed = propose(candidates_names(), TURN_ORDER, register_id="reg")
    with pytest.raises(ValueError, match="register_unbestaetigt"):
        generate_sections(inp, unconfirmed, FakeProviders())


def test_derivation_only_guard_passes_on_identical_hashes():
    verify_derivation_only("a" * 64, "a" * 64)


def test_derivation_only_guard_blocks_snapshot_change():
    with pytest.raises(DerivationError) as exc:
        verify_derivation_only("a" * 64, "b" * 64)
    assert exc.value.reason_code == "rohdaten_veraendert"


def test_chunking_limits_summary_depth_never_transcript():
    turns = [{"turn_id": f"t{i}", "text": "x" * 100} for i in range(300)]
    chunks = chunk_turns(turns, max_chars=1000)
    assert len(chunks) > 1
    seen = [t["turn_id"] for chunk in chunks for t in chunk]
    assert seen == [t["turn_id"] for t in turns]  # nie kuerzen, nie verlieren


def test_chunking_single_chunk_for_small_input():
    turns = [{"turn_id": "t1", "text": "kurz"}]
    assert chunk_turns(turns, max_chars=1000) == [turns]


def test_local_provider_without_artifact_fails_closed(tmp_path):
    provider = LocalLLMProvider(artifact_dir=tmp_path)
    with pytest.raises(ArtifactGateError) as exc:
        provider.load()
    assert exc.value.reason_code == "waiting_for_local_artifact"
