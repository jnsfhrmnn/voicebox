"""JFW-7: Transkriptions-Vertrag (transcription_runs + transcript_revisions-Erweiterung)

Revision ID: e7b3c9a1d5f4
Revises: c4a17f2e93ab
Create Date: 2026-09-23

JFW-7 (Spec „Processing Contract" + „Rohtranskript und Inhaltstreue"):

- ``transcription_runs`` traegt Run-Identitaet (identity_hash UNIQUE +
  payload_hash), den eingefrorenen Run-Snapshot, die sichtbare Attempt-Liste mit
  Backend-/Generations-Bindung und den Processing-Contract-Zustand. Genau ein
  terminaler Ausgang pro Attempt ueber bedingte Transaktionen.
- ``transcript_revisions`` wird um die Revisionstrennung erweitert (nullable —
  Altzeilen ohne ``revision_kind`` gelten als ``raw_transcript``):
  ``revision_kind`` (raw_transcript | user_edited), ``parent_revision_id``,
  ``text_hash``, ``segments``, ``provenance``, ``run_identity_hash``.

Schema-Kontrakt: jfwhisper-v6 -> jfwhisper-v7.
"""
import sqlalchemy as sa

from alembic import op

revision = "e7b3c9a1d5f4"
down_revision = "c4a17f2e93ab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {t["name"] for t in inspector.get_sorted_tables()} if hasattr(
        inspector, "get_sorted_tables"
    ) else set(inspector.get_table_names())

    if "transcription_runs" not in existing:
        op.create_table(
            "transcription_runs",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("source_kind", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("manifest_hash", sa.String(), nullable=True),
            sa.Column("capture_id", sa.String(), nullable=True),
            sa.Column("stop_reason", sa.String(), nullable=True),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("snapshot_hash", sa.String(), nullable=False),
            sa.Column("stt_model", sa.String(), nullable=True),
            sa.Column("model_revision", sa.String(), nullable=True),
            sa.Column("language_setting", sa.String(), nullable=True),
            sa.Column("backend_variant", sa.String(), nullable=True),
            sa.Column("backend_generation", sa.Integer(), nullable=True),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("attempts", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("revision_id", sa.String(), nullable=True),
            sa.Column("text_hash", sa.String(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_transcription_runs_run_id", "transcription_runs", ["run_id"])
        op.create_index(
            "uq_transcription_runs_identity_hash",
            "transcription_runs",
            ["identity_hash"],
            unique=True,
        )

    tr_cols = {c["name"] for c in inspector.get_columns("transcript_revisions")}
    with op.batch_alter_table("transcript_revisions") as batch_op:
        for name, coltype, nullable in (
            ("revision_kind", sa.String(), True),
            ("parent_revision_id", sa.String(), True),
            ("text_hash", sa.String(), True),
            ("segments", sa.JSON(), True),
            ("provenance", sa.JSON(), True),
            ("run_identity_hash", sa.String(), True),
        ):
            if name not in tr_cols:
                batch_op.add_column(sa.Column(name, coltype, nullable=nullable))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tr_cols = {c["name"] for c in inspector.get_columns("transcript_revisions")}
    with op.batch_alter_table("transcript_revisions") as batch_op:
        for name in (
            "run_identity_hash",
            "provenance",
            "segments",
            "text_hash",
            "parent_revision_id",
            "revision_kind",
        ):
            if name in tr_cols:
                batch_op.drop_column(name)
    tables = set(inspector.get_table_names())
    if "transcription_runs" in tables:
        op.drop_index("uq_transcription_runs_identity_hash", table_name="transcription_runs")
        op.drop_index("ix_transcription_runs_run_id", table_name="transcription_runs")
        op.drop_table("transcription_runs")
