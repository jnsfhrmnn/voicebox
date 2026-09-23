"""JFW-7: Run-Snapshot (Einfrieren der Vertragsparameter) — Vertragstests (TDD, RED zuerst).

Spec AC „Eingabe, Snapshot und Backendbindung": Der Run friert Audio-/Manifest-Hash,
STT-Modell, immutable Modellrevision, Spracheinstellung, Decode-Konfiguration,
JFW-12-Backendgeneration und Ergebnisvertragsversion ein.

Ausfuehrung:  backend/.venv/Scripts/python.exe -m pytest backend/tests/test_transcription_snapshot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


from backend.transcription.snapshot import (
    BACKEND_VARIANTS,
    DECODE_PROFILE_ID,
    LANGUAGE_SETTINGS,
    MODEL_PROFILE_ID,
    RESULT_CONTRACT_VERSION,
    build_snapshot,
    frozen_decode,
    snapshot_hash,
    validate_snapshot,
)


def _snapshot(**overrides) -> dict:
    base = dict(
        audio_hash="a" * 64,
        manifest_hash="b" * 64,
        stt_model="openai/whisper-large-v3-turbo",
        model_revision="1" * 40,
        language_setting="auto",
        backend_variant="cpu",
        backend_generation=4,
    )
    base.update(overrides)
    return build_snapshot(**base)


def test_snapshot_friert_alle_pflichtparameter_ein():
    s = _snapshot()
    assert s["contract_version"] == RESULT_CONTRACT_VERSION
    assert s["model_profile"] == MODEL_PROFILE_ID
    assert s["audio_hash"] == "a" * 64
    assert s["manifest_hash"] == "b" * 64
    assert s["stt_model"] == "openai/whisper-large-v3-turbo"
    assert s["model_revision"] == "1" * 40
    assert s["language_setting"] == "auto"
    assert s["decode"] == frozen_decode("auto")
    assert s["backend_variant"] == "cpu"
    assert s["backend_generation"] == 4


def test_decode_profil_ist_transcribe_und_deterministisch():
    for setting in LANGUAGE_SETTINGS:
        d = frozen_decode(setting)
        assert d["profile_id"] == DECODE_PROFILE_ID
        assert d["task"] == "transcribe"
        assert d["do_sample"] is False
        assert d["num_beams"] == 1
        assert d["return_timestamps"] is True
        assert d["language"] == setting


def test_snapshot_hash_ist_stabil_und_payloadempfindlich():
    s1 = _snapshot()
    s2 = _snapshot()
    assert snapshot_hash(s1) == snapshot_hash(s2)
    assert len(snapshot_hash(s1)) == 64
    assert snapshot_hash(_snapshot(backend_generation=5)) != snapshot_hash(s1)
    assert snapshot_hash(_snapshot(model_revision="2" * 40)) != snapshot_hash(s1)


def test_validate_gueltiger_snapshot_ist_fehlerfrei():
    assert validate_snapshot(_snapshot()) == []


def test_validate_lehnt_falsche_hashes_und_revisionen():
    assert "audio_hash_unzulaessig" in validate_snapshot(_snapshot(audio_hash="xyz"))
    assert "manifest_hash_unzulaessig" in validate_snapshot(_snapshot(manifest_hash="k"))
    assert "modellrevision_unzulaessig" in validate_snapshot(_snapshot(model_revision="123"))


def test_validate_lehnt_unbekannte_sprache_und_backend():
    assert "spracheinstellung_unbekannt" in validate_snapshot(_snapshot(language_setting="fr"))
    assert "backendvariante_unbekannt" in validate_snapshot(_snapshot(backend_variant="rocm"))
    assert "backendgeneration_unzulaessig" in validate_snapshot(_snapshot(backend_generation=0))


def test_validate_erzwingt_transcribe_decode():
    s = _snapshot()
    s["decode"] = dict(s["decode"], task="translate")
    assert "decode_nicht_transcribe" in validate_snapshot(s)
    s2 = _snapshot()
    s2["decode"] = {"profile_id": "anders", "task": "transcribe"}
    assert "decode_profil_unbekannt" in validate_snapshot(s2)


def test_validate_lehnt_unbekannte_vertragsversion():
    s = _snapshot()
    s["contract_version"] = "dictation_raw_v99"
    assert "vertragsversion_unbekannt" in validate_snapshot(s)


def test_backendvarianten_sind_cpu_cuda():
    assert set(BACKEND_VARIANTS) == {"cpu", "cuda"}
