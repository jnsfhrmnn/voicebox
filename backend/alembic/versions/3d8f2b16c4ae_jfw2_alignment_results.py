"""JFW-2: Alignment-Ergebnis (forced alignment) — Schema-Tabelle.

Revision ID: 3d8f2b16c4ae
Revises: 08a06bf47a91
Create Date: 2026-09-23

JFW-2 (Spec „Alignment Result Contract" + „Data And State Model"): Die Tabelle
``alignment_results`` traegt die eine, versionierte Alignment-Ergebnisebene je
Identitaet (job + audio + Transkriptrevision + Profil + Vertragsversion):

- ``identity_hash`` (UNIQUE) und ``payload_hash`` tragen Idempotenz und den
  fail-closed Provenienzkonflikt (gleiche Identitaet, abweichender Payload).
- Der Kopf bindet Audio-/Transkript-/Provenienz-/Vertrags-Hashes, Coverage-
  Zaehler und den kanonischen Ergebnis-Hash; ``result_hash`` gesetzt <=>
  atomarer Ergebnis-Commit.
- ``words`` haelt den Wortvertrag (exakte Substrings der gebundenen Revision;
  No-text-change-Vertrag).

Die Spalten entsprechen exakt ``backend/database/models.py`` (AlignmentResult).
Der Anlegen ist idempotent — DBs auf dem JFW-12-Head sind unabhaengig davon,
aeltere DBs konvergieren beim Upgrade. Keine TTS-/LLM-Tabelle und keine zweite
Runtime-Datenbank.
"""

import sqlalchemy as sa
from alembic import op

revision = "3d8f2b16c4ae"
down_revision = "08a06bf47a91"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "alignment_results" not in existing:
        op.create_table(
            "alignment_results",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("audio_asset_id", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("audio_duration_ms", sa.Integer(), nullable=False),
            sa.Column("timebase", sa.String(), nullable=False, server_default="audio_ms_v1"),
            sa.Column("transcript_run_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_id", sa.String(), nullable=False),
            sa.Column("transcript_revision_hash", sa.String(), nullable=False),
            sa.Column("language_ranges", sa.JSON(), nullable=False),
            sa.Column("alignment_profile", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("model_id", sa.String(), nullable=True),
            sa.Column("model_revision", sa.String(), nullable=True),
            sa.Column("model_sha256", sa.String(), nullable=True),
            sa.Column("model_license", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("words", sa.JSON(), nullable=True),
            sa.Column("coverage_alignable", sa.Integer(), nullable=True),
            sa.Column("coverage_aligned", sa.Integer(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_alignment_results_job_id", "alignment_results", ["job_id"])
        op.create_index(
            "uq_alignment_results_identity_hash",
            "alignment_results",
            ["identity_hash"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "alignment_results" in existing:
        op.drop_index("uq_alignment_results_identity_hash", table_name="alignment_results")
        op.drop_index("ix_alignment_results_job_id", table_name="alignment_results")
        op.drop_table("alignment_results")
