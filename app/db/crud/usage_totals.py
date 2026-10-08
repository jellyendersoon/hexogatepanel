"""
Maintenance helpers for ``node_user_usage_totals``.

``node_user_usage_totals`` holds, per (time bucket, node), the sum of ``node_user_usages.used_traffic``
over the rows whose user still exists, so the unfiltered users-usage chart can read a few thousand
rows instead of millions. Every path that removes ``node_user_usages`` rows calls into this module
inside the same transaction as the delete:

* rows removed for a set of users (user deletion, chart clean on usage reset):
  ``subtract_node_user_usage_totals`` before the delete, ``prune_empty_node_user_usage_totals`` after it;
* rows removed for whole nodes or whole time ranges: ``delete_node_user_usage_totals`` with the same
  filter, after the raw delete.

Lock order is always ``node_user_usages`` first, then ``node_user_usage_totals``, matching the record job.
"""

from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from app.db.models import NodeUserUsage, NodeUserUsageTotal, User

UsageSpan = tuple[datetime, datetime]
PRUNE_SPAN_MARGIN = timedelta(days=1)


def build_node_user_usage_totals_add(dialect: str, source: Select):
    """
    Build an upsert that adds ``source`` rows to ``node_user_usage_totals``.

    ``source`` must select ``created_at``, ``node_id`` and ``used_traffic`` with at most one row per
    (created_at, node_id). Existing totals rows get ``used_traffic`` added; missing ones are inserted.
    Rows are applied in (created_at, node_id) order so concurrent writers lock totals rows in the same order.
    """
    columns = ["created_at", "node_id", "used_traffic"]

    if dialect == "mysql":
        insert_source = source.subquery("insert_source")
        insert_select_stmt = select(
            insert_source.c.created_at,
            insert_source.c.node_id,
            insert_source.c.used_traffic,
        ).order_by(insert_source.c.created_at, insert_source.c.node_id)
        stmt = mysql_insert(NodeUserUsageTotal).from_select(columns, insert_select_stmt)
        return stmt.on_duplicate_key_update(used_traffic=NodeUserUsageTotal.used_traffic + insert_source.c.used_traffic)

    ordered_source = source.order_by(None).order_by(*source.selected_columns[:2])
    insert_fn = pg_insert if dialect == "postgresql" else sqlite_insert
    stmt = insert_fn(NodeUserUsageTotal).from_select(columns, ordered_source)
    return stmt.on_conflict_do_update(
        index_elements=["created_at", "node_id"],
        set_={"used_traffic": NodeUserUsageTotal.used_traffic + stmt.excluded.used_traffic},
    )


def node_user_usage_totals_source(*conditions, sign: int = 1) -> Select:
    """Aggregate ``node_user_usages`` (joined to existing users) per (created_at, node_id)."""
    total = func.sum(NodeUserUsage.used_traffic)
    return (
        select(
            NodeUserUsage.created_at.label("created_at"),
            NodeUserUsage.node_id.label("node_id"),
            (total if sign >= 0 else -total).label("used_traffic"),
        )
        .select_from(NodeUserUsage.__table__.join(User, User.id == NodeUserUsage.user_id))
        .where(*conditions)
        .group_by(NodeUserUsage.created_at, NodeUserUsage.node_id)
    )


async def _lock_usage_rows(db: AsyncSession, conditions) -> UsageSpan | None:
    """
    Lock the ``node_user_usages`` rows about to be removed and return their created_at span.

    Locking first makes the following aggregate read exactly the rows the delete removes, even while the
    record job keeps adding to them, and keeps the raw-then-totals lock order of the record job.
    """
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        # PostgreSQL forbids FOR UPDATE together with aggregates, so lock inside a subquery.
        locked = select(NodeUserUsage.created_at).where(*conditions).with_for_update().subquery("locked")
        stmt = select(func.count(), func.min(locked.c.created_at), func.max(locked.c.created_at))
    else:
        stmt = select(
            func.count(),
            func.min(NodeUserUsage.created_at),
            func.max(NodeUserUsage.created_at),
        ).where(*conditions)
        if dialect == "mysql":
            # COUNT(*) forces a scan of every matching row, so all of them get locked.
            stmt = stmt.with_for_update()

    count, start, end = (await db.execute(stmt)).one()
    if not count:
        return None
    return start, end


async def subtract_node_user_usage_totals(db: AsyncSession, *conditions) -> UsageSpan | None:
    """
    Subtract from the totals what the ``node_user_usages`` rows matching ``conditions`` contribute.

    Call it BEFORE deleting those rows (or the users that own them; the FK cascades), in the same
    transaction, then pass the returned span to ``prune_empty_node_user_usage_totals`` after the delete.
    """
    span = await _lock_usage_rows(db, conditions)
    if span is None:
        return None

    dialect = db.bind.dialect.name
    await db.execute(build_node_user_usage_totals_add(dialect, node_user_usage_totals_source(*conditions, sign=-1)))
    return span


async def prune_empty_node_user_usage_totals(db: AsyncSession, span: UsageSpan | None) -> None:
    """
    Drop totals rows inside ``span`` whose bucket/node no longer has any ``node_user_usages`` row.

    The raw chart query returns no row for such a bucket, so the rollup must not return a zero row either.
    """
    if span is None:
        return

    # Widen the span by a day: pruning only ever removes rows that no raw row backs, so a wider range
    # is still exact, and it covers every textual timestamp form SQLite may compare against.
    start, end = span[0] - PRUNE_SPAN_MARGIN, span[1] + PRUNE_SPAN_MARGIN
    in_span = and_(NodeUserUsageTotal.created_at >= start, NodeUserUsageTotal.created_at <= end)
    raw_rows = select(NodeUserUsage.id).select_from(
        NodeUserUsage.__table__.join(User, User.id == NodeUserUsage.user_id)
    )

    remaining = raw_rows.where(
        NodeUserUsage.created_at == NodeUserUsageTotal.created_at,
        NodeUserUsage.node_id == NodeUserUsageTotal.node_id,
    ).exists()
    await db.execute(
        delete(NodeUserUsageTotal).where(
            in_span,
            NodeUserUsageTotal.node_id.is_not(None),
            NodeUserUsageTotal.used_traffic == 0,
            ~remaining,
        )
    )

    # NULL node ids never hit the unique key, so a bucket can hold several offsetting rows: drop them all
    # once no raw row is left for that bucket.
    remaining_without_node = raw_rows.where(
        NodeUserUsage.created_at == NodeUserUsageTotal.created_at,
        NodeUserUsage.node_id.is_(None),
    ).exists()
    await db.execute(
        delete(NodeUserUsageTotal).where(
            in_span,
            NodeUserUsageTotal.node_id.is_(None),
            ~remaining_without_node,
        )
    )


async def delete_node_user_usage_totals(
    db: AsyncSession,
    *,
    node_ids: list[int] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> None:
    """
    Delete totals rows for whole nodes and/or a whole ``[start, end)`` created_at range.

    Only valid when every ``node_user_usages`` row with the same node/range was deleted too, which is the
    case for node removal and for clearing the usage table. Run it after the raw delete.
    """
    conditions = []
    if node_ids is not None:
        conditions.append(NodeUserUsageTotal.node_id.in_(node_ids))
    if start is not None:
        conditions.append(NodeUserUsageTotal.created_at >= start)
    if end is not None:
        conditions.append(NodeUserUsageTotal.created_at < end)

    stmt = delete(NodeUserUsageTotal)
    if conditions:
        stmt = stmt.where(*conditions)
    await db.execute(stmt)
