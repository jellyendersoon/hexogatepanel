"""
node_user_usage_totals: the per (bucket, node) rollup behind the unfiltered users-usage chart.

The rollup must always equal the aggregate of node_user_usages joined to users, so the global chart
returns exactly what the raw query returned before. These tests record usage through the real record
path, read the chart through the API, run every path that deletes node_user_usages rows, and run the
migration backfill.

Each test writes into its own far-future time window, so rows that other tests insert straight into
node_user_usages (bypassing the rollup) never fall inside the ranges compared here.
"""

from __future__ import annotations

import importlib.util
import random
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from itertools import count
from pathlib import Path

import pytest
from fastapi import status
from sqlalchemy import and_, delete, func, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db import base
from app.db.crud.bulk import reset_all_users_data_usage
from app.db.crud.general import (
    _build_trunc_expression,
    attach_timezone_to_period_start,
    get_complete_period_start_for_filter,
    to_utc_for_filter,
)
from app.db.crud.node import clear_usage_data, remove_node, remove_nodes
from app.db.crud.user import (
    bulk_reset_user_data_usage,
    clear_user_node_usages,
    remove_expired_users,
    remove_users,
    reset_user_data_usage,
)
from app.db.models import Admin, AdminRole, Node, NodeUserUsage, NodeUserUsageTotal, User, UserStatus
from app.jobs import record_usages
from app.models.node import UsageTable
from app.models.proxy import ProxyTable
from app.models.stats import Period, UserUsageStat, UserUsageStatsList
from tests.api import GetTestDB, TestSession, client, engine as test_engine
from tests.api.helpers import OPERATOR_ROLE_ID, auth_headers, unique_name

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "db"
    / "migrations"
    / "versions"
    / "e2a21af8f26e_add_node_user_usage_totals.py"
)
TEHRAN = timezone(timedelta(hours=3, minutes=30))
WINDOW_DAYS = 80
# Node coefficients: the rollup must hold the coefficient-scaled values, like node_user_usages.
COEFFICIENTS = (1.5, 0.7, 2.0)
_windows = count(random.randrange(0, 2_000))


def _utc_naive(value: datetime) -> datetime:
    """Drivers return bucket timestamps naive or aware depending on the dialect; compare them as naive UTC."""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _next_window() -> datetime:
    return datetime(2090, 1, 1, tzinfo=UTC) + timedelta(days=WINDOW_DAYS * next(_windows))


@dataclass
class Seed:
    admin_ids: list[int]
    admin_names: list[str]
    user_ids: list[int]
    node_ids: list[int]
    window_start: datetime

    @property
    def window_end(self) -> datetime:
        return self.window_start + timedelta(days=WINDOW_DAYS)

    @property
    def data_start(self) -> datetime:
        # A day into the window: SQLite compares timestamps as text, and record-path rows ("...+00:00") at
        # the exact window start would sort before a "....000000" bound.
        return self.window_start + timedelta(days=1)

    @property
    def coefficients(self) -> dict[int, float]:
        return dict(zip(self.node_ids, COEFFICIENTS, strict=True))


@pytest.fixture
def record_into_test_db(monkeypatch: pytest.MonkeyPatch):
    """Point the record job at the API test database."""
    monkeypatch.setattr(record_usages, "engine", test_engine)
    monkeypatch.setattr(record_usages, "GetDB", GetTestDB)
    return monkeypatch


async def _seed(session_factory=TestSession, *, users: int = 4) -> Seed:
    async with session_factory() as session:
        admins = [
            Admin(username=unique_name("rollup_admin"), hashed_password="secret", role_id=OPERATOR_ROLE_ID)
            for _ in range(2)
        ]
        session.add_all(admins)
        await session.flush()

        db_users = [
            User(
                username=unique_name("rollup_user"),
                admin_id=admins[index % 2].id,
                proxy_settings=ProxyTable().dict(no_obj=True),
            )
            for index in range(users)
        ]
        db_nodes = [
            Node(
                name=unique_name("rollup_node"),
                address="127.0.0.1",
                port=8080,
                api_port=62051,
                server_ca="ca",
                api_key="key",
                core_config_id=None,
            )
            for _ in COEFFICIENTS
        ]
        session.add_all(db_users + db_nodes)
        await session.flush()
        seed = Seed(
            admin_ids=[admin.id for admin in admins],
            admin_names=[admin.username for admin in admins],
            user_ids=[user.id for user in db_users],
            node_ids=[node.id for node in db_nodes],
            window_start=_next_window(),
        )
        await session.commit()
        return seed


async def _record(monkeypatch: pytest.MonkeyPatch, seed: Seed, bucket: datetime, rows: dict[int, list[tuple]]):
    """Record one bucket through the real record path. ``rows`` maps node_id -> [(uid, raw_value)]."""
    monkeypatch.setattr(record_usages, "_get_time_bucket", lambda: bucket)
    await record_usages.record_user_stats_batched(
        {
            node_id: [{"uid": str(uid), "value": value} for uid, value in node_rows]
            for node_id, node_rows in rows.items()
        },
        seed.coefficients,
    )


async def _record_dataset(monkeypatch: pytest.MonkeyPatch, seed: Seed) -> None:
    """
    Several users, nodes and buckets: same-hour buckets, Tehran-midnight and UTC-midnight edges,
    a second day, a second month, a uid that does not exist (dropped by the record path), repeated
    writes to the same bucket, and one bucket/node that only the last user touches.
    """
    base_time = seed.data_start
    users = seed.user_ids
    node_a, node_b, node_c = seed.node_ids
    missing_uid = max(users) + 10_000_000
    buckets = [
        base_time,
        base_time + timedelta(minutes=10),
        base_time + timedelta(hours=1, minutes=30),
        base_time + timedelta(hours=20, minutes=20),  # 23:50 Tehran
        base_time + timedelta(hours=20, minutes=30),  # 00:00 Tehran, next local day
        base_time + timedelta(hours=23, minutes=50),
        base_time + timedelta(days=1, hours=2),
        base_time + timedelta(days=40, hours=5),  # next month
    ]
    for index, bucket in enumerate(buckets):
        await _record(
            monkeypatch,
            seed,
            bucket,
            {
                node_a: [(users[0], 1_000 + index), (users[1], 3_333 + 7 * index), (missing_uid, 999_999)],
                node_b: [(users[1], 501 + index), (users[2], 12_345 * (index + 1))],
            },
        )
        if index % 3 == 0:
            # A second write into the same bucket adds to the existing rows.
            await _record(monkeypatch, seed, bucket, {node_a: [(users[0], 77)], node_b: [(users[2], 5)]})

    # Only the last user ever uses node_c, in a bucket of its own, so removing its rows empties that bucket.
    await _record(monkeypatch, seed, base_time + timedelta(days=3), {node_c: [(users[-1], 4_242)]})
    await _record(monkeypatch, seed, base_time + timedelta(hours=1), {node_c: [(users[-1], 10)]})


async def _raw_aggregate(seed: Seed) -> dict[tuple, int]:
    async with TestSession() as db:
        rows = await db.execute(
            select(NodeUserUsage.created_at, NodeUserUsage.node_id, func.sum(NodeUserUsage.used_traffic))
            .join(User, User.id == NodeUserUsage.user_id)
            .where(NodeUserUsage.created_at >= seed.window_start, NodeUserUsage.created_at < seed.window_end)
            .group_by(NodeUserUsage.created_at, NodeUserUsage.node_id)
        )
        return _by_bucket(rows)


def _by_bucket(rows) -> dict[tuple, int]:
    result: dict[tuple, int] = defaultdict(int)
    for created_at, node_id, total in rows:
        result[(_utc_naive(created_at), node_id)] += int(total)
    return dict(result)


async def _totals_aggregate(seed: Seed) -> dict[tuple, int]:
    async with TestSession() as db:
        rows = await db.execute(
            select(NodeUserUsageTotal.created_at, NodeUserUsageTotal.node_id, func.sum(NodeUserUsageTotal.used_traffic))
            .where(
                NodeUserUsageTotal.created_at >= seed.window_start,
                NodeUserUsageTotal.created_at < seed.window_end,
            )
            .group_by(NodeUserUsageTotal.created_at, NodeUserUsageTotal.node_id)
        )
        return _by_bucket(rows)


async def _assert_totals_match_raw(seed: Seed) -> None:
    raw = await _raw_aggregate(seed)
    assert raw, "the dataset must leave some usage in the window"
    assert await _totals_aggregate(seed) == raw


async def _raw_chart(
    start: datetime,
    end: datetime,
    period: Period,
    *,
    node_id: int | None = None,
    group_by_node: bool = False,
    admins: list[str] | None = None,
) -> dict[int, list[UserUsageStat]]:
    """The users-usage chart computed straight from node_user_usages (the pre-rollup query)."""
    async with TestSession() as db:
        trunc_expr = _build_trunc_expression(db, period, NodeUserUsage.created_at, start)
        conditions = [
            NodeUserUsage.created_at >= get_complete_period_start_for_filter(start, period),
            NodeUserUsage.created_at < to_utc_for_filter(end),
        ]
        from_clause = NodeUserUsage.__table__.join(User, User.id == NodeUserUsage.user_id)
        if admins:
            from_clause = from_clause.join(Admin, Admin.id == User.admin_id)
            conditions.append(Admin.username.in_(admins))
        stats_key = -1
        if node_id is not None:
            conditions.append(NodeUserUsage.node_id == node_id)
            stats_key = node_id

        columns = [trunc_expr.label("period_start")]
        group_by = [trunc_expr]
        if group_by_node:
            columns.append(func.coalesce(NodeUserUsage.node_id, 0).label("node_id"))
            group_by.append(NodeUserUsage.node_id)
        columns.append(func.sum(NodeUserUsage.used_traffic).label("total_traffic"))
        stmt = (
            select(*columns).select_from(from_clause).where(and_(*conditions)).group_by(*group_by).order_by(trunc_expr)
        )

        stats: dict[int, list[UserUsageStat]] = defaultdict(list)
        for row in (await db.execute(stmt)).mappings():
            row_dict = dict(row)
            key = row_dict.pop("node_id", stats_key)
            attach_timezone_to_period_start(row_dict, start.tzinfo, db.bind.dialect.name)
            stats[key].append(UserUsageStat(**row_dict))
        return dict(stats)


def _api_chart(
    token: str,
    start: datetime,
    end: datetime,
    period: Period,
    *,
    node_id: int | None = None,
    group_by_node: bool = False,
    admins: list[str] | None = None,
) -> dict[int, list[UserUsageStat]]:
    params: dict = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "period": period.value,
        "group_by_node": str(group_by_node).lower(),
    }
    if node_id is not None:
        params["node_id"] = node_id
    if admins:
        params["admin"] = admins
    response = client.get("/api/users/usage", params=params, headers=auth_headers(token))
    assert response.status_code == status.HTTP_200_OK, response.text
    return UserUsageStatsList.model_validate(response.json()).stats


def _chart_ranges(seed: Seed, period: Period) -> list[tuple[datetime, datetime]]:
    ranges = [(seed.window_start, seed.window_end)]
    if period != Period.month:
        # Unaligned Tehran range: exercises the timezone conversion and the first-complete-bucket filter.
        tehran_start = (seed.window_start + timedelta(minutes=5)).astimezone(TEHRAN)
        ranges.append((tehran_start, (seed.window_start + timedelta(days=45)).astimezone(TEHRAN)))
    return ranges


async def _assert_global_chart_matches_raw(token: str, seed: Seed, periods=(Period.hour, Period.day, Period.month)):
    for period in periods:
        for start, end in _chart_ranges(seed, period):
            for node_id in (None, *seed.node_ids):
                for group_by_node in (False, True):
                    expected = await _raw_chart(start, end, period, node_id=node_id, group_by_node=group_by_node)
                    actual = _api_chart(token, start, end, period, node_id=node_id, group_by_node=group_by_node)
                    assert actual == expected, (period, start, node_id, group_by_node)


async def _load_users(session, user_ids: list[int]) -> list[User]:
    return list((await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all())


# --- (a) recording + global chart ---------------------------------------------------------------------


async def test_global_chart_from_rollup_equals_raw_aggregate(access_token, record_into_test_db):
    seed = await _seed()
    await _record_dataset(record_into_test_db, seed)

    await _assert_totals_match_raw(seed)
    raw = await _raw_aggregate(seed)
    # The coefficient-scaled values are stored, and the missing uid is dropped from both tables.
    first_bucket = raw[(_utc_naive(seed.data_start), seed.node_ids[0])]
    assert first_bucket == int(1_000 * 1.5) + int(3_333 * 1.5) + int(77 * 1.5)

    await _assert_global_chart_matches_raw(access_token, seed)


async def test_unfiltered_chart_reads_rollup_and_filtered_chart_reads_raw_rows(access_token, record_into_test_db):
    seed = await _seed()
    await _record_dataset(record_into_test_db, seed)
    start, end = seed.window_start, seed.window_end

    # (b) admin-filtered requests keep using node_user_usages and match the raw per-admin aggregate.
    for admins in ([seed.admin_names[0]], [seed.admin_names[1]], seed.admin_names):
        for group_by_node in (False, True):
            for period in (Period.hour, Period.day):
                expected = await _raw_chart(start, end, period, group_by_node=group_by_node, admins=admins)
                assert expected
                actual = _api_chart(access_token, start, end, period, group_by_node=group_by_node, admins=admins)
                assert actual == expected

    filtered_before = _api_chart(access_token, start, end, Period.day, admins=seed.admin_names)
    unfiltered_before = _api_chart(access_token, start, end, Period.day)

    # A rollup-only change shows up in the unfiltered chart and not in the admin-filtered one.
    extra_bucket = seed.data_start + timedelta(days=10)
    async with TestSession() as session:
        session.add(NodeUserUsageTotal(created_at=extra_bucket, node_id=seed.node_ids[0], used_traffic=123))
        await session.commit()
    try:
        assert _api_chart(access_token, start, end, Period.day, admins=seed.admin_names) == filtered_before
        unfiltered_after = _api_chart(access_token, start, end, Period.day)
        assert unfiltered_after != unfiltered_before
        assert sum(stat.total_traffic for stat in unfiltered_after[-1]) == (
            sum(stat.total_traffic for stat in unfiltered_before[-1]) + 123
        )
    finally:
        async with TestSession() as session:
            await session.execute(
                delete(NodeUserUsageTotal).where(
                    NodeUserUsageTotal.created_at == extra_bucket, NodeUserUsageTotal.node_id == seed.node_ids[0]
                )
            )
            await session.commit()


# --- (c) delete paths ---------------------------------------------------------------------------------


async def _prepare(record_into_test_db) -> Seed:
    seed = await _seed()
    await _record_dataset(record_into_test_db, seed)
    await _assert_totals_match_raw(seed)
    return seed


async def _assert_consistent(token: str, seed: Seed) -> None:
    await _assert_totals_match_raw(seed)
    await _assert_global_chart_matches_raw(token, seed, periods=(Period.hour, Period.day))


async def test_remove_user_via_api_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        username = (await session.execute(select(User.username).where(User.id == seed.user_ids[-1]))).scalar_one()

    response = client.delete(f"/api/user/{username}", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_204_NO_CONTENT

    await _assert_consistent(access_token, seed)
    # node_c only ever carried the removed user's traffic: its rollup rows are gone, not left at zero.
    async with TestSession() as session:
        node_c_rows = await session.execute(
            select(func.count()).select_from(NodeUserUsageTotal).where(NodeUserUsageTotal.node_id == seed.node_ids[2])
        )
        assert node_c_rows.scalar_one() == 0


async def test_remove_users_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        await remove_users(session, await _load_users(session, seed.user_ids[1:3]))
    await _assert_consistent(access_token, seed)


async def test_remove_expired_users_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        await session.execute(update(User).where(User.id.in_(seed.user_ids[:2])).values(status=UserStatus.disabled))
        await session.commit()
        removed = await remove_expired_users(session, admin_id=seed.admin_ids[0], target="disabled")
    assert len(removed) == 1
    await _assert_consistent(access_token, seed)


async def test_reset_usage_with_chart_cleanup_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        (db_user,) = await _load_users(session, [seed.user_ids[2]])
        await reset_user_data_usage(session, db_user, clean_chart_data=True)
    await _assert_consistent(access_token, seed)

    async with TestSession() as session:
        await bulk_reset_user_data_usage(
            session, await _load_users(session, [seed.user_ids[0], seed.user_ids[-1]]), clean_chart_data=True
        )
    await _assert_consistent(access_token, seed)


async def test_clear_user_node_usages_before_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        await clear_user_node_usages(session, seed.user_ids[1], before=seed.data_start + timedelta(hours=1, minutes=30))
        await session.commit()
    await _assert_consistent(access_token, seed)


async def test_reset_all_users_of_admin_with_chart_cleanup_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        db_admin = (await session.execute(select(Admin).where(Admin.id == seed.admin_ids[1]))).scalar_one()
        await reset_all_users_data_usage(session, db_admin, clean_chart_data=True)
    await _assert_consistent(access_token, seed)


async def test_remove_node_and_nodes_keep_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        db_node = (await session.execute(select(Node).where(Node.id == seed.node_ids[2]))).scalar_one()
        await remove_node(session, db_node)
    await _assert_consistent(access_token, seed)

    async with TestSession() as session:
        await remove_nodes(session, [seed.node_ids[1]])
    await _assert_consistent(access_token, seed)


async def test_clear_usage_data_range_keeps_totals(access_token, record_into_test_db):
    seed = await _prepare(record_into_test_db)
    async with TestSession() as session:
        await clear_usage_data(
            session,
            UsageTable.node_user_usages,
            seed.data_start + timedelta(minutes=10),
            seed.data_start + timedelta(days=1),
        )
    await _assert_consistent(access_token, seed)


# --- (d) migration backfill ---------------------------------------------------------------------------


def _load_migration():
    spec = importlib.util.spec_from_file_location("node_user_usage_totals_migration", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_migration_backfill_matches_raw_aggregate(tmp_path, monkeypatch: pytest.MonkeyPatch):
    migration = _load_migration()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    class BackfillGetDB:
        def __init__(self):
            self.db = session_factory()

        async def __aenter__(self):
            return self.db

        async def __aexit__(self, *_exc):
            await self.db.close()

    monkeypatch.setattr(record_usages, "engine", engine)
    monkeypatch.setattr(record_usages, "GetDB", BackfillGetDB)
    monkeypatch.setattr(record_usages, "_dialect_cache", [])

    try:
        async with engine.begin() as conn:
            await conn.run_sync(base.Base.metadata.create_all)
        async with session_factory() as session:
            session.add(AdminRole(name="operator", is_owner=False, permissions={}, limits={}, features={}, access={}))
            await session.commit()

        seed = await _seed(session_factory)
        await _record_dataset(monkeypatch, seed)

        # Rows written outside the record path: an exact UTC midnight (chunk edge), a NULL node and a row
        # whose user no longer exists (excluded, like in the chart's join).
        midnight = seed.data_start + timedelta(days=2)
        async with session_factory() as session:
            await session.execute(
                insert(NodeUserUsage),
                [
                    {
                        "created_at": midnight,
                        "user_id": seed.user_ids[0],
                        "node_id": seed.node_ids[0],
                        "used_traffic": 11,
                    },
                    {"created_at": midnight, "user_id": seed.user_ids[1], "node_id": None, "used_traffic": 22},
                    {"created_at": midnight, "user_id": seed.user_ids[0], "node_id": None, "used_traffic": 33},
                    {
                        "created_at": midnight,
                        "user_id": max(seed.user_ids) + 999,
                        "node_id": seed.node_ids[0],
                        "used_traffic": 44,
                    },
                ],
            )
            # Pre-migration state: no rollup rows yet.
            await session.execute(delete(NodeUserUsageTotal))
            await session.commit()

        async with engine.begin() as conn:
            inserted = await conn.run_sync(migration.backfill_node_user_usage_totals)

        async def aggregates():
            async with session_factory() as session:
                raw = await session.execute(
                    select(NodeUserUsage.created_at, NodeUserUsage.node_id, func.sum(NodeUserUsage.used_traffic))
                    .join(User, User.id == NodeUserUsage.user_id)
                    .group_by(NodeUserUsage.created_at, NodeUserUsage.node_id)
                )
                totals = await session.execute(
                    select(NodeUserUsageTotal.created_at, NodeUserUsageTotal.node_id, NodeUserUsageTotal.used_traffic)
                )
                return _by_bucket(raw), [(_utc_naive(c), n, int(t)) for c, n, t in totals]

        raw, totals_rows = await aggregates()
        assert inserted == len(raw) == len(totals_rows)
        assert {(c, n): t for c, n, t in totals_rows} == raw
        assert raw[(_utc_naive(midnight), None)] == 55
        assert raw[(_utc_naive(midnight), seed.node_ids[0])] == 11

        # The record path keeps adding to the backfilled rows instead of creating parallel ones.
        await _record(monkeypatch, seed, seed.data_start, {seed.node_ids[0]: [(seed.user_ids[0], 1_000)]})
        raw, totals_rows = await aggregates()
        assert len(totals_rows) == len(raw)
        assert {(c, n): t for c, n, t in totals_rows} == raw
    finally:
        await engine.dispose()
