"""JFW-8: Delivery-Vertrag (delivery_operations)

Revision ID: f3b8c2d1a6e4
Revises: e7b3c9a1d5f4
Create Date: 2026-09-23

JFW-8 (Spec „Delivery State Contract" + „Recovery und manuelle
Wiederverwendung"):

- ``delivery_operations`` traegt Operations-Identitaet (identity_hash UNIQUE +
  payload_hash), die JFW-7-Bindung (Run-, Audio-, Attempt-, Revision-ID,
  text_hash), den eingefrorenen Ziel-Snapshot, den Delivery-State-Contract-
  Zustand mit ``auto_attempt_consumed``-Einmalbudget, den benutzergebundenen
  begrenzten Recovery-Rohtext sowie die inhaltsfreie Fehlerpfad-Spur.
  Genau ein terminaler Ausgang pro Operation ueber bedingte Transaktionen;
  die Loeschung entfernt Text und Zwischenstaende gemeinsam (Tombstone bleibt).

Schema-Kontrakt: jfwhisper-v7 -> jfwhisper-v8.
"""
import sqlalchemy as sa

from alembic import op

revision = "f3b8c2d1a6e4"
down_revision = "e7b3c9a1d5f4"
branch_labels = None
depends_on = None

def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {t["name"] for t in inspector.get_sorted_tables()} if hasattr(
        inspector, "get_sorted_tables"
    ) else set(inspector.get_table_names())

    if "delivery_operations" not in existing:
        op.create_table(
            "delivery_operations",
            sa.Column("id", sa.String(), nullable=False),  # uuid4
            sa.Column("identity_hash", sa.String(), nullable=False),
            sa.Column("payload_hash", sa.String(), nullable=False),
            sa.Column("delivery_operation_id", sa.String(), nullable=False),
            sa.Column("parent_operation_id", sa.String(), nullable=True),
            sa.Column("contract_version", sa.String(), nullable=False),
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("audio_hash", sa.String(), nullable=False),
            sa.Column("jfw7_attempt_id", sa.String(), nullable=True),
            sa.Column("revision_id", sa.String(), nullable=True),
            sa.Column("text_hash", sa.String(), nullable=False),
            sa.Column("target_snapshot", sa.JSON(), nullable=True),
            sa.Column("target_confirmed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("capability", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="received"),
            sa.Column("auto_attempt_consumed", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("attempt_intent", sa.JSON(), nullable=True),
            sa.Column("attempt_id", sa.String(), nullable=True),
            sa.Column("app_epoch", sa.String(), nullable=True),
            sa.Column("reason_code", sa.String(), nullable=True),
            sa.Column("recovery_text", sa.Text(), nullable=True),
            sa.Column("recovery_expires_at", sa.DateTime(), nullable=True),
            sa.Column("error_trace", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=True),
            sa.Column("terminal_at", sa.DateTime(), nullable=True),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_delivery_operations_delivery_operation_id",
            "delivery_operations",
            ["delivery_operation_id"],
        )
        op.create_index(
            "uq_delivery_operations_identity_hash",
            "delivery_operations",
            ["identity_hash"],
            unique=True,
        )

def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "delivery_operations" in tables:
        op.drop_index(
            "uq_delivery_operations_identity_hash", table_name="delivery_operations"
        )
        op.drop_index(
            "ix_delivery_operations_delivery_operation_id",
            table_name="delivery_operations",
        )
        op.drop_table("delivery_operations")
