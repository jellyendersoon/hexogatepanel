import asyncio
from dataclasses import dataclass

from aiorwlock import RWLock
from PasarGuardNodeBridge import Health, NodeType, PasarGuardNode, create_node
from PasarGuardNodeBridge.common.service_pb2 import User as ProtoUser

from app.db.models import Node, NodeConnectionType
from app.node.nats_memory import ensure_bridge_memory, get_bridge_memory
from app.node.user import core_users
from app.utils.logger import get_logger
from config import nats_settings, node_settings

type_map = {
    NodeConnectionType.rest: NodeType.rest,
    NodeConnectionType.grpc: NodeType.grpc,
}


class NodeManager:
    def __init__(self):
        self._nodes: dict[int, PasarGuardNode] = {}
        self._node_signatures: dict[int, tuple] = {}
        self._user_sync_locks: dict[int, asyncio.Lock] = {}
        self._lock = RWLock(fast=True)
        self.logger = get_logger("node-manager")

    @staticmethod
    def _connection_signature(node: Node) -> tuple:
        """Fields that, if changed, actually require tearing down the remote backend."""
        return (
            node.connection_type,
            node.address,
            node.port,
            node.api_port,
            node.server_ca,
            node.api_key,
            node.default_timeout,
            node.internal_timeout,
            node.proxy_url,
        )

    def _create_node_kwargs(self, node: Node) -> dict:
        kwargs = {
            "connection": type_map[node.connection_type],
            "address": node.address,
            "port": node.port,
            "api_port": node.api_port,
            "server_ca": node.server_ca,
            "api_key": node.api_key,
            "name": node.name,
            "logger": self.logger,
            "default_timeout": node.default_timeout,
            "internal_timeout": node.internal_timeout,
            "proxy": node.proxy_url,
            "extra": {"id": node.id, "usage_coefficient": node.usage_coefficient},
            "node_id": str(node.id),
        }
        store, coordinator, worker_id = get_bridge_memory()
        if store is not None and coordinator is not None:
            kwargs["user_sync_store"] = store
            kwargs["lifecycle_coordinator"] = coordinator
            kwargs["worker_id"] = worker_id
        return kwargs

    async def _shutdown_node(self, node: PasarGuardNode | None, *, remote_stop: bool = True):
        if node is None:
            return

        try:
            await node.set_health(Health.INVALID)
            if remote_stop:
                await node.stop()
        except Exception:
            pass

    async def update_node(self, node: Node) -> PasarGuardNode:
        await ensure_bridge_memory()

        # Serialize against in-flight full syncs (sync_full) so a reconnect/health-check
        # restart doesn't swap the node object out from under a slow peer sync — that race
        # is what turns a slow sync into a stop/start restart loop.
        lock = self._user_sync_locks.setdefault(node.id, asyncio.Lock())
        async with lock:
            signature = self._connection_signature(node)
            async with self._lock.reader_lock:
                existing = self._nodes.get(node.id)

            # update_node() runs on every reconnect attempt, including the automated
            # ones the health-check watchdog fires every ~minute. If nothing about the
            # connection actually changed, reuse the live object instead of killing a
            # possibly-healthy remote backend (a real Stop RPC) just to recreate it —
            # that used to defeat the attach-if-already-running logic below and turned
            # transient health-check false negatives into a permanent restart loop.
            if existing is not None and self._node_signatures.get(node.id) == signature:
                existing_extra = await existing.get_extra()
                if existing.name == node.name and existing_extra.get("usage_coefficient") == node.usage_coefficient:
                    return existing

            async with self._lock.writer_lock:
                old_node: PasarGuardNode | None = self._nodes.pop(node.id, None)

                new_node = create_node(**self._create_node_kwargs(node))

                self._nodes[node.id] = new_node
                self._node_signatures[node.id] = signature

            # Stop the old node after releasing the lock.
            await self._shutdown_node(old_node)

        return new_node

    async def remove_node(self, id: int, *, remote_stop: bool = True) -> None:
        # Serialize against in-flight sync_full/update_node the same way update_node does,
        # so removal can't tear the node down mid-sync and can't drop the lock entry while
        # a current waiter still holds that lock identity.
        lock = self._user_sync_locks.setdefault(id, asyncio.Lock())
        async with lock, self._lock.writer_lock:
            old_node: PasarGuardNode | None = self._nodes.pop(id, None)
            self._node_signatures.pop(id, None)
            self._user_sync_locks.pop(id, None)

        # Do cleanup without holding the lock to avoid slow delete operations.
        asyncio.create_task(self._shutdown_node(old_node, remote_stop=remote_stop))

    async def get_node(self, id: int) -> PasarGuardNode | None:
        async with self._lock.reader_lock:
            return self._nodes.get(id, None)

    async def get_nodes(self) -> dict[int, PasarGuardNode]:
        async with self._lock.reader_lock:
            return self._nodes

    async def get_healthy_nodes(self) -> list[tuple[int, PasarGuardNode]]:
        async with self._lock.reader_lock:
            nodes: list[tuple[int, PasarGuardNode]] = [
                (id, node) for id, node in self._nodes.items() if (await node.get_health() == Health.HEALTHY)
            ]
            return nodes

    async def get_broken_nodes(self) -> list[tuple[int, PasarGuardNode]]:
        async with self._lock.reader_lock:
            nodes: list[tuple[int, PasarGuardNode]] = [
                (id, node) for id, node in self._nodes.items() if (await node.get_health() == Health.BROKEN)
            ]
            return nodes

    async def get_not_connected_nodes(self) -> list[tuple[int, PasarGuardNode]]:
        async with self._lock.reader_lock:
            nodes: list[tuple[int, PasarGuardNode]] = [
                (id, node) for id, node in self._nodes.items() if (await node.get_health() == Health.NOT_CONNECTED)
            ]
            return nodes

    async def _snapshot_nodes(self) -> list[PasarGuardNode]:
        async with self._lock.reader_lock:
            return list(self._nodes.values())

    async def _snapshot_node_items(self) -> list[tuple[int, PasarGuardNode]]:
        async with self._lock.reader_lock:
            return list(self._nodes.items())

    @staticmethod
    def _chunk_users(users: list[ProtoUser], size: int) -> list[list[ProtoUser]]:
        return [users[start : start + size] for start in range(0, len(users), size)]

    async def _sync_user_batch_to_node(self, node: PasarGuardNode, batch: list[ProtoUser]) -> int:
        users_to_sync = batch
        supports_chunked = True
        supports_chunked_check = getattr(node, "_supports_chunked_sync", None)
        if callable(supports_chunked_check):
            supports_chunked, _ = await supports_chunked_check()

        if supports_chunked:
            users_to_sync = await node.sync_users_chunked(
                batch,
                chunk_size=len(batch),
                flush_pending=False,
            )
            if not users_to_sync:
                return 0

        sync_batch_users = getattr(node, "_sync_batch_users", None)
        if callable(sync_batch_users):
            users_to_sync = await sync_batch_users(users_to_sync)

        return len(users_to_sync)

    async def _sync_users_to_node(self, node_id: int, node: PasarGuardNode, users: list[ProtoUser]):
        batch_size = max(1, nats_settings.node_update_users_batch_size)
        lock = self._user_sync_locks.setdefault(node_id, asyncio.Lock())
        failed_count = 0

        async with lock:
            for batch in self._chunk_users(users, batch_size):
                failed_count += await self._sync_user_batch_to_node(node, batch)

        if failed_count:
            raise RuntimeError(f"failed to sync {failed_count}/{len(users)} users to node {node_id}")

    async def sync_full(
        self, node_id: int, users: list[ProtoUser], *, flush_pending: bool = False
    ) -> PasarGuardNode | None:
        """Push a full user snapshot to a node, serialized against update_node/remove_node.

        Guards against the reconnect/health-check watchdog tearing down the node object
        mid-sync (which previously restarted the sync from scratch and could loop).
        """
        lock = self._user_sync_locks.setdefault(node_id, asyncio.Lock())
        async with lock:
            node = await self.get_node(node_id)
            if node is None:
                return None
            await node.sync_users(users, flush_pending=flush_pending)
            return node

    async def _update_users(self, users: list[ProtoUser]):
        nodes = await self._snapshot_node_items()
        if not nodes:
            return

        results = await asyncio.gather(
            *(self._sync_users_to_node(node_id, node, users) for node_id, node in nodes), return_exceptions=True
        )
        for result in results:
            if isinstance(result, Exception):
                self.logger.error("Failed to sync users to one of the nodes: %s", result)

    async def update_users(self, users: list[ProtoUser]) -> None:
        asyncio.create_task(self._update_users(users))

    async def update_user(self, user: ProtoUser) -> None:
        nodes = await self._snapshot_nodes()
        if not nodes:
            return

        results = await asyncio.gather(*(node.update_user(user) for node in nodes), return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                raise result


@dataclass(slots=True)
class _ReconnectState:
    failures: int = 0
    attempts: int = 0
    next_attempt_at: int = 0


class NodeReconnectBackoff:
    """Decide when the health check may auto-reconnect a node that keeps failing.

    Failed checks are counted per node. The first reconnect fires once a node has
    failed `after_failures` consecutive checks; every later attempt waits
    min(2 ** attempts, max_backoff_checks) more failed checks, so a host that is
    really down is retried with exponential backoff instead of on every check.
    A successful check, or a manual reconnect, clears the node's state.
    """

    def __init__(self, after_failures: int, max_backoff_checks: int):
        self.after_failures = max(1, after_failures)
        self.max_backoff_checks = max(1, max_backoff_checks)
        self._states: dict[int, _ReconnectState] = {}

    def record_failure(self, node_id: int) -> bool:
        """Count one failed check and return True when a reconnect should be attempted now."""
        state = self._states.get(node_id)
        if state is None:
            state = _ReconnectState(next_attempt_at=self.after_failures)
            self._states[node_id] = state

        state.failures += 1
        if state.failures < state.next_attempt_at:
            return False

        state.attempts += 1
        state.next_attempt_at = state.failures + min(2**state.attempts, self.max_backoff_checks)
        return True

    def record_success(self, node_id: int) -> None:
        self._states.pop(node_id, None)

    def reset(self, node_id: int) -> None:
        """Forget a node's failures, e.g. after an admin reconnected it by hand."""
        self._states.pop(node_id, None)

    def reset_all(self) -> None:
        self._states.clear()

    def failures(self, node_id: int) -> int:
        state = self._states.get(node_id)
        return state.failures if state else 0

    def attempts(self, node_id: int) -> int:
        state = self._states.get(node_id)
        return state.attempts if state else 0

    def checks_until_next_attempt(self, node_id: int) -> int:
        state = self._states.get(node_id)
        if state is None:
            return self.after_failures
        return max(0, state.next_attempt_at - state.failures)


node_manager: NodeManager = NodeManager()
reconnect_backoff: NodeReconnectBackoff = NodeReconnectBackoff(
    after_failures=node_settings.auto_reconnect_after_failures,
    max_backoff_checks=node_settings.auto_reconnect_max_backoff_checks,
)


__all__ = ["NodeReconnectBackoff", "core_users", "node_manager", "reconnect_backoff"]
