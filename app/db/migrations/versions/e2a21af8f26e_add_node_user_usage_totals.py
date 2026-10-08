"""add node_user_usage_totals rollup for the global users-usage chart

Revision ID: e2a21af8f26e
Revises: 48a6bcb8bba1
Create Date: 2026-10-08 00:00:00.000000

node_user_usage_totals holds SUM(node_user_usages.used_traffic) per (created_at bucket, node_id)
over rows whose user exists. It is maintained on write by the record job and by every delete path,
and the unfiltered users-usage chart reads it instead of scanning node_user_usages.

The backfill aggregates node_user_usages one UTC day at a time with a plain SELECT (a non-locking
read on InnoDB) and inserts the few thousand resulting rows per day, so no long INSERT ... SELECT
holds locks on node_user_usages.
"""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

from app.db.compiles_types import SqliteCompatibleBigInteger

# revision identifiers, used by Alembic.
revision = "e2a21af8f26e"
down_revision = "48a6bcb8bba1"
branch_labels = None
depends_on = None

TABLE_NAME = "node_user_usage_totals"
INSERT_BATCH_SIZE = 5_000


def _utc_day_start(value: datetime) -> datetime:
    """Midnight UTC of ``value``; naive values are already UTC (MySQL/SQLite)."""
    if value.tzinfo is not None:
        value = value.astimezone(UTC)
    return value.replace(hour=0, minute=0, second=0, microsecond=0)


def backfill_node_user_usage_totals(connection, *, chunk: timedelta = timedelta(days=1)) -> int:
    """
    Fill node_user_usage_totals from node_user_usages joined to users, one UTC day per statement.

    created_at and node_id are carried over untyped so each bucket keeps exactly the stored value the
    record job will later upsert against. Returns the number of totals rows inserted.
    """
    typed_created_at = sa.column("created_at", sa.DateTime(timezone=True))
    raw = sa.table(
        "node_user_usages",
        sa.column("created_at"),
        sa.column("user_id"),
        sa.column("node_id"),
        sa.column("used_traffic", sa.BigInteger()),
    )
    typed_raw = sa.table("node_user_usages", typed_created_at)
    users = sa.table("users", sa.column("id"))
    totals = sa.table(
        TABLE_NAME,
        sa.column("created_at"),
        sa.column("node_id"),
        sa.column("used_traffic", sa.BigInteger()),
    )

    first, last = connection.execute(
        sa.select(sa.func.min(typed_raw.c.created_at), sa.func.max(typed_raw.c.created_at))
    ).one()
    if first is None:
        return 0

    inserted = 0
    day = _utc_day_start(first)
    last = last.astimezone(UTC) if last.tzinfo is not None else last
    is_first = True
    while True:
        next_day = day + chunk
        is_last = next_day > last
        # The first and last chunks are open-ended so every row lands in exactly one chunk, whatever
        # textual form SQLite stored it in.
        conditions = []
        if not is_first:
            conditions.append(raw.c.created_at >= sa.bindparam("day_start", day, type_=sa.DateTime(timezone=True)))
        if not is_last:
            conditions.append(raw.c.created_at < sa.bindparam("day_end", next_day, type_=sa.DateTime(timezone=True)))

        rows = connection.execute(
            sa.select(
                raw.c.created_at,
                raw.c.node_id,
                sa.func.sum(raw.c.used_traffic).label("used_traffic"),
            )
            .select_from(raw.join(users, users.c.id == raw.c.user_id))
            .where(*conditions)
            .group_by(raw.c.created_at, raw.c.node_id)
        ).all()

        values = [
            {"created_at": row.created_at, "node_id": row.node_id, "used_traffic": int(row.used_traffic or 0)}
            for row in rows
        ]
        for start in range(0, len(values), INSERT_BATCH_SIZE):
            connection.execute(sa.insert(totals), values[start : start + INSERT_BATCH_SIZE])
        inserted += len(values)
        if is_last:
            break
        day = next_day
        is_first = False

    return inserted


def upgrade() -> None:
    op.create_table(
        TABLE_NAME,
        sa.Column("id", SqliteCompatibleBigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("node_id", SqliteCompatibleBigInteger(), nullable=True),
        sa.Column("used_traffic", sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(
            ["node_id"], ["nodes.id"], name=op.f("fk_node_user_usage_totals_node_id_nodes"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_node_user_usage_totals")),
        sa.UniqueConstraint("created_at", "node_id", name=op.f("uq_node_user_usage_totals_created_at")),
    )
    op.create_index("ix_node_user_usage_totals_created_at", TABLE_NAME, ["created_at"], unique=False)

    backfill_node_user_usage_totals(op.get_bind())


def downgrade() -> None:
    op.drop_index("ix_node_user_usage_totals_created_at", table_name=TABLE_NAME)
    op.drop_table(TABLE_NAME)
