"""add admins.parent_id (the master reseller an admin belongs to)

Revision ID: a7c3d91e5b20
Revises: e2a21af8f26e
Create Date: 2026-10-12 00:00:00.000000

Additive: one nullable column, an index and a self-referencing foreign key (SET NULL).
"""

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

# revision identifiers, used by Alembic.
revision = "a7c3d91e5b20"
down_revision = "e2a21af8f26e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("admins") as batch_op:
        batch_op.add_column(sa.Column("parent_id", SqliteCompatibleBigInteger(), nullable=True))
        batch_op.create_index("ix_admins_parent_id", ["parent_id"])
        batch_op.create_foreign_key("fk_admins_parent_id_admins", "admins", ["parent_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("admins") as batch_op:
        batch_op.drop_constraint("fk_admins_parent_id_admins", type_="foreignkey")
        batch_op.drop_index("ix_admins_parent_id")
        batch_op.drop_column("parent_id")
