"""Live fleet traffic: in-memory store for the per-server samples pushed by the hexogate-livestats reporter.

Each exit server runs a small reporter that posts one sample every few seconds (speed, TCP/UDP counts, CPU, memory)
and, less often, the number of established connections per listening port. The panel keeps:

- a short ring of raw samples per server (about 30 minutes) for the row sparklines, and
- one-minute averages for 24 hours for the history chart, saved to disk so a panel restart keeps the history.

Everything here is process-local on purpose: the panel runs as a single API process, and losing a few seconds of
samples on restart is fine.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

RAW_SECONDS = 30 * 60
MINUTE_POINTS = 24 * 60
STALE_SECONDS = 30
OFFLINE_SECONDS = 180
SNAPSHOT_EVERY = 60
SNAPSHOT_PATH = Path(os.environ.get("FLEET_LIVE_SNAPSHOT", "/var/lib/pasarguard/fleet-live.json"))

_NUMERIC = ("rx_bps", "tx_bps", "tcp", "udp", "cpu", "steal", "mem_used", "mem_total", "load1")


def _num(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except TypeError, ValueError:
        return default
    return number if math.isfinite(number) and number >= 0 else default


@dataclass
class ServerState:
    key: str
    last: dict[str, float] = field(default_factory=dict)
    last_ts: float = 0.0
    cores: int = 0
    ports: dict[int, int] = field(default_factory=dict)
    ports_ts: float = 0.0
    raw: deque = field(default_factory=deque)  # (ts, rx_bps, tx_bps, tcp, udp)
    minutes: deque = field(default_factory=lambda: deque(maxlen=MINUTE_POINTS))  # (minute_ts, rx, tx, tcp, udp)
    _bucket: int = 0
    _acc: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])
    _count: int = 0

    def add(self, sample: dict[str, Any], now: float) -> None:
        values = {name: _num(sample.get(name)) for name in _NUMERIC}
        self.last = values
        self.last_ts = now
        self.cores = int(_num(sample.get("cores"), self.cores))
        ports = sample.get("ports")
        if isinstance(ports, dict):
            parsed: dict[int, int] = {}
            for port, count in list(ports.items())[:200]:
                try:
                    parsed[int(port)] = int(count)
                except TypeError, ValueError:
                    continue
            self.ports = parsed
            self.ports_ts = now

        point = (now, values["rx_bps"], values["tx_bps"], values["tcp"], values["udp"])
        self.raw.append(point)
        while self.raw and self.raw[0][0] < now - RAW_SECONDS:
            self.raw.popleft()

        bucket = int(now // 60)
        if self._bucket and bucket != self._bucket and self._count:
            self.minutes.append((self._bucket * 60, *[round(total / self._count) for total in self._acc]))
            self._acc = [0.0, 0.0, 0.0, 0.0]
            self._count = 0
        self._bucket = bucket
        for index, value in enumerate(point[1:]):
            self._acc[index] += value
        self._count += 1

    def spark(self, now: float, points: int = 60) -> list[int]:
        """Total speed (down + up) over the raw window, averaged into `points` slots; -1 marks an empty slot."""
        if not self.raw:
            return []
        step = RAW_SECONDS / points
        start = now - RAW_SECONDS
        sums = [0.0] * points
        counts = [0] * points
        for ts, rx, tx, _tcp, _udp in self.raw:
            slot = min(points - 1, int((ts - start) // step))
            if slot >= 0:
                sums[slot] += rx + tx
                counts[slot] += 1
        return [round(sums[i] / counts[i]) if counts[i] else -1 for i in range(points)]


class FleetLiveStore:
    def __init__(self) -> None:
        self.servers: dict[str, ServerState] = {}
        self._last_snapshot = 0.0
        self._loaded = False

    def ingest(self, key: str, sample: dict[str, Any], now: float | None = None) -> None:
        now = now or time.time()
        self.load()
        state = self.servers.get(key)
        if state is None:
            if len(self.servers) >= 200:
                return
            state = self.servers[key] = ServerState(key=key)
        state.add(sample, now)
        if now - self._last_snapshot >= SNAPSHOT_EVERY:
            self.save(now)

    def status(self, state: ServerState, now: float) -> str:
        age = now - state.last_ts
        if age <= STALE_SECONDS:
            return "live"
        return "stale" if age <= OFFLINE_SECONDS else "offline"

    def history(self, key: str | None, minutes: int, now: float | None = None) -> list[list[float]]:
        """One-minute series [ts, rx, tx, tcp, udp]; key=None sums every server."""
        now = now or time.time()
        self.load()
        since = (int(now // 60) - minutes) * 60
        if key is not None:
            state = self.servers.get(key)
            return [list(point) for point in state.minutes if point[0] >= since] if state else []
        merged: dict[int, list[float]] = {}
        for state in self.servers.values():
            for ts, rx, tx, tcp, udp in state.minutes:
                if ts < since:
                    continue
                row = merged.setdefault(ts, [0.0, 0.0, 0.0, 0.0])
                row[0] += rx
                row[1] += tx
                row[2] += tcp
                row[3] += udp
        return [[ts, *merged[ts]] for ts in sorted(merged)]

    def save(self, now: float | None = None) -> None:
        self._last_snapshot = now or time.time()
        try:
            data = {key: [list(point) for point in state.minutes] for key, state in self.servers.items()}
            tmp = SNAPSHOT_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"saved": self._last_snapshot, "minutes": data}))
            os.replace(tmp, SNAPSHOT_PATH)
        except OSError:
            pass

    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(SNAPSHOT_PATH.read_text())
        except OSError, ValueError:
            return
        cutoff = time.time() - MINUTE_POINTS * 60
        for key, points in (data.get("minutes") or {}).items():
            state = self.servers.setdefault(str(key), ServerState(key=str(key)))
            for point in points:
                if isinstance(point, list) and len(point) == 5 and point[0] >= cutoff:
                    state.minutes.append(tuple(point))


USER_WINDOWS = 20  # about ten minutes at the default 30 s usage interval
USER_STALE_SECONDS = 150


class LiveUsers:
    """Per-user traffic from the usage recording job: the bytes each user moved on each node in every interval."""

    def __init__(self) -> None:
        self.windows: deque = deque(maxlen=USER_WINDOWS)  # (ts, seconds, {uid: {node_id: bytes}})
        self._last_ts = 0.0

    def record(self, api_params: dict, now: float | None = None) -> None:
        now = now or time.time()
        previous, self._last_ts = self._last_ts, now
        if not previous:
            return  # the first report after a start covers an unknown stretch of time, so it cannot give a speed
        seconds = min(300.0, max(5.0, now - previous))
        window: dict[int, dict[int, int]] = {}
        for node_id, params in (api_params or {}).items():
            for param in params or ():
                value = int(param["value"])
                if value > 0:
                    window.setdefault(int(param["uid"]), {})[int(node_id)] = value
        self.windows.append((now, seconds, window))

    def top(self, limit: int = 10, span: str = "now", now: float | None = None) -> dict[str, Any]:
        """Busiest users over the newest interval (span="now") or over everything kept (span="10m")."""
        now = now or time.time()
        if not self.windows or now - self.windows[-1][0] > USER_STALE_SECONDS:
            return {"age": None, "seconds": 0, "active": 0, "total_bps": 0, "users": []}
        chosen = [self.windows[-1]] if span == "now" else list(self.windows)
        seconds = sum(window[1] for window in chosen)
        totals: dict[int, int] = {}
        per_node: dict[int, dict[int, int]] = {}
        for _ts, _seconds, window in chosen:
            for uid, nodes in window.items():
                totals[uid] = totals.get(uid, 0) + sum(nodes.values())
                bucket = per_node.setdefault(uid, {})
                for node_id, value in nodes.items():
                    bucket[node_id] = bucket.get(node_id, 0) + value
        ranked = sorted(totals, key=totals.__getitem__, reverse=True)[:limit]
        users = []
        for uid in ranked:
            kept = sum(sum(window.get(uid, {}).values()) for _ts, _seconds, window in self.windows)
            users.append(
                {
                    "uid": uid,
                    "bytes": totals[uid],
                    "bps": round(totals[uid] * 8 / seconds),
                    "kept_bytes": kept,
                    "nodes": sorted(per_node[uid].items(), key=lambda item: item[1], reverse=True),
                    "spark": [round(sum(w.get(uid, {}).values()) * 8 / s) for _ts, s, w in self.windows],
                }
            )
        return {
            "age": round(now - self.windows[-1][0], 1),
            "seconds": round(seconds),
            "active": len(self.windows[-1][2]),
            "total_bps": round(sum(totals.values()) * 8 / seconds),
            "users": users,
        }


store = FleetLiveStore()
live_users = LiveUsers()
