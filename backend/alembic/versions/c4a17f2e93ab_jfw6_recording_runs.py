"""JFW-6: Diktat-Aufnahme-Run (``recording_runs``) — Schema-Tabelle.

Revision ID: c4a17f2e93ab
Revises: 5b1f7c09e6a4
Create Date: 2026-09-23

JFW-6 (Spec „State Contract" + „Data And State Model"): Die Tabelle
``recording_runs`` traegt genau einen autoritativen Diktat-Run mit stabiler
Identität:

- ``identity_hash`` (UNIQUE) und ``payload_hash`` tragen Idempotenz und den
  fail-closed Provenienzkonflikt (gleiche Identität, abweichender Payload).
- ``run_id`` (``jfw6-run-<uuid4hex>``) ist ueber den gesamten Lebenszyklus
  inklusive Recovery stabil; ``started_at_100ns``/``stop_at_100ns`` tragen
  den monotonen Stopp-Intent (genau einer, mit Ursache).
- ``format`` bindet das tatsaechlich geoeffnete Mikrofonformat;
  ``device_stable_id_hash`` die stabile Geraeteidentität ausschliesslich als
  Hash (nie die volle Identität in Logs).
- ``manifest``/``frames``/``gaps``/``sound_cue_marks``/``recovery_status``
  halten den Run-Vertrag (Frame-Bilanz, Luecken, Sound-Cue-Marken,
  Recovery-Grenzen); ``result_hash`` gesetzt <=> atomarer Ergebnis-Commit.
- ``transcription_authorized``/``handoff_count`` sichern den idempotenten
  JFW-7-Handoff (hoechstens eine Transkriptionsautorisierung),
  ``deletion_contract_hash`` den gebundenen Löschvertrag (``Verwerfen``).

Die Spalten entsprechen exakt ``backend/database/models.py`` (RecordingRun).
Der Anlegen ist idempotent — DBs auf dem JFW-11-Head sind unabhaengig davon,
aeltere DBs konvergieren beim Upgrade. Keine TTS-/LLM-Tabelle und keine
zweite Runtime-Datenbank.
"""
import sqlalchemy as sa

from alembic import op

revision = "c4a17f2e93ab"
down_revision = "5b1f7c09e6a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())

    if "recording_runs" not in existing:
        op.create_table(
            "recording_runs",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("device_stable_id_hash", sa.String(), nullable=False),
            sa.Column("format", sa.JSON(), nullable=False),
            sa.Column("started_at_100ns", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="starting"),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("stop_reason", sa.String(), nullable=True),
            sa.Column("stop_cause", sa.String(), nullable=True),
            sa.Column("stop_at_100ns", sa.Integer(), nullable=True),
            sa.Column("audio_hash", sa.String(), nullable=True),
            sa.Column("manifest_hash", sa.String(), nullable=True),
            sa.Column("result_hash", sa.String(), nullable=True),
            sa.Column("manifest", sa.JSON(), nullable=True),
            sa.Column("frames", sa.JSON(), nullable=True),
            sa.Column("gaps", sa.JSON(), nullable=True),
            sa.Column("sound_cue_marks", sa.JSON(), nullable=True),
            sa.Column("recovery_status", sa.JSON(), nullable=True),
            sa.Column(
                "transcription_authorized", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("handoff_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deletion_contract_hash", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_recording_runs_run_id", "recording_runs", ["run_id"])
        op.create_index(
            "uq_recording_runs_identity_hash",
            "recording_runs",
            ["identity_hash"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "recording_runs" in existing:
        op.drop_index("uq_recording_runs_identity_hash", table_name="recording_runs")
        op.drop_index("ix_recording_runs_run_id", table_name="recording_runs")
        op.drop_table("recording_runs")
