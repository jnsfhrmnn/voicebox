"""JFW-13: fail-closed Eingangsbindung an den JFW-4-Snapshot ``jfw4_export_v1``.

JFW-13 ist AUSSCHLIESSLICH Konsient des verlustfreien JFW-4-Dokuments. Jede
fehlende, ungueltige oder hashfremde Bindung ist fail-closed ``blocked`` mit
konkretem Grundcode — es entsteht nie ein Teilprotokoll aus ungebundenen
Quellen. Teilfehler aus JFW-2/3/11 werden als Warnliste sichtbar mitgefuehrt.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .provenance import MinutesRequest, canonical_hash

PARTIAL_WARNING_STATES = (
    "partially_aligned",
    "partially_diarized",
    "secured_partial",
    "sync_unsicher",
    "single_source",
    "dual_source_partial",
    "dedupe_unsicher",
)


@dataclass(frozen=True)
class MinutesInput:
    document: MappingProxyType
    text: str
    words: tuple
    turns: tuple
    speakers: tuple
    snapshot_hash: str
    readiness: dict
    warnings: tuple
    binding_status: str
    reason_code: str | None


def document_result_hash(document: dict) -> str | None:
    if not isinstance(document, dict) or "result_hash" not in document:
        return None
    without = {k: v for k, v in document.items() if k != "result_hash"}
    return canonical_hash(without)


def verify_snapshot_unchanged(document: dict) -> list[str]:
    """Reine Ableitung: der Snapshot muss feldweise hashidentisch bleiben."""
    errors: list[str] = []
    expected = document.get("result_hash") if isinstance(document, dict) else None
    actual = document_result_hash(document)
    if expected is None or actual is None or expected != actual:
        errors.append("eingang_hash_inkonsistent")
    return errors


def _blocked(reason_code: str) -> MinutesInput:
    return MinutesInput(
        document=MappingProxyType({}),
        text="",
        words=tuple(),
        turns=tuple(),
        speakers=tuple(),
        snapshot_hash="",
        readiness={"state": "blocked", "warnings": [], "reason_code": reason_code},
        warnings=tuple(),
        binding_status="blockiert",
        reason_code=reason_code,
    )


def _collect_warnings(document: dict) -> list[dict]:
    warnings: list[dict] = []
    revisions = document.get("revisions", {}) or {}

    def add(kind: str, quelle: str):
        warnings.append({"kind": kind, "quelle": quelle, "sichtbar": True})

    jfw2 = revisions.get("jfw2") or {}
    jfw3 = revisions.get("jfw3") or {}
    jfw11 = revisions.get("jfw11") or {}
    for state, quelle in (
        (jfw2.get("status"), "jfw2"),
        (jfw3.get("status"), "jfw3"),
        (jfw11.get("status"), "jfw11"),
        (jfw11.get("sync_status"), "jfw11"),
        (jfw11.get("dedupe_status"), "jfw11"),
    ):
        if state in PARTIAL_WARNING_STATES:
            add(state, quelle)
    return warnings


def bind_input(request: MinutesRequest, document: dict | None) -> MinutesInput:
    if not document:
        return _blocked("jfw4_snapshot_fehlt")
    if document.get("contract_version") != "jfw4_export_v1":
        return _blocked("eingang_nicht_jfw4_export")
    if verify_snapshot_unchanged(document):
        return _blocked("eingang_hash_inkonsistent")

    job = document.get("job", {}) or {}
    transcript = document.get("transcript", {}) or {}
    revisions = document.get("revisions", {}) or {}
    jfw4_checks = (
        request.job_id == job.get("job_id"),
        request.audio_hash == job.get("audio_hash"),
        int(request.audio_duration_ms) == int(job.get("audio_duration_ms") or 0),
        request.transcript_revision_id == transcript.get("revision_id"),
        request.transcript_revision_hash == transcript.get("revision_hash"),
        request.transcript_text_hash == transcript.get("text_hash"),
        request.jfw2_result_hash == (revisions.get("jfw2") or {}).get("result_hash"),
        request.jfw2_status == (revisions.get("jfw2") or {}).get("status"),
        request.jfw3_result_hash == (revisions.get("jfw3") or {}).get("result_hash"),
        request.jfw3_status == (revisions.get("jfw3") or {}).get("status"),
        request.jfw4_export_key == document.get("export_key"),
        request.jfw4_result_hash == document.get("result_hash"),
    )
    jfw11 = revisions.get("jfw11")
    if request.jfw11_expected:
        jfw11 = jfw11 or {}
        jfw11_checks = (
            request.jfw11_commit_hash == jfw11.get("commit_hash"),
            request.jfw11_status == jfw11.get("status"),
        )
    else:
        jfw11_checks = (True,)
    if not all(jfw4_checks) or not all(jfw11_checks):
        return _blocked("bindung_inkonsistent")

    warnings = _collect_warnings(document)
    state = "ready_with_warnings" if warnings else "ready"
    return MinutesInput(
        document=MappingProxyType(document),
        text=transcript.get("text", ""),
        words=tuple(dict(w) for w in document.get("words", [])),
        turns=tuple(dict(t) for t in document.get("turns", [])),
        speakers=tuple(dict(s) for s in document.get("speakers", [])),
        snapshot_hash=document.get("result_hash", ""),
        readiness={"state": state, "warnings": warnings, "reason_code": None},
        warnings=tuple(warnings),
        binding_status="gebunden",
        reason_code=None,
    )
