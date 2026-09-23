"""JFW-6: Sound-Cue-Marken (I/O-frei).

Start-/Stopp-/Fehlerton-Fenster werden im Run-Manifest markiert (Pflichtinhalt
des JFW-7-Handoffs) und sind nie Sprach- oder Aufnahme-Evidenz (Muster
JFW-11 ``is_cue_window``).
"""
from __future__ import annotations

from dataclasses import dataclass

CUE_KINDS = ("start_ton", "stopp_ton", "fehler_ton")


@dataclass(frozen=True)
class CueMark:
    kind: str
    start_100ns: int
    end_100ns: int

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "start_100ns": int(self.start_100ns),
            "end_100ns": int(self.end_100ns),
        }


def _norm(mark) -> dict:
    return mark.to_dict() if isinstance(mark, CueMark) else dict(mark)


def to_manifest(marks) -> list[dict]:
    return [_norm(m) for m in marks]


def validate_cues(marks, *, started_at_100ns: int, ended_at_100ns: int) -> list[str]:
    """Fail-closed Validierung; leere Liste = Cue-Marken zulaessig."""
    errors: list[str] = []
    prev_end: int | None = None
    for i, raw in enumerate(marks):
        m = _norm(raw)
        kind = m.get("kind")
        start = int(m.get("start_100ns", 0))
        end = int(m.get("end_100ns", 0))
        if kind not in CUE_KINDS:
            errors.append(f"cue_unbekannt:{kind}")
        if start >= end:
            errors.append(f"cue_fenster_unzulaessig:{i}")
        if start < int(started_at_100ns) or end > int(ended_at_100ns):
            errors.append(f"cue_ausserhalb_des_runs:{i}")
        if prev_end is not None and start < prev_end:
            errors.append(f"cue_ueberlappung:{i}")
        prev_end = end
    return errors


def is_cue_window(marks, t_100ns: int) -> bool:
    """Cue-Fenster sind markiert und nie Teilnehmer-, Sprach- oder
    Namensevidenz."""
    for raw in marks:
        m = _norm(raw)
        if int(m["start_100ns"]) <= int(t_100ns) <= int(m["end_100ns"]):
            return True
    return False
