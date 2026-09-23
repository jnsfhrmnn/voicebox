"""JFW-4: gebundener, unveraenderlicher Export-Snapshot (I/O-frei).

``build_snapshot`` validiert die explizit uebergebenen Quellpayloads (JFW-2,
JFW-3, optional JFW-11) gegen die gebundenen Revisions-Hashes und baut daraus
einen frozen Snapshot. Jede fehlende, ungueltige oder hashfremde Bindung ist
fail-closed ``blocked`` — Quellrevisionen werden nie mutiert, Anzeigeentscheide
landen ausschliesslich in getrennten abgeleiteten Feldern.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType

from .contract import (
    TIMING_MISSING,
    TIMING_OK,
    TIMING_PARTIAL,
    assess_readiness,
    blocked_readiness,
    validate_words,
)
from .provenance import ExportRequest, transcript_text_hash

AUTHORIZED_NAME_STATES = ("confirmed", "manual")
UNAUTHORIZED_NAME_STATES = ("suggested", "conflict", "rejected", "invalidated")
PARTIAL_SOURCE_STATES = (
    "secured_partial",
    "sync_unsicher",
    "single_source",
    "dual_source_partial",
)
DEDUPE_UNSAFE_KINDS = ("dedupe_unsicher", "getrennt_behalten")


@dataclass(frozen=True)
class ExportSnapshot:
    request: ExportRequest
    text: str
    words: tuple
    clusters: tuple
    turns: tuple
    word_assignments: tuple
    segments: tuple
    jfw11: dict | None
    readiness: dict
    speakers_display: tuple
    binding_status: str
    reason_code: str | None


def display_speakers(clusters, name_mappings, name_policy: str) -> tuple[dict, ...]:
    """Getrennte Anzeigeentscheide: ``display_name`` nur bei ``confirmed``/
    ``manual`` und bestaetigter Namenspolitik; alles andere bleibt neutrale
    Anzeige und Annotation."""
    by_cluster: dict[str, dict] = {}
    if name_mappings:
        for mapping in name_mappings:
            entry = by_cluster.setdefault(str(mapping.get("cluster_id")), {
                "mapping_state": "neutral",
                "mapping_provenance": {},
                "authorized": None,
                "unauthorized": [],
            })
            state = str(mapping.get("state"))
            record = {
                "name": mapping.get("name"),
                "state": state,
                "provenance": mapping.get("provenance") or {},
            }
            if state in AUTHORIZED_NAME_STATES:
                entry["mapping_state"] = state
                entry["mapping_provenance"] = record["provenance"]
                entry["authorized"] = record
            elif state != "neutral":
                entry["mapping_state"] = state
                entry["mapping_provenance"] = record["provenance"]
                entry["unauthorized"].append(record)

    out: list[dict] = []
    for cluster in clusters:
        cluster_id = str(cluster.get("cluster_id"))
        entry = by_cluster.get(cluster_id, {
            "mapping_state": "neutral",
            "mapping_provenance": {},
            "authorized": None,
            "unauthorized": [],
        })
        authorized = entry["authorized"]
        display_name = None
        display_name_source = None
        if (
            name_policy == "confirmed_names"
            and authorized is not None
            and authorized["state"] in AUTHORIZED_NAME_STATES
        ):
            display_name = authorized["name"]
            display_name_source = authorized["state"]
        out.append({
            "cluster_id": cluster_id,
            "display_label": cluster.get("display_label"),
            "display_name": display_name,
            "display_name_source": display_name_source,
            "mapping_state": entry["mapping_state"],
            "mapping_provenance": entry["mapping_provenance"],
            "unauthorized_name_annotations": entry["unauthorized"],
        })
    return tuple(out)


def _binding_blocked(reason: str) -> dict:
    return blocked_readiness(reason)


def build_snapshot(request: ExportRequest, sources: dict) -> ExportSnapshot:
    words = tuple(dict(w) for w in sources.get("jfw2", {}).get("words", []))
    clusters = tuple(dict(c) for c in sources.get("jfw3", {}).get("clusters", []))
    turns = tuple(dict(t) for t in sources.get("jfw3", {}).get("turns", []))
    assignments = tuple(
        dict(a) for a in sources.get("jfw3", {}).get("word_assignments", [])
    )
    segments = tuple(dict(s) for s in sources.get("segments", []))
    jfw11 = sources.get("jfw11")
    text = str(sources.get("text", ""))

    binding_status = "ok"
    reason: str | None = None

    # 1. Transkript-Textbindung
    if transcript_text_hash(text) != request.transcript_text_hash:
        binding_status, reason = "binding_conflict", "binding_conflict"
    # 2. Ergebnisbindungen JFW-2/JFW-3
    if binding_status == "ok":
        jfw2 = sources.get("jfw2") or {}
        jfw3 = sources.get("jfw3") or {}
        if jfw2.get("result_hash") != request.jfw2_result_hash or jfw3.get("result_hash") != request.jfw3_result_hash:
            binding_status, reason = "binding_conflict", "binding_conflict"
    # 3. Optionale JFW-11-Bindung (Pflicht nur bei gebundenem Dual-Source-Meeting)
    if binding_status == "ok":
        if request.jfw11_expected and not jfw11:
            binding_status, reason = "binding_missing", "binding_missing"
        elif jfw11 is not None:
            if request.jfw11_commit_hash is None:
                binding_status, reason = "binding_missing", "binding_missing"
            elif jfw11.get("commit_hash") != request.jfw11_commit_hash:
                binding_status, reason = "binding_conflict", "binding_conflict"
    # 4. Heilige Wort-Invarianten
    if binding_status == "ok":
        word_errors = validate_words(words, request.audio_duration_ms)
        if word_errors:
            return ExportSnapshot(
                request=request,
                text=text,
                words=words,
                clusters=clusters,
                turns=turns,
                word_assignments=assignments,
                segments=segments,
                jfw11=jfw11,
                readiness=blocked_readiness("wortdaten_ungueltig"),
                speakers_display=display_speakers(
                    clusters,
                    (jfw11 or {}).get("name_mappings"),
                    request.name_policy,
                ),
                binding_status="ok",
                reason_code="wortdaten_ungueltig",
            )

    if binding_status != "ok":
        readiness = _binding_blocked(reason or binding_status)
        return ExportSnapshot(
            request=request,
            text=text,
            words=words,
            clusters=clusters,
            turns=turns,
            word_assignments=assignments,
            segments=segments,
            jfw11=jfw11,
            readiness=readiness,
            speakers_display=display_speakers(
                clusters, (jfw11 or {}).get("name_mappings"), request.name_policy
            ),
            binding_status=binding_status,
            reason_code=reason,
        )

    # Qualitaetsdimensionen (nichts wird still hochgestuft)
    aligned = sum(1 for w in words if w.get("timing_status") == "aligned")
    if request.jfw2_status in ("failed", "no_alignable_speech") or aligned == 0:
        timing = TIMING_MISSING
    elif aligned == len(words) and request.jfw2_status == "aligned":
        timing = TIMING_OK
    else:
        timing = TIMING_PARTIAL

    unsafe_assignments = sum(
        1 for a in assignments if a.get("speaker_status") != "sicher"
    )
    if request.jfw3_status in ("failed", "no_speech") or not clusters:
        speakers = TIMING_MISSING
    elif request.jfw3_status == "partially_diarized" or unsafe_assignments:
        speakers = TIMING_PARTIAL
    else:
        speakers = TIMING_OK

    if jfw11 is None:
        sources_dim = "nicht_betroffen"
        dedupe_dim = "nicht_betroffen"
    else:
        gaps = jfw11.get("source_gaps") or []
        partial_markers = (
            request.jfw11_status in PARTIAL_SOURCE_STATES
            or jfw11.get("sync_status") not in (None, "sync_ok")
            or bool(gaps)
        )
        sources_dim = TIMING_PARTIAL if partial_markers else TIMING_OK
        decisions = jfw11.get("dedupe_decisions") or []
        if any(d.get("kind") in DEDUPE_UNSAFE_KINDS for d in decisions):
            dedupe_dim = "unsicher"
        elif decisions or (jfw11.get("dedupe_status") or None) == "duplikat_bestaetigt":
            dedupe_dim = "sauber"
        else:
            dedupe_dim = "nicht_betroffen"

    unbounded = sum(1 for w in words if w.get("start_ms") is None)
    unsafe_decisions = sum(
        1 for d in (jfw11 or {}).get("dedupe_decisions") or []
        if d.get("kind") in DEDUPE_UNSAFE_KINDS
    )
    gap_list = (jfw11 or {}).get("source_gaps") or []
    counts = {
        "timing": unbounded or (1 if timing == TIMING_PARTIAL else 0),
        "speakers": unsafe_assignments or (1 if speakers == TIMING_PARTIAL else 0),
        "sources": len(gap_list)
        + (0 if (jfw11 or {}).get("sync_status") in (None, "sync_ok") else 1),
        "dedupe": unsafe_decisions or (1 if dedupe_dim == "unsicher" else 0),
        "partial": 1,
    }
    readiness = assess_readiness(
        timing=timing,
        speakers=speakers,
        sources=sources_dim,
        dedupe=dedupe_dim,
        partial_confirmed=bool(request.partial_confirmed),
        counts=counts,
    )
    speakers_display = display_speakers(
        clusters, (jfw11 or {}).get("name_mappings"), request.name_policy
    )
    return ExportSnapshot(
        request=request,
        text=text,
        words=words,
        clusters=clusters,
        turns=turns,
        word_assignments=assignments,
        segments=segments,
        jfw11=jfw11,
        readiness=readiness,
        speakers_display=speakers_display,
        binding_status="ok",
        reason_code=None,
    )


def assignment_map(snapshot: ExportSnapshot):
    return MappingProxyType(
        {str(a.get("word_id")): a for a in snapshot.word_assignments}
    )
