"""JFW-4: Cue-Gruppierung ``lesbare_untertitel_v1`` und SRT/VTT (I/O-frei).

Grenzen-Leiter: Wortgrenze → Turngrenze → Segmentgrenze. Jeder Cue verwendet
ausschliesslich autoritative vorhandene Grenzen; ohne jeden autoritativen
Zeitbereich bleibt das Praesentationsformat blockiert (``not_exportable`` nennt
den Bereich) — nie still ausgelassen, nie erfundene Zeit. Overlap behaltte
seine ueberlappenden Intervalle; Labels sind getrennte Praesationszeilen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

SENTENCE_END_CHARS = ".!?…;,"
LINE_WIDTH = 42
MAX_CUE_MS = 6_000
MAX_CUE_CHARS = 84

_SRT_TS = re.compile(r"^\d{2,}:\d{2}:\d{2},\d{3}$")
_VTT_TS = re.compile(r"^\d{2,}:\d{2}:\d{2}\.\d{3}$")

PARTIAL_MARKERS = {
    "timing_only": "Teilqualität: timing_only — ohne Sprecher",
    "speaker_only": "Teilqualität: speaker_only — ohne Wortzeiten",
}


@dataclass(frozen=True)
class Cue:
    index: int
    start_ms: int
    end_ms: int
    time_quality: str
    word_ids: tuple[str, ...]
    cluster_id: str | None
    label: str | None
    labels: tuple[str, ...]
    text: str


def wrap_lines(text: str, width: int = LINE_WIDTH) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split(" "):
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines or [""]


def _cover_lookup(snapshot):
    """Wort-ID -> (Art, Cover-ID, (start, end)) ueber Turn-/Segmentmitgliedschaft."""
    covers: dict[str, tuple[str, str, tuple[int, int]]] = {}
    for turn in snapshot.turns:
        for word_id in turn.get("word_ids", []):
            covers.setdefault(
                str(word_id),
                ("turn", str(turn.get("turn_id")),
                 (int(turn.get("start_ms")), int(turn.get("end_ms")))),
            )
    for segment in snapshot.segments:
        for word_id in segment.get("word_ids", []):
            covers.setdefault(
                str(word_id),
                ("segment", str(segment.get("segment_id")),
                 (int(segment.get("start_ms")), int(segment.get("end_ms")))),
            )
    return covers


def build_cues(snapshot) -> tuple[list[Cue], list[dict]]:
    """Liefert ``(cues, not_exportable_ranges)``; non-empty zweite Liste =
    Praesentationsformat blockiert."""
    covers = _cover_lookup(snapshot)
    assignments = {str(a.get("word_id")): a for a in snapshot.word_assignments}
    display_by_cluster = {d["cluster_id"]: d for d in snapshot.speakers_display}
    turn_by_id = {str(t.get("turn_id")): t for t in snapshot.turns}

    infos = []
    not_exportable: list[dict] = []
    open_range: dict | None = None
    for word in snapshot.words:
        word_id = str(word.get("word_id"))
        own = (
            (int(word["start_ms"]), int(word["end_ms"]))
            if word.get("timing_status") == "aligned"
            and word.get("start_ms") is not None
            else None
        )
        cover = covers.get(word_id)
        if own is None and cover is None:
            if open_range is None:
                open_range = {
                    "word_ids": [],
                    "char_start": int(word.get("char_start", 0)),
                    "char_end": int(word.get("char_end", 0)),
                    "start_ms": None,
                    "end_ms": None,
                    "reason_code": "kein_autoritativer_zeitbereich",
                }
            open_range["word_ids"].append(word_id)
            open_range["char_end"] = int(word.get("char_end", 0))
            continue
        if open_range is not None:
            not_exportable.append(open_range)
            open_range = None
        infos.append({"word": word, "own": own, "cover": cover})
    if open_range is not None:
        not_exportable.append(open_range)

    # Units: nicht-eigene Grenzen duerfen nur innerhalb eines gemeinsamen
    # umfassenden autoritativen Zeitbereichs (Turn/Segment) stehen.
    units: list[tuple[list[dict], tuple | None]] = []
    cur: list[dict] = []
    cur_cover: tuple | None = None
    cover_words = {
        cid: set(turn_by_id[cid].get("word_ids", []))
        for kind, cid, _ in (
            (i["cover"][0], i["cover"][1], i["cover"][2]) for i in infos if i["cover"]
        )
        if kind == "turn"
    }
    for info in infos:
        word_id = str(info["word"].get("word_id"))
        joinable = False
        if info["own"] is not None:
            joinable = cur_cover is None or (
                info["cover"] is not None and info["cover"][1] == cur_cover[1]
            )
        else:
            if cur_cover is not None:
                joinable = info["cover"] is not None and info["cover"][1] == cur_cover[1]
            else:
                cid = info["cover"][1]
                member = cover_words.get(cid)
                if member is None:
                    member = {
                        wid
                        for seg in snapshot.segments
                        if str(seg.get("segment_id")) == cid
                        for wid in seg.get("word_ids", [])
                    }
                joinable = all(
                    str(x["word"].get("word_id")) in member for x in cur
                ) if cur else True
                if joinable:
                    cur_cover = info["cover"]
        if joinable and cur:
            cur.append(info)
        else:
            if cur:
                units.append((cur, cur_cover))
            cur = [info]
            cur_cover = info["cover"] if info["own"] is None else None
    if cur:
        units.append((cur, cur_cover))

    cues: list[Cue] = []
    for unit, cover in units:
        all_owned = all(x["own"] is not None for x in unit)
        if all_owned:
            subunits = _split_readable(unit, assignments)
            quality = "wortgenau"
            for sub in subunits:
                starts = [x["own"][0] for x in sub]
                ends = [x["own"][1] for x in sub]
                cues.append(_make_cue(snapshot, sub, min(starts), max(ends),
                                      quality, assignments, display_by_cluster, turn_by_id))
        else:
            starts = [cover[2][0]] + [x["own"][0] for x in unit if x["own"]]
            ends = [cover[2][1]] + [x["own"][1] for x in unit if x["own"]]
            quality = "turngenau" if cover[0] == "turn" else "segmentgenau"
            cues.append(_make_cue(snapshot, unit, min(starts), max(ends),
                                  quality, assignments, display_by_cluster, turn_by_id))

    # Deterministische Ordnung: Start, dann Ende, dann Quellreihenfolge.
    decorated = sorted(enumerate(cues), key=lambda ic: (ic[1].start_ms, ic[1].end_ms, ic[0]))
    ordered = [
        Cue(
            index=position,
            start_ms=c.start_ms,
            end_ms=c.end_ms,
            time_quality=c.time_quality,
            word_ids=c.word_ids,
            cluster_id=c.cluster_id,
            label=c.label,
            labels=c.labels,
            text=c.text,
        )
        for position, (_, c) in enumerate(decorated)
    ]
    return ordered, not_exportable


def _split_readable(unit: list[dict], assignments: dict) -> list[list[dict]]:
    subunits: list[list[dict]] = []
    cur: list[dict] = []
    for info in unit:
        if cur:
            prev = cur[-1]
            prev_text = str(prev["word"].get("text", ""))
            prev_cluster = _cluster_of(prev["word"], assignments)
            this_cluster = _cluster_of(info["word"], assignments)
            start_ms = cur[0]["own"][0]
            end_ms = info["own"][1]
            chars = sum(len(str(x["word"].get("text", ""))) + 1 for x in cur) + len(
                str(info["word"].get("text", ""))
            )
            if (
                prev_cluster != this_cluster
                or prev_text.endswith(tuple(SENTENCE_END_CHARS))
                or end_ms - start_ms > MAX_CUE_MS
                or chars > MAX_CUE_CHARS
            ):
                subunits.append(cur)
                cur = [info]
                continue
        cur.append(info)
    if cur:
        subunits.append(cur)
    return subunits


def _cluster_of(word: dict, assignments: dict) -> str | None:
    entry = assignments.get(str(word.get("word_id")))
    return str(entry.get("cluster_id")) if entry and entry.get("cluster_id") else None


def _make_cue(snapshot, unit, start_ms, end_ms, quality, assignments,
              display_by_cluster, turn_by_id) -> Cue:
    words = [x["word"] for x in unit]
    word_ids = tuple(str(w.get("word_id")) for w in words)
    cluster_id = None
    for w in words:
        cluster_id = _cluster_of(w, assignments)
        if cluster_id:
            break
    display = display_by_cluster.get(cluster_id or "")
    label = None
    if display is not None:
        label = display.get("display_name") or display.get("display_label")
    labels: list[str] = []
    statuses = {
        (assignments.get(str(w.get("word_id"))) or {}).get("speaker_status")
        for w in words
    }
    if "unsicher" in statuses:
        labels.append("unsicher")
    if "nicht_zugeordnet" in statuses or cluster_id is None:
        labels.append("nicht_zugeordnet")
    overlaps = set()
    for w in words:
        entry = assignments.get(str(w.get("word_id"))) or {}
        turn = turn_by_id.get(str(entry.get("turn_id")))
        if turn is not None:
            overlaps.add(turn.get("overlap"))
    if "ueberlappend" in overlaps:
        labels.append("überlappend")
    if "overlap_unsicher" in overlaps:
        labels.append("overlap_unsicher")
    gaps = (snapshot.jfw11 or {}).get("source_gaps") or []
    if any(
        int(g.get("start_ms", -1)) < end_ms and int(g.get("end_ms", -1)) > start_ms
        for g in gaps
    ):
        labels.append("quellenbegrenzt")
    jfw11 = snapshot.jfw11 or {}
    repr_words: dict[str, set] = {}
    for rep in jfw11.get("source_representations") or []:
        for rep_word in rep.get("word_ids") or []:
            repr_words.setdefault(str(rep.get("source_id")), set()).add(str(rep_word))
    word_kinds: dict[str, set] = {}
    for decision in jfw11.get("dedupe_decisions") or []:
        kind = str(decision.get("kind"))
        if kind not in ("dedupe_unsicher", "getrennt_behalten"):
            continue
        for sid in decision.get("source_ids") or []:
            for rep_word in repr_words.get(str(sid), set()):
                word_kinds.setdefault(rep_word, set()).add(kind)
    kinds_here: set = set()
    for w in words:
        kinds_here |= word_kinds.get(str(w.get("word_id")), set())
    if "dedupe_unsicher" in kinds_here:
        labels.append("Deduplizierung unsicher")
    if "getrennt_behalten" in kinds_here:
        labels.append("Getrennt behalten")
    return Cue(
        index=0,
        start_ms=int(start_ms),
        end_ms=int(end_ms),
        time_quality=quality,
        word_ids=word_ids,
        cluster_id=cluster_id,
        label=label,
        labels=tuple(labels),
        text=" ".join(str(w.get("text", "")) for w in words),
    )


def _label_line(cue: Cue, extra: str | None = None) -> str | None:
    items = ([cue.label] if cue.label else []) + list(cue.labels)
    if extra:
        items.append(extra)
    return "[" + ", ".join(items) + "]" if items else None


def _fmt_ts(ms: int, sep: str) -> str:
    ms = max(0, int(ms))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{sep}{millis:03d}"


def mask_vtt(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def serialize_srt(cues, partial_marker: str | None = None) -> str:
    blocks = []
    for position, cue in enumerate(cues, start=1):
        lines = []
        label = _label_line(cue, partial_marker if position == 1 else None)
        if label:
            lines.append(label)
        lines.extend(wrap_lines(cue.text))
        blocks.append(
            f"{position}\n{_fmt_ts(cue.start_ms, ',')} --> {_fmt_ts(cue.end_ms, ',')}\n"
            + "\n".join(lines)
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def serialize_vtt(cues, partial_marker: str | None = None) -> str:
    blocks = []
    for position, cue in enumerate(cues, start=1):
        lines = []
        label = _label_line(cue, partial_marker if position == 1 else None)
        if label:
            lines.append(mask_vtt(label))
        lines.extend(mask_vtt(line) for line in wrap_lines(cue.text))
        blocks.append(
            f"{position}\n{_fmt_ts(cue.start_ms, '.')} --> {_fmt_ts(cue.end_ms, '.')}\n"
            + "\n".join(lines)
        )
    body = "\n\n".join(blocks)
    return f"WEBVTT\n\n{body}\n" if blocks else "WEBVTT\n"


def _validate_blocks(text: str, duration_ms: int, ts_re: re.Pattern,
                     sep: str, expect_header: bool) -> list[str]:
    errors: list[str] = []
    if expect_header and not text.startswith("WEBVTT"):
        errors.append("kopf_fehlt")
    body = text[len("WEBVTT\n\n"):] if expect_header else text
    blocks = [b for b in body.strip().split("\n\n") if b.strip()]
    if not blocks:
        errors.append("keine_cues")
        return errors
    prev_start = -1
    for position, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            errors.append(f"block_unvollstaendig:{position}")
            continue
        if lines[0] != str(position):
            errors.append(f"nummer_falsch:{position}")
        timing = lines[1].split(" --> ")
        if len(timing) != 2 or not ts_re.match(timing[0]) or not ts_re.match(timing[1]):
            errors.append(f"zeitformat_falsch:{position}")
            continue
        start = _parse_ts(timing[0], sep)
        end = _parse_ts(timing[1], sep)
        if not (0 <= start < end <= int(duration_ms)):
            errors.append(f"intervall_ungueltig:{position}")
        if start < prev_start:
            errors.append(f"ordnung_falsch:{position}")
        prev_start = start
    return errors


def _parse_ts(ts: str, sep: str) -> int:
    hhmmss, ms = ts.split(sep)
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return ((h * 60 + m) * 60 + s) * 1000 + int(ms)


def validate_srt(text: str, audio_duration_ms: int) -> list[str]:
    return _validate_blocks(text, audio_duration_ms, _SRT_TS, ",", False)


def validate_vtt(text: str, audio_duration_ms: int) -> list[str]:
    return _validate_blocks(text, audio_duration_ms, _VTT_TS, ".", True)
