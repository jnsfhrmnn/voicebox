"""JFW-4: Export-Identitaet, Export-Schluessel und Payload-Bindung (I/O-frei).

* ``export_key``: deterministischer Export-Schluessel ueber Vertragsversion,
  Job-/Asset-/Transkriptidentitaet und -Hashes, die gebundenen JFW-2-/JFW-3-/
  (optionalen) JFW-11-Ergebnis-Hashes und die INHALTSWIRKSAMEN Optionen.
  Bewusst OHNE Zielordner/Dateisystemzeit: identischer Snapshot + Optionen
  ergeben byteidentische Inhalte unabhaengig vom Dateiziel (Abnahme-Gate).
* ``payload_hash``: bindet zusaetzlich Zielidentitaet und erwartete Dateinamen
  (Auftragsbindung) — Idempotenz ``existing`` bei Gleichheit, fail-closed
  ``conflict`` bei Abweichung.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

CONTRACT_VERSION = "jfw4_export_v1"
JSON_FORMAT = "json"
PRESENTATION_FORMATS = ("srt", "vtt")
FORMAT_ORDER = (JSON_FORMAT, "srt", "vtt")

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def canonical_hash(obj) -> str:
    canon = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def transcript_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExportRequest:
    """Gebundener Exportauftrag (Identitaet + Optionen + Ziel) einer Zeile."""

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
    jfw11_commit_hash: str | None = None
    jfw11_status: str | None = None
    jfw11_expected: bool = False
    formats: tuple[str, ...] = (JSON_FORMAT,)
    export_profile: str = "lesbare_untertitel_v1"
    name_policy: str = "neutral"
    partial_mode: str | None = None
    partial_confirmed: bool = False
    target_dir: str | None = None


def normalize_formats(formats) -> tuple[str, ...]:
    """Erzwingt JSON bei Praesentationsformaten und eine feste Rollenordnung."""
    wanted = {str(f).lower() for f in formats}
    if wanted & set(PRESENTATION_FORMATS):
        wanted.add(JSON_FORMAT)
    return tuple(role for role in FORMAT_ORDER if role in wanted)


def file_token(job_id: str) -> str:
    """Windows-sicherer, deterministischer Dateinamen-Anteil aus der Job-ID."""
    token = _INVALID_NAME_CHARS.sub("_", job_id).rstrip(". ")
    if not token:
        token = "export"
    base = token.split(".")[0].upper()
    if base in _RESERVED_NAMES:
        token = "_" + token
    return token[:100]


def content_material(request: ExportRequest) -> dict:
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
            "jfw11": jfw11,
        },
        "options": {
            "formats": list(normalize_formats(request.formats)),
            "export_profile": request.export_profile,
            "name_policy": request.name_policy,
            "partial_mode": request.partial_mode,
        },
    }


def export_key(request: ExportRequest) -> str:
    return canonical_hash(content_material(request))


def expected_file_names(request: ExportRequest) -> tuple[str, ...]:
    """Kanonische, zielunabhaengige Dateinamen: ``<job_id>-<key12>.<ext>``."""
    key = export_key(request)
    token = file_token(request.job_id)
    return tuple(f"{token}-{key[:12]}.{role}" for role in normalize_formats(request.formats))


def order_material(request: ExportRequest) -> dict:
    return {
        "content": content_material(request),
        "target_dir": request.target_dir,
        "expected_files": list(expected_file_names(request)),
    }


def payload_hash(request: ExportRequest) -> str:
    return canonical_hash(order_material(request))
