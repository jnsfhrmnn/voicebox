"""${message}

Revision ID: ${rev}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from alembic import op
import sqlalchemy as sa
${imports if false else ""}

revision = ${repr(rev)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if false else "pass"}


def downgrade() -> None:
    ${downgrades if false else "pass"}
