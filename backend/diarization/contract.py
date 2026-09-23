"""JFW-3: Ergebnisvertrag Diarisierung — Cluster, Turns, Overlap, Unveränderlichkeit.

Vertrag (Spec „Diarization Result Contract" + „Sprechercluster-, Turn- und
Wortvertrag"):

* **Heiliger Unveränderlichkeits-Vertrag:** JFW-3 ergänzt ausschliesslich
  Sprechercluster, Turns und Overlap-/Unsicherheitsstatus. Jeder uebernommene
  JFW-2-Wort-Eintrag bleibt feldweise unveraendert (word_id, order,
  char_start/char_end, text, status, start_ms, end_ms);
  :func:`verify_words_unchanged` wirft ``ValueError`` bei jeder Mutation
  (Text, Reihenfolge, Grenzen, Wortmenge). Wortbezogene Sprecherzuordnung ist
  ein reines Overlay (``speaker_ref``/``assignment_status``/``cluster_candidates``).
* **Neutrale Cluster nur revisionsintern:** ``speaker_01``… (deterministisch
  nach erster Turn-Startzeit je Ergebnisrevision) mit getrennten
  Anzeigeetiketten ``Sprecher 1``… — nie ein Personenname, nie ein
  jobuebergreifender Identitaetsanspruch.
* **Turn-Integrität:** ``0 <= start_ms < end_ms <= Audiodauer``, endliche
  Werte. Ungueltige Provider-Turns werden verworfen und als
  Integritaetsfehler gezaehlt — der Ergebniszustand wird ``failed``.
  Zeitliche Ueberschneidungen sind nur mit explizitem Overlap-Status gueltig;
  ueberlappende Turns desselben Clusters werden zurueck in einen Turn
  zusammengefuehrt (Union).
* **Abdeckung (verwertbare Sprecherzuordnungsabdeckung):** bewertbare
  Sprachdauer = Zeitunion aller gueltigen Turns, partitioniert in
  ``verwertbar`` (sichere Einzelcluster-Zuordnung ODER explizites Overlap mit
  >= 2 belegten Clusterkandidaten), ``unsicher``, ``overlap_unsicher`` und
  ``nicht_zugeordnet``. Die drei letzteren bleiben sichtbar im Nenner und
  erhoehen die Abdeckung nicht. Die Kategorien sind zeitlich disjunkt, weil
  alle Turns einer Ueberschneidungsgruppe dieselbe Overlap-Kategorie tragen.
* **Zustandsableitung:** keine bewertbare Sprache -> ``no_speech``;
  Integritaetsfehler -> ``failed``; 100 % Abdeckung UND alle Woerter mit
  integritaetsgeprueftem Zuordnungsstatus -> ``diarized``; >= 95 % (und nicht
  ``diarized``) mit mindestens einem verwertbaren Turn -> ``partially_diarized``;
  sonst -> ``failed``.
"""
from __future__ import annotations

import hashlib
import json
import math

#: Version des Ergebnisvertrags (Teil jedes Ergebniskopfes und jedes Hashes).
RESULT_CONTRACT_VERSION = "jfw3-diarization-v1"
#: Zeitbasis aller Grenzen: Fließkomma-Millisekunden ueber die gebundene Audioquelle.
TIMEBASE_AUDIO_MS = "audio_ms_v1"

# --- Statuswerte (Spec-Wortlaut; ``nicht_ueberlappend`` ist der dokumentierte
# Vertragszusatz fuer den Nicht-Overlap-Fall, den die Spec nicht benennt). -----
TURN_ASSIGNED = "zugeordnet"
TURN_UNCERTAIN = "unsicher"
TURN_UNASSIGNED = "nicht_zugeordnet"

OVERLAP_NONE = "nicht_ueberlappend"
OVERLAP = "überlappend"
OVERLAP_UNCERTAIN = "overlap_unsicher"

WORD_ASSIGNED = "zugeordnet"
WORD_AMBIGUOUS = "mehrdeutig"
WORD_OVERLAP = "überlappend"
WORD_UNASSIGNED = "nicht_zugeordnet"

TURN_ASSIGNMENT_STATUSES = frozenset({TURN_ASSIGNED, TURN_UNCERTAIN, TURN_UNASSIGNED})
OVERLAP_STATUSES = frozenset({OVERLAP_NONE, OVERLAP, OVERLAP_UNCERTAIN})
WORD_STATUSES = frozenset({WORD_ASSIGNED, WORD_AMBIGUOUS, WORD_OVERLAP, WORD_UNASSIGNED})

#: Versionierte, inhaltsfreie Grundcodes (keine Textinhalte).
REASON_CODES = frozenset(
    {
        "no_acoustic_match",
        "invalid_boundary",
        "integrity_error",
        "overlap_uncertain",
        "uncertain_assignment",
        "no_precise_boundary",
        "text_or_boundary_change_detected",
        "provider_error",
        "provider_runtime_missing",
        "artifact_missing",
    }
)

#: Zustaende des Diarization-Result-Contracts der Spec.
RESULT_STATES = frozenset(
    {
        "queued",
        "waiting_for_local_artifact",
        "diarizing",
        "diarized",
        "partially_diarized",
        "no_speech",
        "failed",
        "canceled",
        "invalidated",
    }
)
#: Zustaende mit autoritativem, fuer JFW-11/JFW-4 freigegebenem Ergebnis.
RELEASABLE_STATES = frozenset({"diarized", "partially_diarized"})

#: Felder eines JFW-2-Wort-Eintrags, die JFW-3 nie veraendern darf.
IMMUTABLE_WORD_FIELDS = (
    "word_id",
    "order",
    "char_start",
    "char_end",
    "text",
    "status",
    "start_ms",
    "end_ms",
)


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def cluster_id_for(index: int) -> str:
    """Neutrale stabile Maschinen-ID (1-basiert): ``speaker_01`` …"""
    return f"speaker_{index:02d}"


def display_label_for(index: int) -> str:
    """Getrenntes, lokalisierbares Anzeigeetikett: ``Sprecher 1`` …"""
    return f"Sprecher {index}"


def verify_words_unchanged(
    base_words: list[dict], result_words: list[dict], text: str | None = None
) -> None:
    """Heiliges Gate: wirft ``ValueError``, sobald JFW-3 einen JFW-2-Wort-Eintrag
    mutiert hat (Text, Reihenfolge, Zeichen-/Zeitgrenzen) oder Wörter fehlen/
    doppelt vorkommen. Overlay-Felder (Sprecherzuordnung) sind erlaubt.

    Zusätzlich wird die interne Konsistenz jedes Eintrags geprueft
    (``text`` muss exakt die Spanne ``char_start:char_end`` sein) und — wenn
    ``text`` der gebundenen Transkriptrevision uebergeben wird — die
    Substring-Gleichheit gegen diese Revision."""
    if len(base_words) != len(result_words):
        raise ValueError(
            f"Wortmenge veraendert: {len(base_words)} -> {len(result_words)}"
        )
    for words in (base_words, result_words):
        for w in words:
            span = w.get("char_start"), w.get("char_end")
            if None not in span and len(w.get("text") or "") != span[1] - span[0]:
                raise ValueError(f"Anzeigetext mutiert: {w.get('word_id')}")
            if (
                text is not None
                and None not in span
                and w.get("text") != text[span[0] : span[1]]
            ):
                raise ValueError(f"Anzeigetext mutiert: {w.get('word_id')}")
    for base, out in zip(base_words, result_words, strict=True):
        for field in IMMUTABLE_WORD_FIELDS:
            if out.get(field) != base.get(field):
                raise ValueError(f"Wortfeld mutiert: {base.get('word_id')}.{field}")


def _union_ms(intervals: list[tuple[float, float]]) -> float:
    """Laenge der Zeitunion der Intervalle (keine Doppelzählung)."""
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= cur_end:
            cur_end = max(cur_end, end)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
    total += cur_end - cur_start
    return total


def _turn_category(turn: dict) -> str:
    """Kategorie je Turn fuer die Abdeckungspartition."""
    if (
        turn["assignment_status"] == TURN_ASSIGNED
        and turn["overlap_status"] == OVERLAP
        and len(turn["cluster_candidates"]) >= 2
    ):
        return "overlap"
    if (
        turn["assignment_status"] == TURN_ASSIGNED
        and turn["overlap_status"] == OVERLAP_NONE
        and len(turn["cluster_candidates"]) == 1
    ):
        return "single"
    if turn["overlap_status"] == OVERLAP_UNCERTAIN:
        return "overlap_unsicher"
    if turn["assignment_status"] == TURN_UNCERTAIN and turn["overlap_status"] == OVERLAP_NONE:
        return "unsicher"
    return "nicht_zugeordnet"


def coverage(result: dict) -> tuple[float, float]:
    """(bewertbare Sprachdauer, verwertbare Dauer) in Millisekunden."""
    cov = result["coverage"]
    return cov["speech_ms"], cov["usable_ms"]


def derive_state(turns: list[dict], words: list[dict], cov: dict, integrity_errors: int) -> str:
    if integrity_errors > 0:
        return "failed"
    speech = float(cov.get("speech_ms", 0.0))
    usable = float(cov.get("usable_ms", 0.0))
    if speech <= 0:
        return "no_speech"
    words_ok = all(w.get("assignment_status") in WORD_STATUSES for w in words)
    has_usable_turn = bool(turns) and usable > 0
    if turns and usable >= speech and words_ok:
        return "diarized"
    if has_usable_turn and usable / speech >= 0.95:
        return "partially_diarized"
    return "failed"


# --- Turn-Aufbereitung ------------------------------------------------------

def _raw_candidates(raw: dict) -> list[str]:
    cand = raw.get("candidates")
    if isinstance(cand, list) and cand:
        return [str(c) for c in cand]
    key = raw.get("speaker_key")
    return [str(key)] if key else []


def _valid_boundary(raw: dict, duration_ms: float) -> bool:
    try:
        start = float(raw["start_ms"])
        end = float(raw["end_ms"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        math.isfinite(start)
        and math.isfinite(end)
        and 0 <= start < end <= float(duration_ms)
    )


def _merge_same_speaker(raws: list[dict]) -> list[dict]:
    """Ueberlappende Turns desselben Einzelclusters zurueck zu einem Turn
    (Union) fuehren — ein Sprecher spricht nicht gleichzeitig mit sich selbst."""
    singles: dict[str, list[dict]] = {}
    others: list[dict] = []
    for raw in raws:
        cands = _raw_candidates(raw)
        if len(cands) == 1:
            singles.setdefault(cands[0], []).append(raw)
        else:
            others.append(raw)
    merged: list[dict] = []
    for group in singles.values():
        group.sort(key=lambda r: (float(r["start_ms"]), float(r["end_ms"])))
        cur = dict(group[0])
        for raw in group[1:]:
            if float(raw["start_ms"]) < float(cur["end_ms"]):  # echte Ueberschneidung
                cur["end_ms"] = max(float(cur["end_ms"]), float(raw["end_ms"]))
                cur["start_ms"] = min(float(cur["start_ms"]), float(raw["start_ms"]))
                if raw.get("overlap_hint") == "uncertain":
                    cur["overlap_hint"] = "uncertain"
                scores = [s for s in (cur.get("score"), raw.get("score")) if s is not None]
                cur["score"] = min(scores) if scores else None
            else:
                merged.append(cur)
                cur = dict(raw)
        merged.append(cur)
    merged.extend(others)
    return merged


def _overlap_groups(turns: list[dict]) -> list[list[int]]:
    """Ueberschneidungsgruppen (Union-Find ueber schneidende Intervalle)."""
    n = len(turns)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            a, b = turns[i], turns[j]
            if float(a["start_ms"]) < float(b["end_ms"]) and float(b["start_ms"]) < float(a["end_ms"]):
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return [sorted(v) for _, v in sorted(groups.items())]


def build_result(
    base_words: list[dict],
    raw_turns: list[dict],
    *,
    duration_ms: float,
    provider: str,
    speaker_spec: dict,
    frame_ms: float | None = None,
) -> dict:
    """Baut das Diarisierungsergebnis aus Provider-Turns und JFW-2-Wortvertrag.

    ``raw_turn``-Eintrag: ``start_ms``/``end_ms``, ``speaker_key`` (oder None),
    ``candidates`` (optional, belegbare Kandidaten), ``score``, ``overlap_hint``
    (``unknown`` | ``no`` | ``yes`` | ``uncertain``). Ungueltige Turns werden
    verworfen und als Integritaetsfehler gezaehlt (Ergebnis ``failed``).
    """
    integrity_errors = 0
    valid_raws: list[dict] = []
    for raw in raw_turns or []:
        if _valid_boundary(raw, duration_ms):
            valid_raws.append(dict(raw))
        else:
            integrity_errors += 1

    merged = _merge_same_speaker(valid_raws)

    # Neutrale Cluster-IDs: Reihenfolge des ersten Vorkommens (Turn-Startzeit).
    order_keys: list[str] = []
    for raw in sorted(merged, key=lambda r: (float(r["start_ms"]), float(r["end_ms"]))):
        for key in [raw.get("speaker_key"), *_raw_candidates(raw)]:
            if key and key not in order_keys:
                order_keys.append(str(key))
    key_to_cluster = {key: cluster_id_for(i + 1) for i, key in enumerate(order_keys)}

    def map_cands(keys: list[str]) -> list[str]:
        seen: list[str] = []
        for key in keys:
            cid = key_to_cluster.get(key)
            if cid and cid not in seen:
                seen.append(cid)
        return seen

    groups = _overlap_groups(merged)
    turn_records: list[dict] = []
    for group in groups:
        members = [merged[i] for i in group]
        union_keys: list[str] = []
        for raw in members:
            for key in [raw.get("speaker_key"), *_raw_candidates(raw)]:
                if key and key not in union_keys:
                    union_keys.append(str(key))
        union_clusters = map_cands(union_keys)
        reliable = all(m.get("overlap_hint") != "uncertain" for m in members)

        if len(group) >= 2:
            if reliable and len(union_clusters) >= 2:
                overlap_status, forced_assign = OVERLAP, TURN_ASSIGNED
            else:
                overlap_status, forced_assign = OVERLAP_UNCERTAIN, None
        else:
            hint = members[0].get("overlap_hint")
            if hint == "yes":
                if len(union_clusters) >= 2:
                    overlap_status, forced_assign = OVERLAP, TURN_ASSIGNED
                else:
                    overlap_status, forced_assign = OVERLAP_UNCERTAIN, None
            elif hint == "uncertain":
                overlap_status, forced_assign = OVERLAP_NONE, None
            else:
                overlap_status, forced_assign = OVERLAP_NONE, None

        for raw in members:
            own = map_cands(_raw_candidates(raw))
            uncertain = raw.get("overlap_hint") == "uncertain"
            if forced_assign == TURN_ASSIGNED:
                assign, cands, reason = TURN_ASSIGNED, union_clusters, None
            elif overlap_status == OVERLAP_UNCERTAIN:
                if own:
                    assign, reason = TURN_UNCERTAIN, "overlap_uncertain"
                else:
                    assign, reason = TURN_UNASSIGNED, "no_acoustic_match"
                cands = union_clusters
            elif len(own) == 1 and not uncertain:
                assign, cands, reason = TURN_ASSIGNED, own, None
            elif own:
                assign, cands, reason = TURN_UNCERTAIN, own, "uncertain_assignment"
            else:
                assign, cands, reason = TURN_UNASSIGNED, [], "no_acoustic_match"
            turn_records.append(
                {
                    "raw": raw,
                    "start_ms": float(raw["start_ms"]),
                    "end_ms": float(raw["end_ms"]),
                    "cluster_candidates": cands,
                    "assignment_status": assign,
                    "overlap_status": overlap_status,
                    "reason_code": reason,
                }
            )

    turn_records.sort(key=lambda t: (t["start_ms"], t["end_ms"]))
    turns: list[dict] = []
    for i, rec in enumerate(turn_records):
        raw = rec.pop("raw")
        turns.append(
            {
                "turn_id": f"t-{i:04d}",
                "start_ms": rec["start_ms"],
                "end_ms": rec["end_ms"],
                "cluster_candidates": rec["cluster_candidates"],
                "assignment_status": rec["assignment_status"],
                "overlap_status": rec["overlap_status"],
                "reason_code": rec["reason_code"],
                "quality": {
                    "provider": provider,
                    "frame_ms": frame_ms,
                    "speaker_key": raw.get("speaker_key"),
                    "score": (float(raw["score"]) if raw.get("score") is not None else None),
                },
            }
        )

    # --- Abdeckungspartition (Intervall-Union je Kategorie) ------------------
    cat_intervals: dict[str, list[tuple[float, float]]] = {
        "single": [],
        "overlap": [],
        "unsicher": [],
        "overlap_unsicher": [],
        "nicht_zugeordnet": [],
    }
    for t in turns:
        cat_intervals[_turn_category(t)].append((t["start_ms"], t["end_ms"]))
    single_ms = _union_ms(cat_intervals["single"])
    overlap_ms = _union_ms(cat_intervals["overlap"])
    unsicher_ms = _union_ms(cat_intervals["unsicher"])
    overlap_unsicher_ms = _union_ms(cat_intervals["overlap_unsicher"])
    nicht_zugeordnet_ms = _union_ms(cat_intervals["nicht_zugeordnet"])
    speech_ms = _union_ms([(t["start_ms"], t["end_ms"]) for t in turns])
    cov = {
        "speech_ms": speech_ms,
        "usable_ms": single_ms + overlap_ms,
        "single_ms": single_ms,
        "overlap_ms": overlap_ms,
        "unsicher_ms": unsicher_ms,
        "overlap_unsicher_ms": overlap_unsicher_ms,
        "nicht_zugeordnet_ms": nicht_zugeordnet_ms,
    }

    # --- Wort-Overlay (heiliger Unveränderlichkeits-Vertrag) ----------------
    words: list[dict] = []
    for base in base_words or []:
        entry = {field: base.get(field) for field in IMMUTABLE_WORD_FIELDS}
        start, end = base.get("start_ms"), base.get("end_ms")
        entry.update(
            {
                "speaker_ref": None,
                "assignment_status": WORD_UNASSIGNED,
                "cluster_candidates": [],
                "reason_code": "no_acoustic_match",
                "quality": {
                    "provider": provider,
                    "frame_ms": frame_ms,
                    "turn_ids": [],
                },
            }
        )
        if start is None or end is None:
            entry["reason_code"] = "no_precise_boundary"
            words.append(entry)
            continue
        cutting = [
            t
            for t in turns
            if t["start_ms"] < float(end) and float(start) < t["end_ms"]
        ]
        entry["quality"]["turn_ids"] = [t["turn_id"] for t in cutting]
        if not cutting:
            words.append(entry)
            continue
        union: list[str] = []
        for t in cutting:
            for cid in t["cluster_candidates"]:
                if cid not in union:
                    union.append(cid)
        union.sort()
        if any(t["overlap_status"] == OVERLAP for t in cutting):
            entry["assignment_status"] = WORD_OVERLAP
            entry["cluster_candidates"] = union
            entry["reason_code"] = None
        elif any(
            t["overlap_status"] == OVERLAP_UNCERTAIN or t["assignment_status"] == TURN_UNCERTAIN
            for t in cutting
        ):
            entry["assignment_status"] = WORD_AMBIGUOUS
            entry["cluster_candidates"] = union
            entry["reason_code"] = "overlap_uncertain"
        elif len(cutting) > 1:
            entry["assignment_status"] = WORD_AMBIGUOUS
            entry["cluster_candidates"] = union
            entry["reason_code"] = "uncertain_assignment"
        elif (
            cutting[0]["assignment_status"] == TURN_ASSIGNED
            and cutting[0]["overlap_status"] == OVERLAP_NONE
            and len(cutting[0]["cluster_candidates"]) == 1
        ):
            entry["assignment_status"] = WORD_ASSIGNED
            entry["cluster_candidates"] = union
            entry["speaker_ref"] = cutting[0]["cluster_candidates"][0]
            entry["reason_code"] = None
        words.append(entry)

    verify_words_unchanged(base_words or [], words)  # heiliges Gate

    status = derive_state(turns, words, cov, integrity_errors)
    counters = {
        "cluster_count": len(order_keys),
        "turn_count": len(turns),
        "word_count": len(words),
        "word_assigned_count": sum(1 for w in words if w["assignment_status"] == WORD_ASSIGNED),
        "overlap_count": sum(1 for t in turns if t["overlap_status"] != OVERLAP_NONE),
        "uncertainty_count": sum(
            1
            for t in turns
            if t["assignment_status"] == TURN_UNCERTAIN
            or t["overlap_status"] == OVERLAP_UNCERTAIN
        ),
        "invalid_turn_count": integrity_errors,
    }
    payload = {
        "contract_version": RESULT_CONTRACT_VERSION,
        "timebase": TIMEBASE_AUDIO_MS,
        "status": status,
        "reason_code": ("integrity_error" if integrity_errors else None),
        "speaker_spec": dict(speaker_spec or {}),
        "clusters": [
            {"cluster_id": cluster_id_for(i + 1), "display_label": display_label_for(i + 1)}
            for i in range(len(order_keys))
        ],
        "turns": turns,
        "words": words,
        "coverage": cov,
        "counters": counters,
    }
    return {**payload, "result_hash": canonical_hash(payload)}
