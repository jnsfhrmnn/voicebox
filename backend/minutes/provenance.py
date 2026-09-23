"""JFW-13: Protokoll-Identitaet, Schluessel und kanonische Serialisierung (I/O-frei).

* ``minutes_key``: deterministischer Schluessel ueber Vertragsversion,
  Job-/Asset-/Transkriptidentitaet und -Hashes, die gebundenen JFW-2-/JFW-3-/
  JFW-4-/(optionalen) JFW-11-Ergebnis-Hashes sowie die bestaetigte
  Registerrevision und das Profil. Bewusst OHNE Zielordner/Dateisystemzeit:
  identische Eingangs- UND Registerrevision ergibt byteidentische Inhalte
  (Byte-Regel, AC 88) und genau ein autoritatives Ergebnis je Schluessel.
* ``payload_hash``: bindet zusaetzlich die Zielidentitaet (Auftragsbindung) —
  Idempotenz ``existing`` bei Gleichheit, fail-closed ``conflict`` sonst.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

CONTRACT_VERSION = "jfw13_minutes_v1"
DEFAULT_PROFILE = "protokoll_aufgaben_zusammenfassung_transkript_v1"


def canonical_json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(doc: dict) -> bytes:
    return (canonical_json(doc) + "\n").encode("utf-8")


def canonical_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MinutesRequest:
    """Gebundener Protokollauftrag (Identitaet + Eingangsrevisionen + Register)."""

    job_id: str
    audio_asset_id: str
    audio_hash: str
    audio_duration_ms: int
    timebase: str
    transcript_run_id: str
    transcript_revision_id: str
    transcript_revision_hash: str
    transcript_text_hash: str
    jfw2_result_hash: str
    jfw2_status: str
    jfw3_result_hash: str
    jfw3_status: str
    jfw4_export_key: str
    jfw4_result_hash: str
    register_revision: str
    jfw11_commit_hash: str | None = None
    jfw11_status: str | None = None
    jfw11_expected: bool = False
    minutes_profile: str = DEFAULT_PROFILE
    target_dir: str | None = None


def content_material(request: MinutesRequest) -> dict:
    jfw11 = None
    if request.jfw11_commit_hash is not None or request.jfw11_status is not None:
        jfw11 = {
            "commit_hash": request.jfw11_commit_hash,
            "status": request.jfw11_status,
        }
    return {
        "contract_version": CONTRACT_VERSION,
        "job": {
            "job_id": request.job_id,
            "audio_asset_id": request.audio_asset_id,
            "audio_hash": request.audio_hash,
            "audio_duration_ms": int(request.audio_duration_ms),
            "timebase": request.timebase,
        },
        "transcript": {
            "run_id": request.transcript_run_id,
            "revision_id": request.transcript_revision_id,
            "revision_hash": request.transcript_revision_hash,
            "text_hash": request.transcript_text_hash,
        },
        "revisions": {
            "jfw2": {"result_hash": request.jfw2_result_hash, "status": request.jfw2_status},
            "jfw3": {"result_hash": request.jfw3_result_hash, "status": request.jfw3_status},
            "jfw4": {"export_key": request.jfw4_export_key,
                     "result_hash": request.jfw4_result_hash},
            "jfw11": jfw11,
        },
        "register_revision": request.register_revision,
        "minutes_profile": request.minutes_profile,
    }


def minutes_key(request: MinutesRequest) -> str:
    return canonical_hash(content_material(request))


def payload_hash(request: MinutesRequest) -> str:
    material = content_material(request)
    material["target"] = {"target_dir": request.target_dir}
    return canonical_hash(material)
