"""JFW-12: Task-/Lease-Datenvertrag (tasks + transcript_revisions)

Revision ID: 08a06bf47a91
Revises: be53d0afd8cc
Create Date: 2026-09-15

JFW-12 (Spec B7/C, Data And State Model): Die JFW-1-reservierten Tabellen
``tasks`` und ``transcript_revisions`` werden konkretisiert. Sie bilden die
Exactly-once-Basis für Drain/Cancel/Finalisierung beim CPU-/CUDA-Backendwechsel:

- ``tasks`` trägt den Attempt-Zustand (Job/Attempt/Epoche/Generation/Vertrag/
  Status/Cancel-/Terminalzeit). Ein Attempt bindet ``job_id``, ``id``
  (= attempt_id), ``app_epoch``, ``backend_generation``,
  ``model_contract_hash`` und ``input_hash``.
- ``transcript_revisions`` hält das genau-einmal autoritative Rohresultat;
  ``source_attempt_id`` ist eindeutig, damit konkurrierende Finalisierung/Cancel
  keine doppelte Rohrevision erzeugen können.

Die Spalten entsprechen exakt ``backend/database/models.py`` (TaskAttempt,
TranscriptRevision). Der Anlegen ist idempotent — DBs auf dem JFW-12-Head sind
unbetroffen, ältere DBs konvergieren beim Upgrade. Keine TTS-/LLM-Tabelle und
keine zweite Runtime-Datenbank (Spec C).
"""

import sqlalchemy as sa
from alembic import op

revision = "08a06bf47a91"
down_revision = "be53d0afd8cc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "tasks" not in existing:
        op.create_table(
            "tasks",
            sa.Column("id", sa.String(), nullable=False),  # attempt_id (uuid4)
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("app_epoch", sa.String(), nullable=False),
            sa.Column("backend_generation", sa.Integer(), nullable=False),
            sa.Column("backend_variant", sa.String(), nullable=False),
            sa.Column("model_contract_hash", sa.String(), nullable=False),
            sa.Column("input_hash", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_tasks_job_id", "tasks", ["job_id"])

    if "transcript_revisions" not in existing:
        op.create_table(
            "transcript_revisions",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("source_attempt_id", sa.String(), nullable=False),
            sa.Column("transcript_raw", sa.Text(), nullable=False, server_default=""),
            sa.Column("stt_model", sa.String(), nullable=True),
            sa.Column("language", sa.String(), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        # Eindeutige Attempt-Referenz: genau einmal autoritativ (Spec B7).
        op.create_index(
            "uq_transcript_revisions_source_attempt_id",
            "transcript_revisions",
            ["source_attempt_id"],
            unique=True,
        )


def downgrade() -> None:
    # Downgrade ist kein Zielpfad (Produkt fährt nur vorwärts); die Drops sind
    # idempotent und berühren captures/capture_settings nicht.
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "transcript_revisions" in existing:
        op.drop_index(
            "uq_transcript_revisions_source_attempt_id",
            table_name="transcript_revisions",
        )
        op.drop_table("transcript_revisions")
    if "tasks" in existing:
        op.drop_index("ix_tasks_job_id", table_name="tasks")
        op.drop_table("tasks")
