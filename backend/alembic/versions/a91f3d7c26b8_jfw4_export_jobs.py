"""JFW-4: Export-Vertrag (export_jobs)
Revision ID: a91f3d7c26b8
Revises: f3b8c2d1a6e4
Create Date: 2026-09-23

JFW-4 (Spec „Export Result Contract" + „Format Contract"):

- ``export_jobs`` traegt den deterministischen Export-Schluessel
  (``export_key`` UNIQUE, ohne Zielbindung) plus ``payload_hash`` mit
  Zielidentitaet/erwarteten Dateinamen, die gebundenen JFW-2-/JFW-3-/
  (optionalen) JFW-11-Ergebnis-Hashes, die Exportoptionen, die Readiness
  inklusive Warnungen, das Set-Manifest (Inhalts-Hash je Praesentationsdatei)
  und den Set-``result_hash``. Genau ein terminaler Ausgang pro Attempt ueber
  bedingte Transaktionen; Revisionswechsel markieren alte Nachweise
  ``invalidiert`` (Nutzerdateien bleiben unveraendert).

Schema-Kontrakt: jfwhisper-v8 -> jfwhisper-v9.
"""
import sqlalchemy as sa

from alembic import op

revision = "a91f3d7c26b8"
down_revision = "f3b8c2d1a6e4"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {t["name"] for t in inspector.get_sorted_tables()} if hasattr(
        inspector, "get_sorted_tables"
    ) else set(inspector.get_table_names())

    if "export_jobs" not in existing:
        op.create_table(
            "export_jobs",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("export_key", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("audio_asset_id", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("audio_duration_ms", sa.Integer(), nullable=False),
            sa.Column("timebase", sa.String(), nullable=False),
            sa.Column("transcript_run_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_hash", sa.String(), nullable=False),
            sa.Column("jfw2_result_hash", sa.String(), nullable=False),
            sa.Column("jfw2_status", sa.String(), nullable=False),
            sa.Column("jfw3_result_hash", sa.String(), nullable=False),
            sa.Column("jfw3_status", sa.String(), nullable=False),
            sa.Column("jfw11_commit_hash", sa.String(), nullable=True),
            sa.Column("jfw11_status", sa.String(), nullable=True),
            sa.Column("formats", sa.JSON(), nullable=False),
            sa.Column("export_profile", sa.String(), nullable=False),
            sa.Column("name_policy", sa.String(), nullable=False),
            sa.Column("partial_mode", sa.String(), nullable=True),
            sa.Column("partial_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("target_dir", sa.String(), nullable=True),
            sa.Column("expected_files", sa.JSON(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="preparing"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("readiness", sa.JSON(), nullable=True),
            sa.Column("manifest", sa.JSON(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_export_jobs_job_id",
            "export_jobs",
            ["job_id"],
        )
        op.create_index(
            "uq_export_jobs_export_key",
            "export_jobs",
            ["export_key"],
            unique=True,
        )

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "export_jobs" in tables:
        op.drop_index("uq_export_jobs_export_key", table_name="export_jobs")
        op.drop_index("ix_export_jobs_job_id", table_name="export_jobs")
        op.drop_table("export_jobs")
