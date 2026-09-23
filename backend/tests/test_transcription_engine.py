"""JFW-7: STT-Ausfuehrung (engine) — fail-closed, Ergebnis unveraendert (TDD, RED).

Spec AC „Rohtranskript und Inhaltstreue": genau ein unveraendertes STT-Roh-
ergebnis — kein Text-LLM, kein Refinement, NIE. Die Engine reicht den
Rohoutput des Providers ohne Nachbearbeitung durch.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_engine.py
"""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from backend.transcription.engine import (
    ArtefakteFehlen,
    RunnerFehler,
    require_model,
    transcribe_file,
)
from backend.transcription.raw_transcript import text_hash
from backend.transcription.snapshot import build_snapshot

MODEL_REPO = "openai/whisper-large-v3-turbo"


def _snapshot():
    return build_snapshot(
        stt_model=MODEL_REPO,
        model_revision="1" * 40,
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        language_setting="auto",
        backend_variant="cpu",
        backend_generation=1,
    )


def _modellverzeichnis(tmp_path) -> Path:
    """Legt ein gueltiges Modellverzeichnis mit Installationsmanifest an."""
    model_dir = tmp_path / "jfw7-dictate-v1"
    model_dir.mkdir()
    files = {}
    for name, blob in (("model.safetensors", b"weights"), ("config.json", b"{}")):
        (model_dir / name).write_bytes(blob)
        files[name] = hashlib.sha256(blob).hexdigest()
    manifest = {
        "repo": MODEL_REPO,
        "model_revision": "1" * 40,
        "files": files,
        "license_id": "mit",
        "install_path": str(model_dir).replace("\\", "/"),
    }
    (model_dir / "installationsmanifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return model_dir


def _raw():
    return {
        "text": "你好，世界。Hello world.",  # noqa: RUF001 -- CJK-Satzzeichen ist Testgegenstand
        "segments": [{"start": 0.0, "end": 1.4}],
        "language": {
            "detected": "zh",
            "confidence": 0.88,
            "source": "model_output",
            "is_user_intent": False,
        },
    }


def test_fehlende_artefakte_sind_fail_closed_ohne_download(tmp_path):
    """Kein Modell -> ArtefakteFehlen; der Runner wird nie aufgerufen (kein Netz)."""
    calls = []

    def runner(*args, **kwargs):
        calls.append(args)
        return _raw()

    with pytest.raises(ArtefakteFehlen):
        transcribe_file(
            str(tmp_path / "fehlt"), str(tmp_path / "a.wav"), _snapshot(), runner=runner
        )
    assert calls == []  # kein stiller Inferenzpfad ohne verifizierte Artefakte


def test_require_model_prueft_hash_und_lizenz(tmp_path):
    model_dir = _modellverzeichnis(tmp_path)
    info = require_model(str(model_dir))
    assert info["license_id"] == "mit"
    # Manipulation: Datei geaendert -> Hash bricht -> fail-closed
    (model_dir / "model.safetensors").write_bytes(b"manipuliert")
    with pytest.raises(ArtefakteFehlen):
        require_model(str(model_dir))


def test_runner_erhaelt_transcribe_profil_und_rohtext_bleibt_unveraendert(tmp_path):
    model_dir = _modellverzeichnis(tmp_path)
    gesehen = {}

    def runner(model_dir_arg, audio_path, decode):
        gesehen["decode"] = decode
        return _raw()

    out = transcribe_file(
        str(model_dir), str(tmp_path / "a.wav"), _snapshot(), runner=runner
    )
    assert gesehen["decode"]["task"] == "transcribe"  # translate unerreichbar
    assert gesehen["decode"]["do_sample"] is False
    raw = _raw()
    assert out["text"] == raw["text"]  # genau ein unveraendertes Rohtranskript
    assert out["segments"] == raw["segments"]
    assert out["language"] == raw["language"]
    assert out["text_hash"] == text_hash(raw["text"])


def test_runnerausnahme_wird_kein_ergebnis(tmp_path):
    model_dir = _modellverzeichnis(tmp_path)

    def runner(*args, **kwargs):
        raise RuntimeError("cuda_oom")

    with pytest.raises(RunnerFehler):
        transcribe_file(
            str(model_dir), str(tmp_path / "a.wav"), _snapshot(), runner=runner
        )
