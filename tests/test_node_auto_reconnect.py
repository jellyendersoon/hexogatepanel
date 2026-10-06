import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from PasarGuardNodeBridge import Health, NodeAPIError

from app.db.models import NodeStatus
from app.jobs import node_checker
from app.node import NodeReconnectBackoff
from app.operation import OperatorType
from app.operation.node import NodeOperation
from role import Role

# --- NodeReconnectBackoff -------------------------------------------------------


def _attempt_failures(backoff: NodeReconnectBackoff, node_id: int, checks: int) -> list[int]:
    """Return the failure counts at which record_failure asked for a reconnect."""
    attempts = []
    for _ in range(checks):
        if backoff.record_failure(node_id):
            attempts.append(backoff.failures(node_id))
    return attempts


def test_backoff_waits_for_threshold_then_backs_off_exponentially():
    backoff = NodeReconnectBackoff(after_failures=3, max_backoff_checks=60)

    assert _attempt_failures(backoff, 1, 40) == [3, 5, 9, 17, 33]
    assert backoff.attempts(1) == 5
    assert backoff.checks_until_next_attempt(1) == 65 - 40


def test_backoff_caps_gap_between_attempts():
    backoff = NodeReconnectBackoff(after_failures=1, max_backoff_checks=4)

    # gaps: 2, 4, 4, 4 ... after the cap is hit
    assert _attempt_failures(backoff, 7, 20) == [1, 3, 7, 11, 15, 19]


def test_backoff_success_and_manual_reset_start_over():
    backoff = NodeReconnectBackoff(after_failures=2, max_backoff_checks=60)

    assert _attempt_failures(backoff, 3, 6) == [2, 4]
    backoff.record_success(3)
    assert backoff.failures(3) == 0
    assert backoff.attempts(3) == 0
    assert _attempt_failures(backoff, 3, 2) == [2]

    backoff.reset(3)
    assert backoff.checks_until_next_attempt(3) == 2
    assert _attempt_failures(backoff, 3, 2) == [2]

    backoff.reset_all()
    assert backoff.failures(3) == 0


def test_backoff_tracks_nodes_independently():
    backoff = NodeReconnectBackoff(after_failures=3, max_backoff_checks=60)

    assert _attempt_failures(backoff, 1, 3) == [3]
    assert backoff.record_failure(2) is False
    assert backoff.failures(2) == 1
    assert backoff.failures(1) == 3


def test_backoff_clamps_invalid_settings():
    backoff = NodeReconnectBackoff(after_failures=0, max_backoff_checks=0)

    assert backoff.after_failures == 1
    assert backoff.max_backoff_checks == 1
    assert _attempt_failures(backoff, 1, 4) == [1, 2, 3, 4]


# --- health check integration ---------------------------------------------------


class _FakeDB:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, *args):
        return False


@pytest.fixture
def health_check_env(monkeypatch: pytest.MonkeyPatch):
    backoff = NodeReconnectBackoff(after_failures=3, max_backoff_checks=60)
    reconnect = AsyncMock()
    monkeypatch.setattr(node_checker, "reconnect_backoff", backoff)
    monkeypatch.setattr(node_checker, "GetDB", lambda: _FakeDB())
    monkeypatch.setattr(node_checker, "get_bridge_memory", lambda: (None, None, None))
    monkeypatch.setattr(NodeOperation, "_update_single_node_status", AsyncMock())
    monkeypatch.setattr(node_checker.node_operator, "connect_single_node", reconnect)
    NodeOperation._in_flight_connects.discard(19)
    return SimpleNamespace(backoff=backoff, reconnect=reconnect)


def _broken_node():
    node = MagicMock()
    node.requires_hard_reset.return_value = False
    node.get_lifecycle_state = AsyncMock(return_value=None)
    node.update_observed_lifecycle = AsyncMock()
    return node


async def test_timeout_auto_reconnects_after_consecutive_failures_with_backoff(
    monkeypatch: pytest.MonkeyPatch, health_check_env
):
    """A node that keeps timing out used to stay in error until an admin reconnected it."""
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="late-xray", status=NodeStatus.error)
    monkeypatch.setattr(
        node_checker,
        "verify_node_backend_health",
        AsyncMock(return_value=(Health.BROKEN, -1, "Timeout error: request timed out")),
    )
    reconnect = health_check_env.reconnect

    for _ in range(2):
        await node_checker.process_node_health_check(db_node, node)
    reconnect.assert_not_awaited()

    # 3rd consecutive failure: first attempt is a plain connect (attach-friendly)
    await node_checker.process_node_health_check(db_node, node)
    reconnect.assert_awaited_once()
    assert reconnect.await_args.args[1] == 19
    assert reconnect.await_args.kwargs == {"force_start": False}

    # Backoff: 4th check waits, 5th fires attempt #2 as a forced restart
    await node_checker.process_node_health_check(db_node, node)
    assert reconnect.await_count == 1
    await node_checker.process_node_health_check(db_node, node)
    assert reconnect.await_count == 2
    assert reconnect.await_args.kwargs == {"force_start": True}

    # Next gap is 4 checks: 6, 7, 8 wait; 9 fires attempt #3
    for _ in range(3):
        await node_checker.process_node_health_check(db_node, node)
    assert reconnect.await_count == 2
    await node_checker.process_node_health_check(db_node, node)
    assert reconnect.await_count == 3


async def test_connection_error_counts_towards_auto_reconnect(monkeypatch: pytest.MonkeyPatch, health_check_env):
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="dead-host", status=NodeStatus.error)
    monkeypatch.setattr(
        node_checker,
        "verify_node_backend_health",
        AsyncMock(return_value=(Health.BROKEN, -2, "Connection error: connection refused")),
    )

    for _ in range(3):
        await node_checker.process_node_health_check(db_node, node)

    health_check_env.reconnect.assert_awaited_once()


async def test_health_check_timeout_exception_counts_towards_auto_reconnect(
    monkeypatch: pytest.MonkeyPatch, health_check_env
):
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="slow-node", status=NodeStatus.connected)
    monkeypatch.setattr(node_checker, "verify_node_backend_health", AsyncMock(side_effect=TimeoutError()))

    for _ in range(3):
        await node_checker.process_node_health_check(db_node, node)

    health_check_env.reconnect.assert_awaited_once()
    assert health_check_env.backoff.attempts(19) == 1


async def test_successful_check_resets_failure_counter(monkeypatch: pytest.MonkeyPatch, health_check_env):
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="flaky", status=NodeStatus.connected)
    verify = AsyncMock(return_value=(Health.BROKEN, -1, "Timeout error"))
    monkeypatch.setattr(node_checker, "verify_node_backend_health", verify)

    for _ in range(2):
        await node_checker.process_node_health_check(db_node, node)
    assert health_check_env.backoff.failures(19) == 2

    verify.return_value = (Health.HEALTHY, None, None)
    await node_checker.process_node_health_check(db_node, node)
    assert health_check_env.backoff.failures(19) == 0

    verify.return_value = (Health.BROKEN, -1, "Timeout error")
    for _ in range(2):
        await node_checker.process_node_health_check(db_node, node)
    health_check_env.reconnect.assert_not_awaited()


async def test_in_flight_start_is_not_counted_as_failure(monkeypatch: pytest.MonkeyPatch, health_check_env):
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="starting", status=NodeStatus.error)
    monkeypatch.setattr(
        node_checker,
        "verify_node_backend_health",
        AsyncMock(return_value=(Health.BROKEN, -1, "Timeout error")),
    )

    NodeOperation._in_flight_connects.add(19)
    try:
        for _ in range(5):
            await node_checker.process_node_health_check(db_node, node)
    finally:
        NodeOperation._in_flight_connects.discard(19)

    health_check_env.reconnect.assert_not_awaited()
    assert health_check_env.backoff.failures(19) == 0


async def test_core_still_starting_eventually_auto_reconnects(monkeypatch: pytest.MonkeyPatch, health_check_env):
    """One 'core is not started yet' is normal; the same answer for N checks means it is stuck."""
    node = _broken_node()
    db_node = SimpleNamespace(id=19, name="stuck-core", status=NodeStatus.connected)
    monkeypatch.setattr(
        node_checker,
        "verify_node_backend_health",
        AsyncMock(return_value=(Health.BROKEN, 503, "core is not started yet")),
    )

    await node_checker.process_node_health_check(db_node, node)
    health_check_env.reconnect.assert_not_awaited()

    for _ in range(2):
        await node_checker.process_node_health_check(db_node, node)
    health_check_env.reconnect.assert_awaited_once()


# --- real backend check ---------------------------------------------------------


def _connected_node(*, started: bool):
    node = MagicMock()
    node.get_health = AsyncMock(return_value=Health.HEALTHY)
    node.set_health = AsyncMock()
    node.get_backend_stats = AsyncMock(return_value=object())
    node.info = AsyncMock(return_value=SimpleNamespace(started=started, node_version="0.5.4", core_version="26.3"))
    return node


async def test_verify_reports_backend_not_running_when_info_says_not_started():
    node = _connected_node(started=False)

    health, code, message = await node_checker.verify_node_backend_health(node, "ghost-core")

    assert health is Health.BROKEN
    assert code == node_checker.BACKEND_NOT_RUNNING_CODE
    assert node_checker.is_core_dead_error(code, message) is True
    node.set_health.assert_awaited_once_with(Health.BROKEN)


async def test_verify_stays_healthy_when_info_says_started():
    node = _connected_node(started=True)

    health, code, message = await node_checker.verify_node_backend_health(node, "fine-core")

    assert (health, code, message) == (Health.HEALTHY, None, None)
    node.set_health.assert_not_awaited()


async def test_verify_ignores_failing_info_probe_when_stats_succeed():
    node = _connected_node(started=True)
    node.info = AsyncMock(side_effect=NodeAPIError(-1, "Timeout error"))

    health, code, message = await node_checker.verify_node_backend_health(node, "flaky-info")

    assert (health, code, message) == (Health.HEALTHY, None, None)


async def test_connected_node_with_dead_backend_is_restarted(monkeypatch: pytest.MonkeyPatch, health_check_env):
    node = _connected_node(started=False)
    node.requires_hard_reset.return_value = False
    node.get_lifecycle_state = AsyncMock(return_value=None)
    node.update_observed_lifecycle = AsyncMock()
    db_node = SimpleNamespace(id=19, name="ghost-core", status=NodeStatus.connected)

    await node_checker.process_node_health_check(db_node, node)

    NodeOperation._update_single_node_status.assert_awaited()
    assert NodeOperation._update_single_node_status.await_args.args[2] == NodeStatus.error
    health_check_env.reconnect.assert_awaited_once()


# --- manual reconnect resets the counters --------------------------------------


async def test_manual_reconnect_resets_backoff(monkeypatch: pytest.MonkeyPatch):
    backoff = NodeReconnectBackoff(after_failures=3, max_backoff_checks=60)
    monkeypatch.setattr("app.operation.node.reconnect_backoff", backoff)
    operator = NodeOperation(operator_type=OperatorType.API)
    monkeypatch.setattr(operator, "connect_single_node", AsyncMock())

    for _ in range(2):
        backoff.record_failure(19)
    assert backoff.failures(19) == 2

    await operator.restart_node(object(), 19, SimpleNamespace(username="admin"))

    assert backoff.failures(19) == 0
    operator.connect_single_node.assert_awaited_once()
    assert operator.connect_single_node.await_args.args[1] == 19
    assert operator.connect_single_node.await_args.kwargs == {"force_start": True}


# --- startup hook does not block the lifespan -----------------------------------


@pytest.fixture
def startup_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(node_checker.runtime_settings, "role", Role.ALL_IN_ONE)
    monkeypatch.setattr(node_checker.server_settings, "workers", 1)
    monkeypatch.setattr(node_checker.feature_settings, "stop_nodes_on_shutdown", False)
    monkeypatch.setattr(node_checker, "ensure_bridge_memory", AsyncMock())
    monkeypatch.setattr(node_checker, "GetDB", lambda: _FakeDB())
    monkeypatch.setattr(node_checker, "get_nodes", AsyncMock(return_value=([SimpleNamespace(id=1)], 1)))
    monkeypatch.setattr("app.nats.leader.needs_job_leader", lambda: False)
    monkeypatch.setattr(node_checker.scheduler, "add_job", MagicMock())
    shutdown_hooks: list = []
    monkeypatch.setattr(node_checker, "on_shutdown", shutdown_hooks.append)
    monkeypatch.setattr(node_checker, "_startup_connect_task", None)

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_bulk_connect(db, nodes):
        started.set()
        await release.wait()
        finished.set()

    bulk = AsyncMock(side_effect=slow_bulk_connect)
    monkeypatch.setattr(node_checker.node_operator, "connect_nodes_bulk", bulk)
    return SimpleNamespace(bulk=bulk, started=started, release=release, finished=finished, hooks=shutdown_hooks)


async def test_initialize_nodes_returns_before_bulk_connect_finishes(startup_env):
    await asyncio.wait_for(node_checker.initialize_nodes(), timeout=1)

    # The hook returned while the (blocked) bulk connect is still running in the background.
    assert node_checker.startup_connect_in_progress() is True
    await asyncio.wait_for(startup_env.started.wait(), timeout=1)
    assert startup_env.finished.is_set() is False
    assert node_checker._startup_connect_task is not None
    assert node_checker._stop_startup_connect in startup_env.hooks

    # Health checks stay out of the way until the startup connect is done.
    get_nodes = node_checker.get_nodes
    get_nodes.reset_mock()
    await node_checker.node_health_check()
    get_nodes.assert_not_awaited()

    startup_env.release.set()
    await asyncio.wait_for(node_checker._startup_connect_task, timeout=1)
    assert startup_env.finished.is_set() is True
    assert node_checker.startup_connect_in_progress() is False
    startup_env.bulk.assert_awaited_once()


async def test_shutdown_cancels_running_startup_connect(startup_env):
    await node_checker.initialize_nodes()
    await asyncio.wait_for(startup_env.started.wait(), timeout=1)
    task = node_checker._startup_connect_task

    await asyncio.wait_for(node_checker._stop_startup_connect(), timeout=1)

    assert task.cancelled() is True
    assert startup_env.finished.is_set() is False
    assert node_checker._startup_connect_task is None
    # Idempotent: a second call with nothing running is a no-op.
    await node_checker._stop_startup_connect()


async def test_startup_connect_failure_does_not_propagate(monkeypatch: pytest.MonkeyPatch, startup_env):
    monkeypatch.setattr(
        node_checker.node_operator, "connect_nodes_bulk", AsyncMock(side_effect=RuntimeError("nats down"))
    )

    await node_checker.initialize_nodes()
    await asyncio.wait_for(node_checker._startup_connect_task, timeout=1)

    assert node_checker._startup_connect_task.exception() is None


async def test_initialize_nodes_skips_on_non_node_role(monkeypatch: pytest.MonkeyPatch, startup_env):
    monkeypatch.setattr(node_checker.runtime_settings, "role", Role.SCHEDULER)

    await node_checker.initialize_nodes()

    assert node_checker._startup_connect_task is None
    startup_env.bulk.assert_not_awaited()
