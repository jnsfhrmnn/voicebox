"""JFW-7: STT-Ausfuehrung (engine) — fail-closed an den Vertragskern.

``services/transcribe.py``-Seite / JFW-10-Runstart rufen ``transcribe_file`` mit
dem eingefrorenen Run-Snapshot auf. Vertrag:

* **Kein Netzwerkzugriff, kein Download**: fehlt oder bricht ein Modellartefakt,
  wirft die Engine ``ArtefakteFehlen`` (``require_model``). Installation bleibt
  ein bewusster, getrennter Akt ueber das Modell-/Lizenz-Gate
  (``transcription/artifacts.py``).
* **Genau ein unveraendertes Rohtranskript** (JFW-9-Verbot): KEIN Text-LLM,
  KEIN Refinement, KEIN Translate — der Provider-Rohoutput wird ohne jede
  Nachbearbeitung durchgereicht (Text, Segmente, Sprachprovenienz) und nur mit
  dem kanonischen Hash gebunden.
* **Decode fest**: ``jfw7-transcribe-greedy-v1`` mit ``task="transcribe"``;
  ``translate`` ist unerreichbar (``assert_task_transcribe``).
* **Kein stiller Fallback**: eine Provider-Ausnahme wird als ``RunnerFehler``
  sichtbar; die Services liefern dann ``failed`` OHNE Ergebnis (Audio und
  Diagnose bleiben, keine Rekonstruktion aus Teilen).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .artifacts import check_model_manifest
from .language import assert_task_transcribe
from .raw_transcript import text_hash
from .snapshot import frozen_decode, validate_snapshot

MANIFEST_NAME = "installationsmanifest.json"


class ArtefakteError(RuntimeError):
    """Modellartefakte fehlen oder sind nicht verifizierbar — kein Download."""


class RunnerError(RuntimeError):
    """Provider-Ausnahme — kein Ergebnis, kein stiller Fallback."""


#: Kompatibilitaetsaliase der Erstbenennung (Tests/Services).
ArtefakteFehlen = ArtefakteError
RunnerFehler = RunnerError


def require_model(model_dir: str) -> dict:
    """Verifiziert Manifest, Herkunft, Lizenz und Datei-Hashes (fail-closed)."""
    d = Path(model_dir)
    manifest_path = d / MANIFEST_NAME
    if not manifest_path.is_file():
        raise ArtefakteFehlen("installationsmanifest_fehlt")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtefakteFehlen("installationsmanifest_unlesbar") from exc
    errors = check_model_manifest(manifest)
    if errors:
        raise ArtefakteFehlen(",".join(errors))
    for rel, expected in (manifest.get("files") or {}).items():
        p = d / rel
        if not p.is_file():
            raise ArtefakteFehlen(f"artefakt_fehlt:{rel}")
        actual = hashlib.sha256(p.read_bytes()).hexdigest()
        if actual != expected:
            raise ArtefakteFehlen(f"artefakt_hash_falsch:{rel}")
    return dict(manifest)


def _transformers_runner(model_dir: str, audio_path: str, decode: dict) -> dict:
    """Duennster Transformers-Whisper-Pfad; Ergebnis bleibt Rohoutput."""
    try:
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
    except ImportError as exc:
        raise RunnerFehler("provider_runtime_missing") from exc
    processor = WhisperProcessor.from_pretrained(model_dir)
    model = WhisperForConditionalGeneration.from_pretrained(model_dir)
    model.eval()

    import soundfile as sf

    audio, rate = sf.read(audio_path)
    if rate != 16000:
        raise RunnerFehler("audio_samplerate_unerwartet")
    inputs = processor(
        audio, sampling_rate=16000, return_tensors="pt"
    )
    language = decode.get("language")
    gen_kwargs = {
        "task": "transcribe",
        "do_sample": False,
        "num_beams": 1,
    }
    if language:
        gen_kwargs["language"] = language
    with torch.no_grad():
        ids = model.generate(inputs.input_features, **gen_kwargs)
    text = processor.batch_decode(ids, skip_special_tokens=True)[0]
    # Rohoutput — keine Nachbearbeitung (JFW-9-Verbot).
    return {
        "text": text,
        "segments": [],
        "language": None,
    }


def transcribe_file(
    model_dir: str,
    audio_path: str,
    snapshot: dict,
    *,
    runner=None,
) -> dict:
    """Fuehrt GENAU EINE Transkription aus und reicht das Rohergebnis unveraendert durch."""
    snap_errors = validate_snapshot(snapshot)
    if snap_errors:
        raise ArtefakteFehlen("snapshot_ungueltig:" + ",".join(snap_errors))
    require_model(model_dir)  # fail-closed; hier endet jeder fehlende Artefakt-Pfad
    decode = frozen_decode(snapshot.get("language_setting") or "auto")
    assert_task_transcribe(decode)  # translate unerreichbar
    run = runner or _transformers_runner
    try:
        raw = run(model_dir, audio_path, decode)
    except RunnerFehler:
        raise
    except Exception as exc:  # Provider-Ausnahme bleibt sichtbar
        raise RunnerFehler("provider_error") from exc
    text = (raw or {}).get("text") or ""
    segments = list((raw or {}).get("segments") or [])
    language = (raw or {}).get("language")
    # JFW-9-Verbot: KEIN LLM, KEIN Refinement, KEINE Umformung.
    return {
        "text": text,
        "segments": segments,
        "language": language,
        "text_hash": text_hash(text),
        "decode_profile": decode["profile_id"],
    }
