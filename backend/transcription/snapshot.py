"""JFW-7: Run-Snapshot — friert die Vertragsparameter pro Run ein (I/O-frei).

Spec AC „Eingabe, Snapshot und Backendbindung": Beim Annehmen friert der Run
Audio-/Manifest-Hash, STT-Modell, immutable Modellrevision, Spracheinstellung,
Decode-Konfiguration, JFW-12-Backendgeneration und Ergebnisvertragsversion ein.
"""
from __future__ import annotations

import hashlib
import json
import re

from .language import LANGUAGE_SETTINGS, assert_task_transcribe

#: Ergebnisvertrag der Rohtranskription (backendunabhaengig, CPU wie CUDA).
RESULT_CONTRACT_VERSION = "dictation_raw_v1"
#: Eingefrorenes Modellprofil (CPU-Standard = CUDA-Profil, ein Ergebnisvertrag).
MODEL_PROFILE_ID = "jfw7-dictate-v1"
#: Eingefrorenes Decode-Profil: deterministisch, ``task=transcribe``.
DECODE_PROFILE_ID = "jfw7-transcribe-greedy-v1"
#: JFW-12-Ressourcenvarianten.
BACKEND_VARIANTS = ("cpu", "cuda")

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_HEX40 = re.compile(r"^[0-9a-f]{40}$")


def frozen_decode(language_setting: str) -> dict:
    """Das eingefrorene Decode-Profil fuer eine Spracheinstellung."""
    return {
        "profile_id": DECODE_PROFILE_ID,
        "task": "transcribe",
        "do_sample": False,
        "num_beams": 1,
        "return_timestamps": True,
        "language": language_setting,
    }


def build_snapshot(
    *,
    audio_hash: str,
    manifest_hash: str | None,
    stt_model: str,
    model_revision: str,
    language_setting: str,
    backend_variant: str,
    backend_generation: int,
) -> dict:
    return {
        "contract_version": RESULT_CONTRACT_VERSION,
        "model_profile": MODEL_PROFILE_ID,
        "audio_hash": audio_hash,
        "manifest_hash": manifest_hash,
        "stt_model": stt_model,
        "model_revision": model_revision,
        "language_setting": language_setting,
        "decode": frozen_decode(language_setting),
        "backend_variant": backend_variant,
        "backend_generation": int(backend_generation),
    }


def snapshot_hash(snapshot: dict) -> str:
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_snapshot(snapshot: dict) -> list[str]:
    """Fail-closed Validierung; leere Liste = Snapshot vollstaendig."""
    errors: list[str] = []
    if snapshot.get("contract_version") != RESULT_CONTRACT_VERSION:
        errors.append("vertragsversion_unbekannt")
    if not isinstance(snapshot.get("audio_hash"), str) or not _HEX64.match(snapshot["audio_hash"]):
        errors.append("audio_hash_unzulaessig")
    mh = snapshot.get("manifest_hash")
    if mh is not None and (not isinstance(mh, str) or not _HEX64.match(mh)):
        errors.append("manifest_hash_unzulaessig")
    mr = snapshot.get("model_revision")
    if not isinstance(mr, str) or not _HEX40.match(mr):
        errors.append("modellrevision_unzulaessig")
    if snapshot.get("language_setting") not in LANGUAGE_SETTINGS:
        errors.append("spracheinstellung_unbekannt")
    if snapshot.get("backend_variant") not in BACKEND_VARIANTS:
        errors.append("backendvariante_unbekannt")
    try:
        if int(snapshot.get("backend_generation", 0)) < 1:
            errors.append("backendgeneration_unzulaessig")
    except (TypeError, ValueError):
        errors.append("backendgeneration_unzulaessig")
    decode = snapshot.get("decode") or {}
    if decode.get("profile_id") != DECODE_PROFILE_ID:
        errors.append("decode_profil_unbekannt")
    try:
        assert_task_transcribe(decode)
    except Exception:
        errors.append("decode_nicht_transcribe")
    if decode.get("language") != snapshot.get("language_setting"):
        errors.append("decode_passnicht_zur_sprache")
    return errors
