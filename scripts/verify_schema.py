#!/usr/bin/env python3
"""JFW-1 Schema-Gate: prueft eine jf-whisper-Datenbank gegen das Produktprofil.

Fail-closed: jede verbotene Tabelle (TTS/Generation/Story/Effects/Channel/MCP)
oder ein unerwartetes Objekt in der DB ist ein harter Fehler. Das Profil
(``product-profiles/jf-whisper.json`` -> ``database_tables``) ist die einzige
Wahrheit fuer den erlaubten Tabellen-Satz.

Aufruf:
    python scripts/verify_schema.py <pfad-zur-db>          # pruefen
    python scripts/verify_schema.py --create-empty <pfad>  # leere, konforme DB anlegen
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = REPO_ROOT / "product-profiles" / "jf-whisper.json"


def load_profile() -> dict:
    prof = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    tables = prof["database_tables"]
    return {
        "active": set(tables["active"]),
        "forbidden": set(tables["forbidden"]),
        # Infrastruktur der Schema-Linie (Alembic + Manifest-Meta): erlaubt,
        # aber kein Produkt-Datensatz.
        "infrastructure": set(tables.get("infrastructure", [])),
    }


def table_names(db_path: Path) -> set[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


def check(db_path: Path) -> int:
    prof = load_profile()
    active, forbidden = prof["active"], prof["forbidden"]
    infrastructure = prof.get("infrastructure", set())

    if not db_path.exists():
        print(f"[schema-gate] FEHLER: DB nicht vorhanden: {db_path}")
        return 2

    found = table_names(db_path)
    missing_active = sorted(active - found)
    present_forbidden = sorted(forbidden & found)
    unknown = sorted(found - active - forbidden - infrastructure)

    problems: list[str] = []
    if missing_active:
        problems.append(f"erlaubte Tabellen fehlen: {missing_active}")
    if present_forbidden:
        problems.append(f"verbotene Tabellen vorhanden: {present_forbidden}")
    if unknown:
        problems.append(f"unbekannte Objekte (weder erlaubt noch bekannt-verboten): {unknown}")

    if problems:
        print(f"[schema-gate] FEHLER ({db_path}):")
        for p in problems:
            print(f"  - {p}")
        return 1

    print(
        f"[schema-gate] OK: {db_path} — exakt die erlaubten Tabellen "
        f"{sorted(active)} (+ Infrastruktur {sorted(infrastructure) if infrastructure else '—'}); "
        f"keine verbotene Struktur."
    )
    return 0


def create_empty(db_path: Path) -> int:
    """Leere, konforme jf-whisper-DB anlegen (nur erlaubte Tabellen)."""
    prof = load_profile()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(db_path)
    try:
        # Minimale, profilkonforme Struktur. Die produktive Migration wird von
        # dem CPU-Early-Entrypoint unter Lock/Backup gefuehrt; hier entsteht nur
        # der leere, erlaubte Tabellen-Satz (AC: "exakt die erlaubten
        # Tabellen/Spalten aus dem Produktprofil").
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS captures (
                id TEXT PRIMARY KEY,
                audio_path TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'file',
                language TEXT,
                duration_ms INTEGER,
                transcript_raw TEXT NOT NULL DEFAULT '',
                transcript_refined TEXT,
                stt_model TEXT,
                llm_model TEXT,
                refinement_flags TEXT,
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS capture_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                stt_model TEXT NOT NULL DEFAULT 'turbo',
                language TEXT NOT NULL DEFAULT 'auto',
                auto_refine BOOLEAN NOT NULL DEFAULT 0,
                llm_model TEXT NOT NULL DEFAULT '0.6B',
                smart_cleanup BOOLEAN NOT NULL DEFAULT 1,
                self_correction BOOLEAN NOT NULL DEFAULT 1,
                preserve_technical BOOLEAN NOT NULL DEFAULT 1,
                allow_auto_paste BOOLEAN NOT NULL DEFAULT 1,
                default_playback_voice_id TEXT,
                hotkey_enabled BOOLEAN NOT NULL DEFAULT 0,
                chord_push_to_talk_keys JSON NOT NULL,
                chord_toggle_to_talk_keys JSON NOT NULL,
                updated_at TIMESTAMP
            );
            """
        )
        con.commit()
    finally:
        con.close()

    print(f"[schema-gate] leere konforme DB angelegt: {db_path}")
    return check(db_path)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("db", nargs="?", help="Pfad zur jf-whisper SQLite-DB")
    ap.add_argument("--create-empty", action="store_true",
                    help="leere, profilkonforme DB anlegen und pruefen")
    args = ap.parse_args(argv)

    if not args.db:
        ap.error("db-Pfad fehlt (oder --create-empty <pfad>)")
    db_path = Path(args.db)

    if args.create_empty:
        return create_empty(db_path)
    return check(db_path)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
