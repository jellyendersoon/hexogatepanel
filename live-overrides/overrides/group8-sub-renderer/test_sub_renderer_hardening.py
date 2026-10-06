#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from stat import S_IMODE

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DBAPIError

import sub_renderer
from app.db.base import engine
from app.operation.permissions import LimitExceeded, PermissionDenied
from config import database_settings


def request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/sub/simulated",
            "raw_path": b"/sub/simulated",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1),
            "server": ("localhost", 80),
        }
    )


async def main() -> None:
    handlers = sub_renderer.app.exception_handlers
    for exception_type in (
        RequestValidationError,
        DBAPIError,
        PermissionDenied,
        LimitExceeded,
    ):
        if exception_type not in handlers:
            raise RuntimeError(f"missing handler for {exception_type.__name__}")

    simulated = DBAPIError(
        "SELECT 1",
        {},
        RuntimeError("simulated read-only failure"),
        connection_invalidated=False,
    )
    response = await handlers[DBAPIError](request(), simulated)
    if response.status_code != 503:
        raise RuntimeError("DBAPIError handler did not return 503")
    if json.loads(response.body) != {"detail": "Database temporarily unavailable"}:
        raise RuntimeError("DBAPIError handler body differs from production contract")

    socket_path = Path("/var/lib/pasarguard/sub-renderer-canary.socket")
    socket_stat = socket_path.stat()
    if S_IMODE(socket_stat.st_mode) != 0o660:
        raise RuntimeError("UDS mode is not 0660")
    if socket_stat.st_uid != 0 or socket_stat.st_gid != 0:
        raise RuntimeError("UDS is not root-owned")

    pool = engine.sync_engine.pool
    if database_settings.pool_size != 10 or database_settings.max_overflow != 20:
        raise RuntimeError("database pool settings do not match the shadow bounds")
    if pool.size() != 10 or getattr(pool, "_max_overflow", None) != 20:
        raise RuntimeError("live SQLAlchemy pool does not match the shadow bounds")

    print(
        json.dumps(
            {
                "handlers": 4,
                "simulated_db_status": response.status_code,
                "socket_mode": oct(S_IMODE(socket_stat.st_mode)),
                "socket_owner": "root:root",
                "pool_size": pool.size(),
                "max_overflow": getattr(pool, "_max_overflow", None),
                "pool_status": pool.status(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
