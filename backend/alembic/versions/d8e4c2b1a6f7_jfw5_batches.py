"""JFW-5: Batch-Auftragsmodell (batches, batch_items, batch_attempts)

Revision ID: d8e4c2b1a6f7
Revises: b7c9e1a3f5d8
Create Date: 2026-09-23

JFW-5 (Spec „Batch Result Contract" + „Batch Item Contract"):

- ``batches`` traegt je Zeile GENAU EINE unveraenderliche Snapshot-Revision
  (``identity_hash`` UNIQUE + ``payload_hash`` fail-closed): eingefrorene
  Auswahl samt Quellenidentitaeten/Inhaltsnachweisen, gemeinsame Profilrevision,
  Phasen-/Teilfehler-/Ressourcen-/Ausgabepolitik und eingefrorene Reihenfolge.
- ``batch_items`` bindet jedes Element an Quell-, Profil- und Snapshotrevision
  samt autoritativen Ergebnisreferenzen (``result_refs``) — kein Batchfehler
  loescht einen gueltigen Elementcommit.
- ``batch_attempts`` fuehrt je Element die nachvollziehbaren Versuche inklusive
  ``interrupted`` (fortsetzbar, nie fachlicher Erfolg). UNIQUE
  (batch, item, Versuchsnummer) verhindert doppelte Versuche bei konkurrierender
  Finalisierung/Cancel (Exactly-once-Ausgang ueber bedingte Transaktionen).

Schema-Kontrakt: jfwhisper-v10 -> jfwhisper-v11.
"""
import sqlalchemy as sa

from alembic import op

revision = "d8e4c2b1a6f7"
down_revision = "b7c9e1a3f5d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = set(inspector.get_table_names())

    if "batches" not in existing:
        op.create_table(
            "batches",
            sa.Column("id", sa.String(), nullable=False),  # uuid4 (Zeile)
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("batch_id", sa.String(), nullable=False),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("revision_no", sa.Integer(), nullable=False),
            sa.Column("parent_snapshot_hash", sa.String(), nullable=True),
            sa.Column("snapshot_hash", sa.String(), nullable=False),
            sa.Column("snapshot", sa.JSON(), nullable=False),
            sa.Column("profile", sa.JSON(), nullable=False),
            sa.Column("profile_hash", sa.String(), nullable=False),
            sa.Column("phases", sa.JSON(), nullable=False),
            sa.Column("partial_failure_policy", sa.String(), nullable=False),
            sa.Column("resource_policy", sa.JSON(), nullable=False),
            sa.Column("output_policy", sa.JSON(), nullable=False),
            sa.Column("frozen_order", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("aggregates", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("uq_batches_identity_hash", "batches",
                        ["identity_hash"], unique=True)
        op.create_index("ix_batches_batch_id", "batches", ["batch_id"])
        op.create_index("ix_batches_snapshot_hash", "batches", ["snapshot_hash"])

    if "batch_items" not in existing:
        op.create_table(
            "batch_items",
            sa.Column("id", sa.String(), nullable=False),  # uuid4 (Zeile)
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("batch_identity_hash", sa.String(), nullable=False),
            sa.Column("batch_id", sa.String(), nullable=False),
            sa.Column("snapshot_hash", sa.String(), nullable=False),
            sa.Column("item_id", sa.String(), nullable=False),
            sa.Column("order_index", sa.Integer(), nullable=False),
            sa.Column("source", sa.JSON(), nullable=False),
            sa.Column("relative_path", sa.String(), nullable=False),
            sa.Column("selection_refs", sa.JSON(), nullable=False),
            sa.Column("profile_hash", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("current_phase", sa.String(), nullable=True),
            sa.Column("phases", sa.JSON(), nullable=True),
            sa.Column("result_refs", sa.JSON(), nullable=True),
            sa.Column("warnings", sa.JSON(), nullable=True),
            sa.Column("output", sa.JSON(), nullable=True),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("uq_batch_items_identity_hash", "batch_items",
                        ["identity_hash"], unique=True)
        op.create_index("ix_batch_items_batch_identity_hash", "batch_items",
                        ["batch_identity_hash"])
        op.create_index("ix_batch_items_batch_id", "batch_items", ["batch_id"])
        op.create_index("ix_batch_items_item_id", "batch_items", ["item_id"])

    if "batch_attempts" not in existing:
        op.create_table(
            "batch_attempts",
            sa.Column("id", sa.String(), nullable=False),  # uuid4 (Zeile)
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("batch_identity_hash", sa.String(), nullable=False),
            sa.Column("batch_id", sa.String(), nullable=False),
            sa.Column("item_id", sa.String(), nullable=False),
            sa.Column("attempt_no", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("input_revisions", sa.JSON(), nullable=True),
            sa.Column("reused_commits", sa.JSON(), nullable=True),
            sa.Column("phase_states", sa.JSON(), nullable=True),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("batch_identity_hash", "item_id", "attempt_no",
                                name="uq_batch_attempts_item_no"),
        )
        op.create_index("uq_batch_attempts_attempt_id", "batch_attempts",
                        ["attempt_id"], unique=True)
        op.create_index("ix_batch_attempts_batch_identity_hash", "batch_attempts",
                        ["batch_identity_hash"])
        op.create_index("ix_batch_attempts_batch_id", "batch_attempts", ["batch_id"])
        op.create_index("ix_batch_attempts_item_id", "batch_attempts", ["item_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "batch_attempts" in tables:
        op.drop_index("ix_batch_attempts_item_id", table_name="batch_attempts")
        op.drop_index("ix_batch_attempts_batch_id", table_name="batch_attempts")
        op.drop_index("ix_batch_attempts_batch_identity_hash",
                      table_name="batch_attempts")
        op.drop_index("uq_batch_attempts_attempt_id", table_name="batch_attempts")
        op.drop_table("batch_attempts")
    if "batch_items" in tables:
        op.drop_index("ix_batch_items_item_id", table_name="batch_items")
        op.drop_index("ix_batch_items_batch_id", table_name="batch_items")
        op.drop_index("ix_batch_items_batch_identity_hash", table_name="batch_items")
        op.drop_index("uq_batch_items_identity_hash", table_name="batch_items")
        op.drop_table("batch_items")
    if "batches" in tables:
        op.drop_index("ix_batches_snapshot_hash", table_name="batches")
        op.drop_index("ix_batches_batch_id", table_name="batches")
        op.drop_index("uq_batches_identity_hash", table_name="batches")
        op.drop_table("batches")
