"""JFW-3: Diarisierungsergebnis (speaker diarization) — Schema-Tabelle.

Revision ID: 9f4d2a7be511
Revises: 3d8f2b16c4ae
Create Date: 2026-09-23

JFW-3 (Spec „Diarization Result Contract" + „Data And State Model"): Die Tabelle
``diarization_results`` traegt die eine, versionierte Diarisierungs-Ergebnisebene
je Identitaet (job + audio + Transkriptrevision + JFW-2-Referenz + Profil +
Sprecheranzahl + Vertragsversion):

- ``identity_hash`` (UNIQUE) und ``payload_hash`` tragen Idempotenz und den
  fail-closed Provenienzkonflikt (gleiche Identitaet, abweichender Payload).
- Der Kopf bindet Job-/Audio-/Transkript-/JFW-2-Referenz-/Provenienz-/Vertrags-
  Hashes, Sprecheranzahlmodus, Coverage-/Cluster-/Turn-/Wort-/Overlap-/Unsicher-
  heitszaehler und den kanonischen Ergebnis-Hash; ``result_hash`` gesetzt <=>
  atomarer Ergebnis-Commit.
- ``clusters``/``turns``/``words`` halten ausschliesslich neutrale
  Sprecherstruktur (Cluster, Turns, Overlap-/Unsicherheitsstatus, reines
  Wort-Overlay) — keine Text- oder Zeitmutation gegenueber JFW-2.

Die Spalten entsprechen exakt ``backend/database/models.py`` (DiarizationResult).
Der Anlegen ist idempotent — DBs auf dem JFW-2-Head sind unabhaengig davon,
aeltere DBs konvergieren beim Upgrade. Keine TTS-/LLM-Tabelle und keine zweite
Runtime-Datenbank.
"""
import sqlalchemy as sa

from alembic import op

revision = "9f4d2a7be511"
down_revision = "3d8f2b16c4ae"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "diarization_results" not in existing:
        op.create_table(
            "diarization_results",
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
            sa.Column("jfw2_reference_status", sa.String(), nullable=False),
            sa.Column("jfw2_result_hash", sa.String(), nullable=True),
            sa.Column("speaker_mode", sa.String(), nullable=False, server_default="auto"),
            sa.Column("speaker_count_min", sa.Integer(), nullable=True),
            sa.Column("speaker_count_max", sa.Integer(), nullable=True),
            sa.Column("diarization_profile", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("model_id", sa.String(), nullable=True),
            sa.Column("model_revision", sa.String(), nullable=True),
            sa.Column("model_sha256", sa.String(), nullable=True),
            sa.Column("model_license", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("clusters", sa.JSON(), nullable=True),
            sa.Column("turns", sa.JSON(), nullable=True),
            sa.Column("words", sa.JSON(), nullable=True),
            sa.Column("coverage_speech_ms", sa.Integer(), nullable=True),
            sa.Column("coverage_usable_ms", sa.Integer(), nullable=True),
            sa.Column("cluster_count", sa.Integer(), nullable=True),
            sa.Column("turn_count", sa.Integer(), nullable=True),
            sa.Column("word_count", sa.Integer(), nullable=True),
            sa.Column("word_assigned_count", sa.Integer(), nullable=True),
            sa.Column("overlap_count", sa.Integer(), nullable=True),
            sa.Column("uncertainty_count", sa.Integer(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_diarization_results_job_id", "diarization_results", ["job_id"])
        op.create_index(
            "uq_diarization_results_identity_hash",
            "diarization_results",
            ["identity_hash"],
            unique=True,
        )

def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "diarization_results" in existing:
        op.drop_index("uq_diarization_results_identity_hash", table_name="diarization_results")
        op.drop_index("ix_diarization_results_job_id", table_name="diarization_results")
        op.drop_table("diarization_results")
