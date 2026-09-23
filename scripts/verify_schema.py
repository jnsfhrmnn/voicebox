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
    load_profile()  # Profil muss lesbar sein (fail-closed bei Defekt)
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
                stt_model TEXT,
                created_at TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS capture_settings (
                id INTEGER PRIMARY KEY DEFAULT 1,
                stt_model TEXT NOT NULL DEFAULT 'turbo',
                language TEXT NOT NULL DEFAULT 'auto',
                allow_auto_paste BOOLEAN NOT NULL DEFAULT 1,
                hotkey_enabled BOOLEAN NOT NULL DEFAULT 0,
                chord_push_to_talk_keys JSON NOT NULL,
                chord_toggle_to_talk_keys JSON NOT NULL,
                updated_at TIMESTAMP
            );
            -- JFW-12 (Spec B7/C): Task-/Lease-Datenvertrag.
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                job_id TEXT NOT NULL,
                app_epoch TEXT NOT NULL,
                backend_generation INTEGER NOT NULL,
                backend_variant TEXT NOT NULL,
                model_contract_hash TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                cancel_requested_at TIMESTAMP,
                terminal_at TIMESTAMP,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_tasks_job_id ON tasks (job_id);
            CREATE TABLE IF NOT EXISTS transcript_revisions (
                id TEXT PRIMARY KEY,
                source_attempt_id TEXT NOT NULL,
                transcript_raw TEXT NOT NULL DEFAULT '',
                stt_model TEXT,
                language TEXT,
                duration_ms INTEGER,
                created_at TIMESTAMP
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_transcript_revisions_source_attempt_id
                ON transcript_revisions (source_attempt_id);
            -- JFW-2 (Spec Alignment Result Contract): Forced-Alignment-Ergebnis.
            CREATE TABLE IF NOT EXISTS alignment_results (
                id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_id TEXT,
                app_epoch TEXT,
                audio_asset_id TEXT NOT NULL,
                audio_hash TEXT NOT NULL,
                audio_duration_ms INTEGER NOT NULL,
                timebase TEXT NOT NULL DEFAULT 'audio_ms_v1',
                transcript_run_id TEXT NOT NULL,
                transcript_revision_id TEXT NOT NULL,
                transcript_revision_hash TEXT NOT NULL,
                language_ranges JSON NOT NULL,
                alignment_profile TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                model_id TEXT,
                model_revision TEXT,
                model_sha256 TEXT,
                model_license TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason_code TEXT,
                words JSON,
                coverage_alignable INTEGER,
                coverage_aligned INTEGER,
                result_hash TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                terminal_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_alignment_results_job_id
                ON alignment_results (job_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_alignment_results_identity_hash
                ON alignment_results (identity_hash);
            -- JFW-3 (Spec Diarization Result Contract): Diarisierungsergebnis.
            CREATE TABLE IF NOT EXISTS diarization_results (
                id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                job_id TEXT NOT NULL,
                attempt_id TEXT,
                app_epoch TEXT,
                audio_asset_id TEXT NOT NULL,
                audio_hash TEXT NOT NULL,
                audio_duration_ms INTEGER NOT NULL,
                timebase TEXT NOT NULL DEFAULT 'audio_ms_v1',
                transcript_run_id TEXT NOT NULL,
                transcript_revision_id TEXT NOT NULL,
                transcript_revision_hash TEXT NOT NULL,
                jfw2_reference_status TEXT NOT NULL,
                jfw2_result_hash TEXT,
                speaker_mode TEXT NOT NULL DEFAULT 'auto',
                speaker_count_min INTEGER,
                speaker_count_max INTEGER,
                diarization_profile TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                model_id TEXT,
                model_revision TEXT,
                model_sha256 TEXT,
                model_license TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason_code TEXT,
                clusters JSON,
                turns JSON,
                words JSON,
                coverage_speech_ms INTEGER,
                coverage_usable_ms INTEGER,
                cluster_count INTEGER,
                turn_count INTEGER,
                word_count INTEGER,
                word_assigned_count INTEGER,
                overlap_count INTEGER,
                uncertainty_count INTEGER,
                result_hash TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                terminal_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_diarization_results_job_id
                ON diarization_results (job_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_diarization_results_identity_hash
                ON diarization_results (identity_hash);
            -- JFW-11 (Spec Dual-Source Capture Contract): Meeting-Ergebnis.
            CREATE TABLE IF NOT EXISTS meeting_results (
                id TEXT PRIMARY KEY,
                identity_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                job_id TEXT NOT NULL,
                meeting_run_id TEXT NOT NULL,
                jfw6_run_reference TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                jfw2_result_hash TEXT,
                jfw3_result_hash TEXT,
                manifest_hash TEXT NOT NULL,
                timebase TEXT NOT NULL DEFAULT 'qpc_100ns',
                attempt_id TEXT,
                app_epoch TEXT,
                status TEXT NOT NULL DEFAULT 'queued',
                reason_code TEXT,
                stop_reason TEXT,
                tracks JSON,
                gaps JSON,
                sync JSON,
                sound_cue_marks JSON,
                recovery_status JSON,
                dedupe JSON,
                name_mappings JSON,
                result_hash TEXT,
                created_at TIMESTAMP,
                updated_at TIMESTAMP,
                terminal_at TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS ix_meeting_results_job_id
                ON meeting_results (job_id);
            CREATE UNIQUE INDEX IF NOT EXISTS uq_meeting_results_identity_hash
                ON meeting_results (identity_hash);
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
