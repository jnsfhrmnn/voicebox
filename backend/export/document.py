"""JFW-4: verlustfreies JSON ``jfw4_export_v1`` und Set-Aufbau (I/O-frei).

Kanonische Serialisierung: ``json.dumps(sort_keys=True, separators=(",", ":"),
ensure_ascii=False)`` in UTF-8/LF mit genau einem abschliessenden Zeilenumbruch
— byteidentisch bei Snapshot-Gleichheit (Abnahme-Gate). ``result_hash`` bindet
sae mtliche Dokumentinhalte (ueber das Dokument ohne ``result_hash``); das
Set-Manifest bindet die tatsaechlichen Inhalts-Hashes der Praesentationsdateien.
"""
from __future__ import annotations

import hashlib
import json

from .contract import validate_words
from .provenance import (
    CONTRACT_VERSION,
    canonical_hash,
    expected_file_names,
    export_key,
    normalize_formats,
)
from .subtitles import PARTIAL_MARKERS, build_cues, serialize_srt, serialize_vtt

DOC_KEYS = (
    "contract_version", "export_key", "result_hash", "job", "transcript", "revisions",
    "options", "readiness", "speakers", "turns", "words", "source_representations",
    "dedupe_decisions", "name_mappings", "overlap_regions", "source_gaps", "cues",
    "presentation", "not_exportable_ranges",
)


def canonical_json(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_bytes(doc: dict) -> bytes:
    return (canonical_json(doc) + "\n").encode("utf-8")


def _cue_json(cue) -> dict:
    return {
        "index": cue.index,
        "start_ms": cue.start_ms,
        "end_ms": cue.end_ms,
        "time_quality": cue.time_quality,
        "word_ids": list(cue.word_ids),
        "cluster_id": cue.cluster_id,
        "label": cue.label,
        "labels": list(cue.labels),
    }


def build_set(snapshot, request) -> dict:
    """Erzeugt Dokument + alle Set-Dateien vollstaendig im RAM."""
    key = export_key(request)
    formats = normalize_formats(request.formats)
    names = expected_file_names(request)
    name_by_role = dict(zip(formats, names, strict=False))

    cues, not_exportable = build_cues(snapshot)
    partial_marker = PARTIAL_MARKERS.get(request.partial_mode or "")

    files: dict[str, bytes] = {}
    presentation: list[dict] = []
    format_status: dict[str, str] = {"json": "ok"}
    for role in ("srt", "vtt"):
        if role not in formats:
            continue
        if not_exportable:
            format_status[role] = "blockiert"
            continue
        text = (
            serialize_srt(cues, partial_marker)
            if role == "srt"
            else serialize_vtt(cues, partial_marker)
        )
        data = text.encode("utf-8")
        name = name_by_role[role]
        files[name] = data
        presentation.append({
            "role": role,
            "file_name": name,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
        format_status[role] = "ok"

    assignments = {str(a.get("word_id")): a for a in snapshot.word_assignments}
    jfw11 = snapshot.jfw11 or None
    words = []
    for order, word in enumerate(snapshot.words):
        word_id = str(word.get("word_id"))
        assignment = assignments.get(word_id) or {}
        words.append({
            "word_id": word_id,
            "order": order,
            "text": word.get("text"),
            "char_start": word.get("char_start"),
            "char_end": word.get("char_end"),
            "timing_status": word.get("timing_status"),
            "start_ms": word.get("start_ms"),
            "end_ms": word.get("end_ms"),
            "reason_code": word.get("reason_code"),
            "speaker_status": assignment.get("speaker_status"),
            "cluster_id": assignment.get("cluster_id"),
            "turn_id": assignment.get("turn_id"),
            "source": "jfw2",
        })

    doc = {
        "contract_version": CONTRACT_VERSION,
        "export_key": key,
        "result_hash": None,
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
            "text": snapshot.text,
        },
        "revisions": {
            "jfw2": {"result_hash": request.jfw2_result_hash, "status": request.jfw2_status},
            "jfw3": {"result_hash": request.jfw3_result_hash, "status": request.jfw3_status},
            "jfw11": (
                {
                    "commit_hash": request.jfw11_commit_hash,
                    "status": request.jfw11_status,
                    "sync_status": jfw11.get("sync_status"),
                    "dedupe_status": jfw11.get("dedupe_status"),
                    "name_mapping_revision": jfw11.get("name_mapping_revision"),
                }
                if jfw11 is not None
                else None
            ),
        },
        "options": {
            "formats": list(formats),
            "export_profile": request.export_profile,
            "name_policy": request.name_policy,
            "partial_mode": request.partial_mode,
        },
        "readiness": snapshot.readiness,
        "speakers": [dict(d) for d in snapshot.speakers_display],
        "turns": [dict(t) for t in snapshot.turns],
        "words": words,
        "source_representations": [dict(s) for s in (jfw11 or {}).get("source_representations", [])],
        "dedupe_decisions": [dict(d) for d in (jfw11 or {}).get("dedupe_decisions", [])],
        "name_mappings": [dict(m) for m in (jfw11 or {}).get("name_mappings", [])],
        "overlap_regions": [dict(o) for o in (jfw11 or {}).get("overlap_regions", [])],
        "source_gaps": [dict(g) for g in (jfw11 or {}).get("source_gaps", [])],
        "cues": [_cue_json(c) for c in cues],
        "presentation": presentation,
        "not_exportable_ranges": not_exportable,
    }
    assert set(doc.keys()) == set(DOC_KEYS)
    without = {k: v for k, v in doc.items() if k != "result_hash"}
    doc["result_hash"] = canonical_hash(without)
    if "json" in formats:
        files[name_by_role["json"]] = canonical_bytes(doc)
    return {
        "document": doc,
        "files": files,
        "format_status": format_status,
        "cues": [_cue_json(c) for c in cues],
        "not_exportable_ranges": not_exportable,
    }


def verify_lossless(doc: dict, sources: dict) -> list[str]:
    """Quellenrekonstruktion: 100 % der Quellenwörter/-Turns/-Zustände müssen
    rekonstruierbar sein; jede Mutation des gesprochenen Inhalts ist ein Fehler."""
    errors: list[str] = []
    source_words = sources.get("jfw2", {}).get("words", [])
    doc_words = doc.get("words", [])
    if len(doc_words) != len(source_words):
        errors.append("wortanzahl_veraendert")
    for source_word, doc_word in zip(source_words, doc_words, strict=False):
        for field in ("word_id", "text", "char_start", "char_end", "timing_status",
                      "start_ms", "end_ms", "reason_code"):
            if source_word.get(field) != doc_word.get(field):
                errors.append(f"wort_veraendert:{source_word.get('word_id')}:{field}")
    source_turns = sources.get("jfw3", {}).get("turns", [])
    if [dict(t) for t in source_turns] != doc.get("turns"):
        errors.append("turns_veraendert")
    jfw11 = sources.get("jfw11")
    if jfw11 is not None:
        if [dict(m) for m in jfw11.get("name_mappings", [])] != doc.get("name_mappings"):
            errors.append("name_mappings_veraendert")
        if [dict(d) for d in jfw11.get("dedupe_decisions", [])] != doc.get("dedupe_decisions"):
            errors.append("dedupe_veraendert")
        if [dict(s) for s in jfw11.get("source_representations", [])] != doc.get(
            "source_representations"
        ):
            errors.append("quellenrepraesentation_veraendert")
    # Keine erfundene Praezision zulaessig.
    errors.extend(
        e for e in validate_words(source_words, doc.get("job", {}).get("audio_duration_ms", 0))
    )
    return errors
