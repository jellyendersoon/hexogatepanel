import asyncio

from PasarGuardNodeBridge import Health, NodeAPIError, PasarGuardNode
from PasarGuardNodeBridge.storage import LifecycleStatus

from app import notification, on_shutdown, on_startup, scheduler
from app.db import GetDB
from app.db.crud.node import get_limited_nodes, get_nodes
from app.db.models import Node, NodeStatus
from app.models.node import NodeListQuery, NodeNotification
from app.nats import is_multi_worker
from app.node import node_manager, reconnect_backoff
from app.node.nats_memory import ensure_bridge_memory, get_bridge_memory, shutdown_bridge_memory
from app.operation import OperatorType
from app.operation.node import NodeOperation
from app.utils.logger import get_logger
from config import feature_settings, job_settings, runtime_settings, server_settings

node_operator = NodeOperation(operator_type=OperatorType.SYSTEM)
logger = get_logger("node-checker")

# Hard-limit concurrency: Prevent DB/API overload during health checks
# Limits concurrent node health check operations
NODE_CHECK_SEM = asyncio.Semaphore(5)  # Max 5 concurrent node health checks
ACTIVE_NODE_STATUSES = [NodeStatus.connected, NodeStatus.connecting, NodeStatus.error]


# pg-node returns these while the HTTP API is up. They are not interchangeable:
# - backend gone: keep-alive/crash already called Disconnect; panel must Start again
# - core still coming up / Xray API blip: another Start would kill that process
# "backend not running" is synthesized locally when GET /info reports started=false
# even though the stats call succeeded, so it is handled like a dead backend.
BACKEND_NOT_RUNNING_CODE = 503
BACKEND_NOT_RUNNING_MESSAGE = "backend not running (node reports core not started)"
_CORE_DEAD_MARKERS = ("backend not initialized", "backend not running")
_CORE_STARTING_MARKERS = ("core is not started yet", "failed to get sys stats")


def _health_error_matches(error_code: int | None, error_message: str | None, markers: tuple[str, ...]) -> bool:
    if error_code not in {500, 502, 503, 504}:
        return False
    detail = (error_message or "").lower()
    return any(marker in detail for marker in markers)


def is_core_dead_error(error_code: int | None, error_message: str | None) -> bool:
    return _health_error_matches(error_code, error_message, _CORE_DEAD_MARKERS)


def is_core_starting_error(error_code: int | None, error_message: str | None) -> bool:
    return _health_error_matches(error_code, error_message, _CORE_STARTING_MARKERS)


def is_core_not_started_error(error_code: int | None, error_message: str | None) -> bool:
    return is_core_dead_error(error_code, error_message) or is_core_starting_error(error_code, error_message)


def should_reconnect_after_health_error(error_code: int | None, error_message: str | None) -> bool:
    if error_code is None:
        return False

    # Dead-core and still-starting 5xxs are not generic reconnects. The BROKEN
    # handler starts only a missing backend, and only when no Start is in flight.
    if is_core_not_started_error(error_code, error_message):
        return False

    return error_code > -1


async def _start_already_in_progress(db_node: Node, shared_state) -> bool:
    if db_node.id in NodeOperation._in_flight_connects:
        return True
    if shared_state is not None and shared_state.observed is LifecycleStatus.STARTING:
        return True
    _, coordinator, _ = get_bridge_memory()
    return coordinator is not None and await coordinator.has_active_lease(str(db_node.id))


async def _backend_reported_running(node: PasarGuardNode, node_name: str) -> bool:
    """Ask pg-node whether its core process is actually up.

    A node can answer stats while its core is gone, and the local health flag only
    says the last RPC worked. GET /info carries pg-node's own started flag; use it
    as the source of truth. An unanswerable probe counts as running so a flaky
    secondary call never flips a node that just served stats.
    """
    try:
        info = await node.info()
    except Exception as exc:
        logger.debug(f"[{node_name}] Backend running probe skipped: {type(exc).__name__} - {exc!s}")
        return True
    if info is None:
        return True
    return bool(info.started)


async def verify_node_backend_health(node: PasarGuardNode, node_name: str) -> tuple[Health, int | None, str | None]:
    """
    Verify node health by checking backend stats and pg-node's own started flag.
    Returns (health, error_code, error_message) - error_code and error_message are None if no error occurred.
    """
    current_health = await asyncio.wait_for(node.get_health(), timeout=10)

    # Skip nodes that are not connected or invalid
    if current_health in (Health.NOT_CONNECTED, Health.INVALID):
        return current_health, None, None

    try:
        await node.get_backend_stats()
        if not await _backend_reported_running(node, node_name):
            # "connected" only means the HTTP API answers; the core itself is down.
            raise NodeAPIError(BACKEND_NOT_RUNNING_CODE, BACKEND_NOT_RUNNING_MESSAGE)
        if current_health != Health.HEALTHY:
            await node.set_health(Health.HEALTHY)
            logger.debug(f"[{node_name}] Node health is HEALTHY")
        return Health.HEALTHY, None, None
    except NodeAPIError as e:
        logger.error(
            f"[{node_name}] Health check failed, setting health to BROKEN | Error: NodeAPIError(code={e.code}) - {e.detail}"
        )
        try:
            await node.set_health(Health.BROKEN)
            return Health.BROKEN, e.code, e.detail
        except Exception as e_set_health:
            error_type_set = type(e_set_health).__name__
            logger.error(f"[{node_name}] Failed to set health to BROKEN | Error: {error_type_set} - {e_set_health!s}")
            return current_health, e.code, e.detail
    except Exception as e:
        error_type = type(e).__name__
        error_message = f"{error_type}: {e!s}"
        logger.error(f"[{node_name}] Health check failed, setting health to BROKEN | Error: {error_message}")
        try:
            await node.set_health(Health.BROKEN)
            return Health.BROKEN, None, error_message
        except Exception as e_set_health:
            error_type_set = type(e_set_health).__name__
            logger.error(f"[{node_name}] Failed to set health to BROKEN | Error: {error_type_set} - {e_set_health!s}")
            return current_health, None, error_message


async def _wait_or_auto_reconnect(db_node: Node, reason: str, shared_state=None) -> None:
    """Count a failed check that used to "wait for recovery" forever.

    After NODE_AUTO_RECONNECT_AFTER_FAILURES consecutive failures the node is
    reconnected the way an admin would do it by hand, then retried with
    exponential backoff (NODE_AUTO_RECONNECT_MAX_BACKOFF_CHECKS caps the gap).
    The first attempt is a plain connect so a core that finished starting late is
    attached instead of killed; later attempts force a restart, since a core that
    keeps timing out after being re-attached is hung.
    """
    node_id = db_node.id
    if await _start_already_in_progress(db_node, shared_state):
        # Our own (or another worker's) Start is still running; a second one would
        # kill the core that is coming up, so this check is not a failure yet.
        logger.debug("[%s] %s; backend start already in progress, waiting", db_node.name, reason)
        return
    if not reconnect_backoff.record_failure(node_id):
        logger.debug(
            "[%s] %s; waiting for recovery (%d consecutive failed checks, next auto-reconnect in %d checks)",
            db_node.name,
            reason,
            reconnect_backoff.failures(node_id),
            reconnect_backoff.checks_until_next_attempt(node_id),
        )
        return

    attempt = reconnect_backoff.attempts(node_id)
    force_start = attempt > 1
    logger.warning(
        "[%s] Auto-reconnect attempt #%d (%s) after %d consecutive failed health checks: %s; "
        "next attempt in %d checks if it fails",
        db_node.name,
        attempt,
        "force restart" if force_start else "connect",
        reconnect_backoff.failures(node_id),
        reason,
        reconnect_backoff.checks_until_next_attempt(node_id),
    )
    async with GetDB() as db:
        await node_operator.connect_single_node(db, node_id, force_start=force_start)


async def process_node_health_check(db_node: Node, node: PasarGuardNode):
    """
    Process health check for a single node:
    1. Check if node requires hard reset
    2. Verify backend health (stats call plus pg-node's started flag)
    3. Compare with database status
    4. Update status if needed

    Timeout handling:
    - For timeout/connection errors (code=-1/-2/None): wait for recovery, but after
      N consecutive failures auto-reconnect with exponential backoff
    - For other errors (code > -1): Reconnect (connection works but has another issue)
    - For NOT_CONNECTED/INVALID: Reconnect immediately
    """
    if node is None:
        return

    # Limit concurrent health checks to prevent DB/API overload
    async with NODE_CHECK_SEM:
        # Handle hard reset requirement
        if node.requires_hard_reset():
            async with GetDB() as db:
                await node_operator.connect_single_node(db, db_node.id)
            return

        try:
            health, error_code, error_message = await verify_node_backend_health(node, db_node.name)
        except TimeoutError:
            # Record timeout error in database; reconnect only once the backoff allows it
            logger.warning(f"[{db_node.name}] Health check timed out")
            async with GetDB() as db:
                await NodeOperation._update_single_node_status(
                    db, db_node.id, NodeStatus.error, message="Health check timeout"
                )
            await _wait_or_auto_reconnect(db_node, "health check timeout")
            return
        except NodeAPIError as e:
            # Record error in database
            async with GetDB() as db:
                await NodeOperation._update_single_node_status(db, db_node.id, NodeStatus.error, message=e.detail)
            # For timeout errors (code=-1), wait for recovery with backoff
            if e.code == -1:
                logger.warning(f"[{db_node.name}] Health check timed out (NodeAPIError), waiting for recovery")
                await _wait_or_auto_reconnect(db_node, f"health check timed out: {e.detail}")
                return
            # For other errors, reconnect
            async with GetDB() as db:
                await node_operator.connect_single_node(db, db_node.id)
            return

        # Skip nodes that are already healthy and connected
        if health == Health.HEALTHY and db_node.status == NodeStatus.connected:
            reconnect_backoff.record_success(db_node.id)
            return

        if health is Health.INVALID:
            logger.warning(f"[{db_node.name}] Node health is INVALID, ignoring...")
            return

        # Prefer shared lifecycle state so multi-worker local NOT_CONNECTED does not thrash Start.
        # A BROKEN observation with desired HEALTHY can be an ambiguous client-side Start
        # timeout: the remote core may have completed startup after the panel gave up.
        shared_state = await node.get_lifecycle_state()
        if (
            health is Health.NOT_CONNECTED
            and shared_state is not None
            and (shared_state.observed is LifecycleStatus.HEALTHY or shared_state.desired is LifecycleStatus.HEALTHY)
        ):
            attached = await NodeOperation._attach_if_running(node, db_node.name)
            if attached is not None:
                return

            _, coordinator, _ = get_bridge_memory()
            if coordinator is not None and await coordinator.has_active_lease(str(db_node.id)):
                logger.debug(
                    "[%s] Shared lifecycle HEALTHY with active lease; waiting for owner",
                    db_node.name,
                )
                return

            # Stale/failed desired-healthy state: fall through to reconnect only after
            # the attach probe and active-lease check have both failed.
            logger.debug(
                "[%s] Shared lifecycle desired HEALTHY but attach failed and no active lease; reconnecting",
                db_node.name,
            )

        # Handle NOT_CONNECTED - reconnect immediately
        if health is Health.NOT_CONNECTED:
            async with GetDB() as db:
                await node_operator.connect_single_node(db, db_node.id)
            return

        # Handle BROKEN health
        if health == Health.BROKEN:
            # Record actual error in database
            async with GetDB() as db:
                await NodeOperation._update_single_node_status(db, db_node.id, NodeStatus.error, message=error_message)
            if shared_state is not None:
                await node.update_observed_lifecycle(LifecycleStatus.BROKEN, expected_epoch=shared_state.epoch)
            # Let pg-node recover transient Xray API/core failures internally.
            if should_reconnect_after_health_error(error_code, error_message):
                async with GetDB() as db:
                    await node_operator.connect_single_node(db, db_node.id)
                return
            # Keep-alive timeout / crash leaves HTTP up but Xray stopped
            # ("backend not initialized" / started=false). A second Start while Xray
            # is still coming up ("core is not started yet") would kill that process.
            if is_core_dead_error(error_code, error_message):
                if await _start_already_in_progress(db_node, shared_state):
                    logger.debug("[%s] Core is not running but a start is in progress; waiting", db_node.name)
                    return
                logger.warning(f"[{db_node.name}] Core is not running; re-applying config")
                async with GetDB() as db:
                    await node_operator.connect_single_node(db, db_node.id)
                return
            # Timeout (code=-1/None), connection error (-2) or a core that is still
            # starting: wait, but not forever - auto-reconnect once the backoff allows.
            await _wait_or_auto_reconnect(
                db_node, f"health check failed (code={error_code}): {error_message}", shared_state
            )
            return

        # Update status for recovering nodes
        if db_node.status in (NodeStatus.connecting, NodeStatus.error) and health == Health.HEALTHY:
            reconnect_backoff.record_success(db_node.id)
            async with GetDB() as db:
                logger.info(f"Node '{db_node.name}' have been recovered")
                node_version, core_version = await node.get_versions()
                # Connection restored without a hard reset. Suppress the default
                # connect notification and send a distinct "recovered" one instead,
                # so a self-recovery is visibly different from a full reconnect.
                await NodeOperation._update_single_node_status(
                    db,
                    db_node.id,
                    NodeStatus.connected,
                    xray_version=core_version,
                    node_version=node_version,
                    send_notification=False,
                )
            if shared_state is not None:
                await node.update_observed_lifecycle(LifecycleStatus.HEALTHY, expected_epoch=shared_state.epoch)
            await notification.recovered_node(
                NodeNotification(
                    id=db_node.id,
                    name=db_node.name,
                    xray_version=core_version,
                    node_version=node_version,
                )
            )
            return


async def check_node_limits():
    """
    Check nodes that have exceeded their data limit and update status to limited.
    """

    async with GetDB() as db:
        limited_nodes = await get_limited_nodes(db)

        for db_node in limited_nodes:
            # Disconnect the node first (stop it from running)
            await node_operator.disconnect_single_node(db_node.id)

            # Update status to limited
            await NodeOperation._update_single_node_status(
                db, db_node.id, NodeStatus.limited, message="Data limit exceeded", send_notification=False
            )

            # Send notification
            node_notif = NodeNotification(
                id=db_node.id, name=db_node.name, xray_version=db_node.xray_version, node_version=db_node.node_version
            )
            await notification.limited_node(node_notif, db_node.data_limit, db_node.used_traffic)

            logger.info(f'Node "{db_node.name}" (ID: {db_node.id}) marked as limited due to data limit')


def startup_connect_in_progress() -> bool:
    return _startup_connect_task is not None and not _startup_connect_task.done()


async def node_health_check():
    """
    Cron job that checks health of all enabled nodes.
    """
    if not runtime_settings.role.runs_node:
        return
    if startup_connect_in_progress():
        # The startup bulk connect owns the nodes until it finishes: a parallel health
        # check would race its Start RPCs and its single bulk status update.
        logger.debug("Startup node connection still running; skipping health check")
        return
    async with GetDB() as db:
        db_nodes, _ = await get_nodes(db=db, query=NodeListQuery(status=ACTIVE_NODE_STATUSES), load_usage_logs=False)

    dict_nodes = await node_manager.get_nodes()
    check_tasks = [process_node_health_check(db_node, dict_nodes.get(db_node.id)) for db_node in db_nodes]
    await asyncio.gather(*check_tasks, return_exceptions=True)


_node_loop_tasks: list[asyncio.Task] = []
# Keep a reference so the startup connect task is not garbage collected mid-flight.
_startup_connect_task: asyncio.Task | None = None


async def _interval_loop(coro, seconds: float, name: str):
    """Run node maintenance on every worker (APScheduler may be leader-only)."""
    while True:
        try:
            await coro()
        except Exception as exc:
            logger.error("Node loop %s failed: %s", name, exc)
        await asyncio.sleep(seconds)


async def connect_nodes_on_startup() -> None:
    """Connect every active node once, off the lifespan path.

    uvicorn binds its socket only after lifespan startup returns, so this must not
    be awaited by the startup hook: with a couple of dead nodes the per-node connect
    timeouts used to add up to minutes of total API outage on every restart.
    """
    startup_log = logger.debug if server_settings.workers > 1 else logger.info
    startup_log("Starting nodes' cores in the background...")
    try:
        async with GetDB() as db:
            db_nodes, _ = await get_nodes(
                db=db, query=NodeListQuery(status=ACTIVE_NODE_STATUSES), load_usage_logs=False
            )

            if not db_nodes:
                logger.warning("Attention: You have no node, you need to have at least one node")
                return

            await node_operator.connect_nodes_bulk(db, db_nodes)
        startup_log("All nodes' cores have been started.")
    except asyncio.CancelledError:
        logger.info("Startup node connection cancelled by shutdown")
        raise
    except Exception as exc:
        logger.error("Startup node connection failed: %s", exc)


@on_startup
async def initialize_nodes():
    if not runtime_settings.role.runs_node:
        return

    await ensure_bridge_memory()

    # Return immediately: the API must come up even when nodes are unreachable.
    global _startup_connect_task
    _startup_connect_task = asyncio.create_task(connect_nodes_on_startup(), name="node_startup_connect")
    on_shutdown(_stop_startup_connect)

    from app.nats.leader import needs_job_leader

    if needs_job_leader():
        # Every uvicorn worker must keep local node attachments healthy.
        _node_loop_tasks.append(
            asyncio.create_task(
                _interval_loop(node_health_check, job_settings.core_health_check_interval, "health"),
                name="node_health_loop",
            )
        )
    else:
        scheduler.add_job(
            node_health_check,
            "interval",
            seconds=job_settings.core_health_check_interval,
            coalesce=True,
            max_instances=1,
            id="node_health_check",
            replace_existing=True,
        )

    # Limit checks mutate node status / disconnect; run only on the leader scheduler.
    scheduler.add_job(
        check_node_limits,
        "interval",
        seconds=job_settings.check_node_limits_interval,
        coalesce=True,
        max_instances=1,
        id="check_node_limits",
        replace_existing=True,
    )

    # Multi-uvicorn workers must not Stop remote cores / clear shared sync queues on exit.
    if feature_settings.stop_nodes_on_shutdown and server_settings.workers <= 1:
        on_shutdown(shutdown_nodes)

    on_shutdown(_stop_node_loops)
    on_shutdown(shutdown_bridge_memory)


async def _stop_startup_connect():
    """Cancel a still-running startup connect so shutdown does not race it."""
    global _startup_connect_task
    task = _startup_connect_task
    _startup_connect_task = None
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _stop_node_loops():
    for task in _node_loop_tasks:
        task.cancel()
    if _node_loop_tasks:
        await asyncio.gather(*_node_loop_tasks, return_exceptions=True)
    _node_loop_tasks.clear()


async def shutdown_nodes():
    if not runtime_settings.role.runs_node:
        return
    if is_multi_worker() and server_settings.workers > 1:
        logger.info("Skipping remote node stop on multi-worker shutdown")
        return

    logger.info("Stopping nodes' cores...")

    nodes: dict[int, PasarGuardNode] = await node_manager.get_nodes()

    stop_tasks = [node.stop() for node in nodes.values()]

    # Run all tasks concurrently and wait for them to complete
    await asyncio.gather(*stop_tasks, return_exceptions=True)

    logger.info("All nodes' cores have been stopped.")
