"""JFW-11: Dual-Source-Meeting-Ergebnis (meeting_results) — Schema-Tabelle.

Revision ID: 5b1f7c09e6a4
Revises: 9f4d2a7be511
Create Date: 2026-09-23

JFW-11 (Spec „Dual-Source Capture Contract" + „Data And State Model"): Die
Tabelle ``meeting_results`` traegt die eine, versionierte Ergebnisebene je
Identitaet (job + meeting-run + JFW-6-Run-Referenz + Vertragsversion):

- ``identity_hash`` (UNIQUE) und ``payload_hash`` tragen Idempotenz und den
  fail-closed Provenienzkonflikt (gleiche Identitaet, abweichender Payload —
  z. B. abweichende Track- oder Manifest-Hashes).
- Der Kopf bindet Meeting-Run-ID, JFW-6-Run-Referenz, Vertragsversion,
  JFW-2-/JFW-3-Revisionen und den Manifest-Hash; ``result_hash`` gesetzt <=>
  atomarer Ergebnis-Commit.
- ``tracks``/``gaps``/``sync``/``sound_cue_marks``/``recovery_status`` halten
  den Dual-Source-Capture-Vertrag (zwei getrennte autoritative Rohspuren,
  gemeinsame Zeitbasis ``qpc_offset_drift_v1``, Lücken, Sound-Cue-Marken,
  Recovery-Teilstatus); ``dedupe``/``name_mappings`` sind revisionsgebundene
  Annotationen auf unveraenderten JFW-2-/JFW-3-Ergebnissen.

Die Spalten entsprechen exakt ``backend/database/models.py`` (MeetingResult).
Der Anlegen ist idempotent — DBs auf dem JFW-3-Head sind unabhaengig davon,
aeltere DBs konvergieren beim Upgrade. Keine TTS-/LLM-Tabelle und keine zweite
Runtime-Datenbank.
"""
import sqlalchemy as sa

from alembic import op

revision = "5b1f7c09e6a4"
down_revision = "9f4d2a7be511"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "meeting_results" not in existing:
        op.create_table(
            "meeting_results",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("job_id", sa.String(), nullable=False),
            sa.Column("meeting_run_id", sa.String(), nullable=False),
            sa.Column("jfw6_run_reference", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("jfw2_result_hash", sa.String(), nullable=True),
            sa.Column("jfw3_result_hash", sa.String(), nullable=True),
            sa.Column("manifest_hash", sa.String(), nullable=False),
            sa.Column("timebase", sa.String(), nullable=False, server_default="qpc_100ns"),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="queued"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("stop_reason", sa.String(), nullable=True),
            sa.Column("tracks", sa.JSON(), nullable=True),
            sa.Column("gaps", sa.JSON(), nullable=True),
            sa.Column("sync", sa.JSON(), nullable=True),
            sa.Column("sound_cue_marks", sa.JSON(), nullable=True),
            sa.Column("recovery_status", sa.JSON(), nullable=True),
            sa.Column("dedupe", sa.JSON(), nullable=True),
            sa.Column("name_mappings", sa.JSON(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_meeting_results_job_id", "meeting_results", ["job_id"])
        op.create_index(
            "uq_meeting_results_identity_hash",
            "meeting_results",
            ["identity_hash"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "meeting_results" in existing:
        op.drop_index("uq_meeting_results_identity_hash", table_name="meeting_results")
        op.drop_index("ix_meeting_results_job_id", table_name="meeting_results")
        op.drop_table("meeting_results")
