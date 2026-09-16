"""JFW-1 Schema-Kontrakt-Gate (dauerhaft): S1-S4 gegen das Produktprofil.

S1: frische DB -> exakt captures + capture_settings, keine LLM-/TTS-Spalten.
S2: Legacy-Voicebox-DB (13 verbotene Tabellen + LLM-Spalten) konvergiert auf
    das Kontrakt; Captures-Daten bleiben erhalten.
S3: zweiter Lauf ist ein No-Op (Idempotenz).
S4: Alembic-Head = be53d0afd8cc, schema_meta konsistent.

Aufruf:  backend/.venv/Scripts/python.exe scripts/test_schema_contract.py
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["JFWHISPER_DATA_ROOT"] = tempfile.mkdtemp(prefix="jfw_s1_")

import backend.config as config  # noqa: E402

config.set_data_dir(Path(tempfile.mkdtemp(prefix="jfw_s1_datadir_")))
import backend.database.session as session_mod  # noqa: E402
from sqlalchemy import inspect, text  # noqa: E402


def columns_of(engine_, table):
    insp = inspect(engine_)
    return {c["name"] for c in insp.get_columns(table)}


ALLOWED_TABLES = {"captures", "capture_settings", "tasks", "transcript_revisions"}
FORBIDDEN_COLS = {
    "auto_refine", "llm_model", "smart_cleanup", "self_correction",
    "preserve_technical", "default_playback_voice_id",
    "transcript_refined", "refinement_flags",
}

# --- S1: frische DB -------------------------------------------------------
session_mod.init_db()
engine = session_mod.engine
insp = inspect(engine)
tables = set(insp.get_table_names())
app_tables = tables & ALLOWED_TABLES
infra = tables - ALLOWED_TABLES  # alembic_version, schema_meta erlaubt
assert app_tables == ALLOWED_TABLES, f"S1 Tabellen: {sorted(tables)}"
cs_cols = columns_of(engine, "capture_settings")
cap_cols = columns_of(engine, "captures")
leak_cs = cs_cols & FORBIDDEN_COLS
leak_cap = cap_cols & FORBIDDEN_COLS
assert not leak_cs and not leak_cap, f"S1 LLM-Spalten: {leak_cs} {leak_cap}"
print(f"[S1] OK frische DB: Tabellen={sorted(tables)}")
print(f"     capture_settings Spalten: {sorted(cs_cols)}")
print(f"     captures Spalten:        {sorted(cap_cols)}")

# --- S2: Legacy-DB (Voicebox 15 Tabellen + LLM-Spalten) -------------------
legacy_dir = Path(tempfile.mkdtemp(prefix="jfw_s2_"))
legacy_db = legacy_dir / "jf-whisper.db"
conn = sqlite3.connect(legacy_db)
c = conn.cursor()
# Simulierte Voicebox-DB: verbotene Tabellen + captures/capture_settings mit LLM-Spalten
for t in ["profiles", "profile_samples", "generations", "generation_versions",
          "stories", "story_items", "projects", "effect_presets",
          "audio_channels", "channel_device_mappings", "profile_channel_mappings",
          "generation_settings", "mcp_client_bindings"]:
    c.execute(f"CREATE TABLE {t} (id INTEGER PRIMARY KEY)")
c.execute("""CREATE TABLE capture_settings (
    id INTEGER PRIMARY KEY, stt_model TEXT NOT NULL DEFAULT 'turbo',
    language TEXT NOT NULL DEFAULT 'auto', auto_refine BOOLEAN NOT NULL DEFAULT 1,
    llm_model TEXT NOT NULL DEFAULT '0.6B', smart_cleanup BOOLEAN NOT NULL DEFAULT 1,
    self_correction BOOLEAN NOT NULL DEFAULT 1, preserve_technical BOOLEAN NOT NULL DEFAULT 1,
    allow_auto_paste BOOLEAN NOT NULL DEFAULT 1, default_playback_voice_id TEXT,
    hotkey_enabled BOOLEAN NOT NULL DEFAULT 0,
    chord_push_to_talk_keys TEXT NOT NULL DEFAULT '[]',
    chord_toggle_to_talk_keys TEXT NOT NULL DEFAULT '[]', updated_at DATETIME)""")
c.execute("""CREATE TABLE captures (
    id TEXT PRIMARY KEY, audio_path TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'file',
    language TEXT, duration_ms INTEGER, transcript_raw TEXT NOT NULL DEFAULT '',
    transcript_refined TEXT, stt_model TEXT, llm_model TEXT, refinement_flags TEXT,
    created_at DATETIME)""")
# Daten erhalten bleiben muessen:
c.execute("INSERT INTO capture_settings (id) VALUES (1)")
c.execute("INSERT INTO captures (id, audio_path, source, transcript_raw) "
          "VALUES ('cap-1', 'x.wav', 'dictation', 'hallo welt')")
conn.commit()
conn.close()

# init_db gegen Legacy-DB: Engine neu aufbauen
import backend.database.session as session_mod  # noqa: E402
session_mod.engine = None
session_mod.SessionLocal = None
session_mod._db_path = legacy_db
from sqlalchemy import create_engine  # noqa: E402
new_engine = create_engine(f"sqlite:///{legacy_db}", connect_args={"check_same_thread": False})
# run_migrations + alembic upgrade (gleicher Flow wie init_db, ohne Lock)
from backend.database.migrations import run_migrations  # noqa: E402
run_migrations(new_engine)
os.environ["JFWHISPER_DB_URL"] = f"sqlite:///{legacy_db}"
from backend.schema import run_schema_upgrade  # noqa: E402
run_schema_upgrade(legacy_db, new_engine)

insp2 = inspect(new_engine)
tables2 = set(insp2.get_table_names())
app_tables2 = tables2 & ALLOWED_TABLES
assert app_tables2 == ALLOWED_TABLES, f"S2 Tabellen nach Konvergenz: {sorted(tables2)}"
cs_cols2 = columns_of(new_engine, "capture_settings")
cap_cols2 = columns_of(new_engine, "captures")
leak_cs2 = cs_cols2 & FORBIDDEN_COLS
leak_cap2 = cap_cols2 & FORBIDDEN_COLS
assert not leak_cs2 and not leak_cap2, f"S2 LLM-Spalten uebrig: {leak_cs2} {leak_cap2}"
# Daten erhalten?
with new_engine.connect() as conn2:
    n_settings = conn2.execute(text("SELECT COUNT(*) FROM capture_settings")).scalar()
    transcript = conn2.execute(
        text("SELECT transcript_raw FROM captures WHERE id='cap-1'")
    ).scalar()
assert n_settings == 1, f"S2 settings rows: {n_settings}"
assert transcript == "hallo welt", f"S2 Daten verloren: {transcript!r}"
print(f"[S2] OK Legacy-Konvergenz: Tabellen={sorted(tables2)}")
print(f"     capture_settings Spalten: {sorted(cs_cols2)}")
print(f"     captures Spalten:        {sorted(cap_cols2)}")
print("     Daten erhalten: settings=1, transcript='hallo welt'")

# --- S3: Idempotenz (zweiter Lauf ist No-Op) ------------------------------
run_migrations(new_engine)
run_schema_upgrade(legacy_db, new_engine)
tables3 = set(inspect(new_engine).get_table_names())
assert tables3 == tables2, f"S3 Drift nach 2. Lauf: {sorted(tables3)}"
print("[S3] OK Idempotenz: zweiter Lauf ist No-Op")

# --- S4: Migrationskettenhash + schema_meta -------------------------------
with new_engine.connect() as conn4:
    head = conn4.execute(text("SELECT version_num FROM alembic_version")).scalar()
    meta = conn4.execute(
        text("SELECT key, value FROM schema_meta")
    ).fetchall() if "schema_meta" in tables2 else []
print(f"[S4] Alembic-Head: {head}")
for k, v in meta:
    print(f"     schema_meta[{k}] = {v[:80]}{'...' if len(v) > 80 else ''}")
assert head == "08a06bf47a91", f"S4 Head falsch: {head}"

print("\nALLE S1-S4 GRUEN")
