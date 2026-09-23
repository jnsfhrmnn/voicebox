"""JFW-13: Protokoll- und Register-Vertrag (minutes_results, pseudonym_registers)

Revision ID: b7c9e1a3f5d8
Revises: a91f3d7c26b8
Create Date: 2026-09-23

JFW-13 (Spec „Protokoll Result Contract" + „Pseudonymisierungsvertrag"):

- ``minutes_results`` traegt den deterministischen ``minutes_key`` (UNIQUE,
  bindet alle Eingangsrevisionen UND die bestaetigte Registerrevision) plus
  ``payload_hash`` mit Zielidentitaet, die gebundenen JFW-2-/JFW-3-/JFW-4-/
  (optionalen) JFW-11-Ergebnis-Hashes, Modell-/Artefaktprovenienz, Status-
  Maschine, Warnliste, das Protokolldokument, ``result_hash`` und die
  Nondeterminismus-Revisionsliste. Genau ein terminaler Ausgang pro Attempt
  ueber bedingte Transaktionen.
- ``pseudonym_registers`` ist die getrennte, revisionierte Zuordnungsinformation
  (Parent-Referenz, UNIQUE (register_id, revision)), ausdruecklich loeschbar.

Schema-Kontrakt: jfwhisper-v9 -> jfwhisper-v10.
"""
import sqlalchemy as sa

from alembic import op

revision = "b7c9e1a3f5d8"
down_revision = "a91f3d7c26b8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "minutes_results" not in existing:
        op.create_table(
            "minutes_results",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("minutes_key", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("minutes_profile", sa.String(), nullable=False),
            sa.Column("register_id", sa.String(), nullable=False),
            sa.Column("register_revision", sa.String(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("audio_asset_id", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("audio_duration_ms", sa.Integer(), nullable=False),
            sa.Column("timebase", sa.String(), nullable=False),
            sa.Column("transcript_run_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_hash", sa.String(), nullable=False),
            sa.Column("transcript_text_hash", sa.String(), nullable=False),
            sa.Column("jfw2_result_hash", sa.String(), nullable=False),
            sa.Column("jfw2_status", sa.String(), nullable=False),
            sa.Column("jfw3_result_hash", sa.String(), nullable=False),
            sa.Column("jfw3_status", sa.String(), nullable=False),
            sa.Column("jfw4_export_key", sa.String(), nullable=False),
            sa.Column("jfw4_result_hash", sa.String(), nullable=False),
            sa.Column("jfw11_commit_hash", sa.String(), nullable=True),
            sa.Column("jfw11_status", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("readiness", sa.JSON(), nullable=True),
            sa.Column("warnings", sa.JSON(), nullable=True),
            sa.Column("model_provenance", sa.JSON(), nullable=True),
            sa.Column("document", sa.JSON(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("nondeterminism_revisions", sa.JSON(), nullable=True),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_minutes_results_job_id", "minutes_results", ["job_id"])
        op.create_index("ix_minutes_results_register_id", "minutes_results",
                        ["register_id"])
        op.create_index("uq_minutes_results_minutes_key", "minutes_results",
                        ["minutes_key"], unique=True)

    if "pseudonym_registers" not in existing:
        op.create_table(
            "pseudonym_registers",
            sa.Column("id", sa.String(), nullable=False),  # uuid4 (Zeile)
            sa.Column("register_id", sa.String(), nullable=False),
            sa.Column("revision", sa.Integer(), nullable=False),
            sa.Column("revision_id", sa.String(), nullable=False),
            sa.Column("parent_revision_id", sa.String(), nullable=True),
            sa.Column("minutes_key", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("entries", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("register_id", "revision",
                                name="uq_pseudonym_registers_rev"),
        )
        op.create_index("ix_pseudonym_registers_register_id", "pseudonym_registers",
                        ["register_id"])
        op.create_index("ix_pseudonym_registers_minutes_key", "pseudonym_registers",
                        ["minutes_key"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "pseudonym_registers" in tables:
        op.drop_index("ix_pseudonym_registers_minutes_key",
                      table_name="pseudonym_registers")
        op.drop_index("ix_pseudonym_registers_register_id",
                      table_name="pseudonym_registers")
        op.drop_table("pseudonym_registers")
    if "minutes_results" in tables:
        op.drop_index("uq_minutes_results_minutes_key", table_name="minutes_results")
        op.drop_index("ix_minutes_results_register_id", table_name="minutes_results")
        op.drop_index("ix_minutes_results_job_id", table_name="minutes_results")
        op.drop_table("minutes_results")
