"""Column-level migrations for the JF Whisper SQLite database.

Why not Alembic?  voicebox is a single-user desktop app shipping as a
PyInstaller binary.  Every user has exactly one SQLite file.  Alembic's
strengths -- migration tracking across environments, rollback, team
coordination -- don't apply here and would add bundling complexity
(alembic.ini, env.py, versions/ directory all need to survive
PyInstaller).  The column-existence checks below are idempotent, run in
<50 ms on startup, and have worked reliably across 12 schema changes.
If the project ever moves to a server-based deployment or Postgres, this
decision should be revisited.

JFW-1 (Transkriptions-Produktprofil): Die TTS-/LLM-Migrationen
(story_items, profiles, generations, effect_presets, generation_versions,
mcp_client_bindings) sind entfernt -- die zugehoerigen Tabellen gehoeren
nicht zum Schema-Kontrakt.

Adding a new migration:
    1. Append a new ``_migrate_*`` helper at the bottom of this file.
    2. Call it from ``run_migrations()`` in the appropriate spot.
    3. The helper should check column/table existence before acting
       (idempotent) and print a short message when it does real work.
"""

import json
import logging
import sqlite3

from sqlalchemy import inspect, text

from ..utils.capture_chords import (
    default_push_to_talk_chord,
    default_toggle_to_talk_chord,
)

logger = logging.getLogger(__name__)


# JFW-1 Schema-Kontrakt (Profil: product-profiles/jf-whisper.json ->
# database_tables.forbidden): Diese Tabellen gehoeren nicht zum
# Transkriptionsprodukt. Sie existieren nur noch in Legacy-Datenbanken aus
# Voicebox-Läufen; der Code hat keine Live-Consumer mehr, daher werden sie
# beim Start entfernt, damit jede DB gegen das Kontrakt konvergiert.
_FORBIDDEN_TABLES = (
    "profiles",
    "profile_samples",
    "generations",
    "generation_versions",
    "stories",
    "story_items",
    "projects",
    "effect_presets",
    "audio_channels",
    "channel_device_mappings",
    "profile_channel_mappings",
    "generation_settings",
    "mcp_client_bindings",
)


def run_migrations(engine) -> None:
    """Run all schema migrations.  Safe to call on every startup."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    _drop_forbidden_tables(engine, tables)
    _migrate_capture_settings(engine, inspector, tables)


# -- helpers ---------------------------------------------------------------

def _get_columns(inspector, table: str) -> set[str]:
    return {col["name"] for col in inspector.get_columns(table)}


def _add_column(engine, table: str, column_sql: str, label: str) -> None:
    """Add a column if it doesn't already exist."""
    with engine.connect() as conn:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column_sql}"))
        conn.commit()
    logger.info("Added %s column to %s", label, table)


def _drop_forbidden_tables(engine, tables: set[str]) -> None:
    """Drop legacy TTS/LLM tables so the DB converges to the JFW-1 contract.

    Idempotent: nur Tabellen, die tatsächlich existieren, werden entfernt.
    Die erlaubten Tabellen (captures, capture_settings) bleiben unangetastet;
    ihre Daten sind von den Drops nicht betroffen (keine FK-Beziehungen zu
    den verbotenen Tabellen im Live-Schema).
    """
    present = [t for t in _FORBIDDEN_TABLES if t in tables]
    if not present:
        return
    with engine.connect() as conn:
        for table in present:
            # FK-Referenzen aus Legacy-Tabellen unterbinden das Drop nicht,
            # weil SQLite sie nur bei PRAGMA foreign_keys=ON prüft -- und wir
            # hier die gesamte verbotene Menge entfernen. Reihenfolge ist
            # egal, da alle referenzierenden Tabellen ebenfalls dropped werden.
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        conn.commit()
    logger.info("Dropped %d legacy TTS/LLM table(s): %s", len(present), ", ".join(sorted(present)))


# -- per-table migrations --------------------------------------------------

def _migrate_capture_settings(engine, inspector, tables: set[str]) -> None:
    if "capture_settings" not in tables:
        return
    columns = _get_columns(inspector, "capture_settings")
    push_default = json.dumps(default_push_to_talk_chord())
    toggle_default = json.dumps(default_toggle_to_talk_chord())
    if "allow_auto_paste" not in columns:
        _add_column(
            engine,
            "capture_settings",
            "allow_auto_paste BOOLEAN NOT NULL DEFAULT 1",
            "allow_auto_paste",
        )
    if "default_playback_voice_id" not in columns:
        _add_column(
            engine,
            "capture_settings",
            "default_playback_voice_id VARCHAR",
            "default_playback_voice_id",
        )
    if "chord_push_to_talk_keys" not in columns:
        _add_column(
            engine,
            "capture_settings",
            f"chord_push_to_talk_keys TEXT NOT NULL DEFAULT '{push_default}'",
            "chord_push_to_talk_keys",
        )
    if "chord_toggle_to_talk_keys" not in columns:
        _add_column(
            engine,
            "capture_settings",
            f"chord_toggle_to_talk_keys TEXT NOT NULL DEFAULT '{toggle_default}'",
            "chord_toggle_to_talk_keys",
        )
    if "hotkey_enabled" not in columns:
        _add_column(
            engine,
            "capture_settings",
            "hotkey_enabled BOOLEAN NOT NULL DEFAULT 0",
            "hotkey_enabled",
        )
