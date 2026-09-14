"""JFW-1 initial schema: captures + capture_settings

Revision ID: a765a5987ab2
Revises: 
Create Date: 2026-09-13

Kanonicaler Start der JFW-1-Schema-Linie. Die Spalten entsprechen exakt
``backend/database/models.py`` (Capture, CaptureSettings). Legacy-TTS-/LLM-
Tabellen werden hier NICHT angelegt — sie sind aus dem Produkt entfernt und
werden von ``init_db()`` vor dem Upgrade gedroppt.
"""

from alembic import op
import sqlalchemy as sa

revision = "a765a5987ab2"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Idempotent: Legacy-Voicebox-DBs tragen ``captures``/``capture_settings``
    # bereits (mit Daten) — die werden nicht neu angelegt, sondern von den
    # Column-Migrationen in init_db() nachgezogen. Nur frische DBs bekommen
    # hier die Tabellen.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    # JFW-1: Legacy-TTS-/LLM-Tabellen der Voicebox-Baseline werden hier
    # kanonisch gedroppt (die reine Alembic-Linie --migrate-only laeuft ohne
    # init_db()). Die Liste ist identisch zu _FORBIDDEN_TABLES in
    # backend/database/migrations.py (Profil: database_tables.forbidden).
    # Der Drop ist idempotent und beruehrt captures/capture_settings nicht —
    # deren Daten bleiben erhalten.
    legacy_tables = [
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
    ]
    for table in legacy_tables:
        if table in existing:
            op.drop_table(table)

    if "capture_settings" not in existing:
        op.create_table(
            "capture_settings",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("stt_model", sa.String(), nullable=False),
            sa.Column("language", sa.String(), nullable=False),
            sa.Column("auto_refine", sa.Boolean(), nullable=False),
            sa.Column("llm_model", sa.String(), nullable=False),
            sa.Column("smart_cleanup", sa.Boolean(), nullable=False),
            sa.Column("self_correction", sa.Boolean(), nullable=False),
            sa.Column("preserve_technical", sa.Boolean(), nullable=False),
            sa.Column("allow_auto_paste", sa.Boolean(), nullable=False),
            sa.Column("default_playback_voice_id", sa.String(), nullable=True),
            sa.Column("hotkey_enabled", sa.Boolean(), nullable=False),
            sa.Column("chord_push_to_talk_keys", sa.JSON(), nullable=False),
            sa.Column("chord_toggle_to_talk_keys", sa.JSON(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
    if "captures" not in existing:
        op.create_table(
            "captures",
            sa.Column("id", sa.String(), nullable=False),
            sa.Column("audio_path", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("language", sa.String(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("transcript_raw", sa.Text(), nullable=False),
            sa.Column("transcript_refined", sa.Text(), nullable=True),
            sa.Column("stt_model", sa.String(), nullable=True),
            sa.Column("llm_model", sa.String(), nullable=True),
            sa.Column("refinement_flags", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table("captures")
    op.drop_table("capture_settings")
