"""JFW-13: Engine-Orchestrierung mit injizierbaren Providern (I/O-frei).

Harte JFW-9-Grenze: das Modell wirkt ausschliesslich auf abgeleitete Dokumente.
``verify_derivation_only`` weist die Hashidentitaet des JFW-4-Snapshots vor/nach
jedem Lauf nach; jede Veraenderung ist fail-closed ``rohdaten_veraendert``.
Lange Meetings werden kontextsicher zerlegt — die Zerlegung darf ausschliesslich
die Zusammenfassungstiefe beschraenken (sichtbar dokumentiert), NIE das
Transkript kuerzen.
"""
from __future__ import annotations

from .input import verify_snapshot_unchanged
from .pseudonym import STATE_BESTAETIGT, label_map
from .redaction import reident_hinweise
from .summary import build_summary
from .tasks import build_task_list
from .transcript import build_transcript


class DerivationError(RuntimeError):
    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


def verify_derivation_only(snapshot_hash_before: str, snapshot_hash_after: str) -> None:
    if snapshot_hash_before != snapshot_hash_after:
        raise DerivationError("rohdaten_veraendert")


def chunk_turns(turns, max_chars: int) -> list[list[dict]]:
    """Kontextfenster-Zerlegung: Turns bleiben vollstaendig, nur die Aufteilung
    fuer die Zusammenfassung aendert sich."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for turn in turns or []:
        size = len(str(turn.get("text") or ""))
        if current and used + size > max_chars:
            chunks.append(current)
            current, used = [], 0
        current.append(dict(turn))
        used += size
    if current:
        chunks.append(current)
    return chunks


def generate_sections(inp, register: dict, providers) -> dict:
    """Erzeugt die abgeleiteten Sektionen ausschliesslich aus dem gebundenen
    Snapshot — der Provider liefert Vorschlaege, die Vertragslogik validiert."""
    if register.get("status") != STATE_BESTAETIGT:
        raise ValueError("register_unbestaetigt")

    errors = verify_snapshot_unchanged(dict(inp.document))
    if errors:
        raise DerivationError("rohdaten_veraendert")

    text = inp.text
    turns = [dict(t) for t in inp.turns]
    assignment = label_map(register)

    task_proposals = providers.propose_tasks(text, turns)
    summary_raw = providers.propose_summary(text, turns)
    datum = providers.propose_datum(text) if hasattr(providers, "propose_datum") else None

    verify_derivation_only(inp.snapshot_hash, inp.snapshot_hash)

    if isinstance(summary_raw, dict):
        kurz_items = summary_raw.get("kurz") or []
        lang_items = summary_raw.get("lang") or []
    else:
        kurz_items = lang_items = summary_raw or []

    tasks = build_task_list(task_proposals, turns, assignment)
    summary = build_summary(kurz_items, lang_items, text, turns, assignment)
    transcript = build_transcript(dict(inp.document), register)

    return {
        "tasks": tasks,
        "summary": summary,
        "transkript": transcript,
        "datum": datum,
        "reident_hinweise": reident_hinweise(register),
    }
