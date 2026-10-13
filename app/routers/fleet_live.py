"""Live fleet traffic for the main dashboard.

POST /api/fleet/live/ingest  one sample from a server's hexogate-livestats reporter (shared token, not an admin login)
GET  /api/fleet/live         every server right now: speed, TCP/UDP counts, CPU, memory, a 30-minute sparkline and the
                             established connections per inbound
GET  /api/fleet/live/history one-minute series for the last hours, for one server or the whole fleet
GET  /api/fleet/live/users   the busiest users right now (from the usage recording job), with the servers they are on
"""

import hmac
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from sqlalchemy import select

from app.db import GetDB
from app.db.models import Admin, CoreConfig, Node, User
from app.models.admin import AdminDetails
from app.operation import OperatorType
from app.operation.node import NodeOperation
from app.utils.fleet_live import OFFLINE_SECONDS, live_users, store

from .authentication import require_permission, require_scope_all

router = APIRouter(tags=["Fleet"], prefix="/api/fleet/live")
node_operator = NodeOperation(operator_type=OperatorType.API)

_TOPOLOGY_TTL = 60
_topology: dict[str, Any] = {"at": 0.0, "servers": {}}
_FALLBACK_TTL = 5
_fallback: dict[str, Any] = {"at": 0.0, "stats": {}}


def _ingest_token() -> str:
    return os.environ.get("FLEET_LIVE_TOKEN", "")


async def _load_topology() -> dict[str, dict[str, Any]]:
    """address -> {nodes: [{id, name, status}], ports: {port: [inbound tags]}} from the nodes and their cores."""
    now = time.time()
    if now - _topology["at"] < _TOPOLOGY_TTL and _topology["servers"]:
        return _topology["servers"]
    servers: dict[str, dict[str, Any]] = {}
    async with GetDB() as db:
        rows = (await db.execute(select(Node.id, Node.name, Node.address, Node.status, Node.core_config_id))).all()
        core_ids = {row.core_config_id for row in rows if row.core_config_id}
        cores: dict[int, dict[str, Any]] = {}
        if core_ids:
            for core_id, config in (
                await db.execute(select(CoreConfig.id, CoreConfig.config).where(CoreConfig.id.in_(core_ids)))
            ).all():
                cores[core_id] = config if isinstance(config, dict) else {}
    for row in rows:
        status_value = getattr(row.status, "value", str(row.status))
        server = servers.setdefault(row.address, {"nodes": [], "ports": {}})
        server["nodes"].append({"id": row.id, "name": row.name, "status": status_value})
        if status_value == "disabled":
            continue
        for inbound in cores.get(row.core_config_id, {}).get("inbounds", []) or []:
            port = inbound.get("port")
            tag = inbound.get("tag")
            if isinstance(port, int) and tag:
                stream = inbound.get("streamSettings") or {}
                label = {
                    "tag": tag,
                    "protocol": inbound.get("protocol") or "",
                    "network": stream.get("network") or "tcp",
                    "security": stream.get("security") or "none",
                    "node_id": row.id,
                }
                server["ports"].setdefault(port, []).append(label)
    _topology.update(at=now, servers=servers)
    return servers


async def _node_stats_fallback() -> dict[int, Any]:
    """Speed/CPU/memory from the node API, for servers that do not run the reporter yet."""
    now = time.time()
    if now - _fallback["at"] < _FALLBACK_TTL:
        return _fallback["stats"]
    try:
        stats = await node_operator.get_nodes_system_stats()
    except Exception:
        stats = {}
    _fallback.update(at=now, stats=stats or {})
    return _fallback["stats"]


def _server_name(nodes: list[dict[str, Any]], address: str) -> str:
    names = [node["name"] for node in nodes if node["status"] != "disabled"] or [node["name"] for node in nodes]
    if not names:
        return address
    first = names[0]
    for separator in (" ▸ ", " - ", " | "):
        if separator in first:
            return first.split(separator, 1)[0].strip()
    return first


@router.post("/ingest", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False)
async def ingest_sample(request: Request, x_fleet_token: str = Header(default="")):
    token = _ingest_token()
    if not token or not hmac.compare_digest(x_fleet_token.encode(), token.encode()):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    body = await request.body()
    if len(body) > 16384:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Too large")
    try:
        sample = await request.json()
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")
    key = str(sample.get("server") or "").strip()[:64] if isinstance(sample, dict) else ""
    if not key:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="server is required")
    store.ingest(key, sample)


@router.get("", response_model=dict)
async def fleet_live(_: AdminDetails = Depends(require_permission("nodes", "stats"))):
    """Every server right now, busiest first, plus fleet totals."""
    now = time.time()
    store.load()
    topology = await _load_topology()
    fallback: dict[int, Any] | None = None
    servers: list[dict[str, Any]] = []
    totals = {"rx_bps": 0, "tx_bps": 0, "tcp": 0, "udp": 0, "servers": 0, "reporting": 0}

    keys = set(topology) | set(store.servers)
    for key in keys:
        topo = topology.get(key, {"nodes": [], "ports": {}})
        nodes = topo["nodes"]
        active_nodes = [node for node in nodes if node["status"] != "disabled"]
        state = store.servers.get(key)
        if not active_nodes and (state is None or now - state.last_ts > OFFLINE_SECONDS):
            continue  # a disabled/retired server that is not reporting: not part of the live view
        entry: dict[str, Any] = {
            "key": key,
            "name": _server_name(nodes, key),
            "nodes": nodes,
            "status": "no_reporter",
            "age": None,
            "rx_bps": None,
            "tx_bps": None,
            "tcp": None,
            "udp": None,
            "cpu": None,
            "steal": None,
            "cores": None,
            "mem_used": None,
            "mem_total": None,
            "load1": None,
            "spark": [],
            "inbounds": [],
        }
        if state is not None and state.last_ts:
            entry["status"] = store.status(state, now)
            entry["age"] = round(now - state.last_ts, 1)
            entry["spark"] = state.spark(now)
            if entry["status"] != "offline":
                last = state.last
                entry.update(
                    rx_bps=round(last["rx_bps"]),
                    tx_bps=round(last["tx_bps"]),
                    tcp=round(last["tcp"]),
                    udp=round(last["udp"]),
                    cpu=round(last["cpu"], 1),
                    steal=round(last["steal"], 1),
                    cores=state.cores or None,
                    mem_used=round(last["mem_used"]),
                    mem_total=round(last["mem_total"]),
                    load1=round(last["load1"], 2),
                )
                inbounds = []
                known = topo["ports"]
                other = 0
                for port, count in state.ports.items():
                    labels = known.get(port)
                    if not labels:
                        other += count
                        continue
                    inbounds.append({"port": port, "connections": count, **labels[0]})
                inbounds.sort(key=lambda item: item["connections"], reverse=True)
                if other:
                    inbounds.append({"port": 0, "connections": other, "tag": "other", "protocol": "", "network": ""})
                entry["inbounds"] = inbounds
                entry["inbounds_age"] = round(now - state.ports_ts, 1) if state.ports_ts else None
        if entry["rx_bps"] is None and active_nodes:
            if fallback is None:
                fallback = await _node_stats_fallback()
            rx = tx = 0
            cpu = cores = mem_used = mem_total = None
            found = False
            for node in active_nodes:
                stats = fallback.get(node["id"])
                if stats is None:
                    continue
                found = True
                # several nodes on one server report the same machine: take the largest, never the sum
                rx = max(rx, stats.incoming_bandwidth_speed * 8)
                tx = max(tx, stats.outgoing_bandwidth_speed * 8)
                cpu, cores = stats.cpu_usage, stats.cpu_cores
                mem_used, mem_total = stats.mem_used, stats.mem_total
            if found:
                entry.update(
                    rx_bps=rx, tx_bps=tx, cpu=round(cpu, 1) if cpu is not None else None, cores=cores,
                    mem_used=mem_used, mem_total=mem_total,
                )  # fmt: skip
        totals["servers"] += 1
        if entry["status"] in ("live", "stale"):
            totals["reporting"] += 1
        for name in ("rx_bps", "tx_bps", "tcp", "udp"):
            if entry[name] is not None:
                totals[name] += entry[name]
        servers.append(entry)

    servers.sort(key=lambda item: (item["rx_bps"] or 0) + (item["tx_bps"] or 0), reverse=True)
    return {"generated": round(now, 1), "totals": totals, "servers": servers}


@router.get("/history", response_model=dict)
async def fleet_live_history(
    server: str | None = Query(default=None, max_length=64),
    minutes: int = Query(default=360, ge=10, le=1440),
    _: AdminDetails = Depends(require_permission("nodes", "stats")),
):
    """One-minute averages: [timestamp, download bit/s, upload bit/s, TCP connections, UDP flows]."""
    return {"server": server, "minutes": minutes, "points": store.history(server or None, minutes)}


async def _load_users(uids: list[int]) -> dict[int, dict[str, Any]]:
    if not uids:
        return {}
    async with GetDB() as db:
        rows = (
            await db.execute(
                select(User.id, User.username, User.status, User.used_traffic, User.data_limit, Admin.username)
                .outerjoin(Admin, Admin.id == User.admin_id)
                .where(User.id.in_(uids))
            )
        ).all()
    return {
        row[0]: {
            "username": row[1],
            "status": getattr(row[2], "value", str(row[2])),
            "used_traffic": row[3],
            "data_limit": row[4],
            "admin": row[5],
        }
        for row in rows
    }


@router.get("/users", response_model=dict)
async def fleet_live_users(
    limit: int = Query(default=10, ge=1, le=50),
    span: str = Query(default="now", pattern="^(now|10m)$"),
    _: AdminDetails = Depends(require_scope_all("users", "read")),
):
    """The busiest users: average speed (download + upload) over the last usage interval or the last ten minutes."""
    result = live_users.top(limit=limit, span=span)
    topology = await _load_topology()
    node_names = {
        node["id"]: _server_name(server["nodes"], address) for address, server in topology.items() for node in server["nodes"]
    }  # fmt: skip
    details = await _load_users([user["uid"] for user in result["users"]])
    users = []
    for user in result["users"]:
        servers: dict[str, int] = {}
        for node_id, value in user["nodes"]:
            name = node_names.get(node_id, f"node {node_id}")
            servers[name] = servers.get(name, 0) + value
        detail = details.get(user["uid"], {})
        users.append(
            {
                "id": user["uid"],
                "username": detail.get("username") or f"#{user['uid']}",
                "admin": detail.get("admin"),
                "status": detail.get("status"),
                "used_traffic": detail.get("used_traffic"),
                "data_limit": detail.get("data_limit"),
                "bps": user["bps"],
                "bytes": user["bytes"],
                "kept_bytes": user["kept_bytes"],
                "servers": [{"name": name, "bytes": value} for name, value in servers.items()],
                "spark": user["spark"],
            }
        )
    return {**result, "span": span, "users": users}
