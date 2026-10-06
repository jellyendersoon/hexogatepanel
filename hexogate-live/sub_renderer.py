"""Subscription-only PasarGuard renderer.

This process intentionally excludes the panel dashboard, Telegram manager,
node worker, scheduler, and lifecycle hooks.  It is suitable for an isolated
shadow UDS used to validate presentation-only subscription changes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
from pathlib import Path
import socket
from stat import S_IMODE, S_ISSOCK

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
import uvicorn

from app.app_factory import database_operational_error_handler
from app.core.hosts import host_manager
from app.core.manager import core_manager
from app.db import GetDB
from app.db.crud.host import get_hosts
from app.models.host import BaseHost
from app.operation.permissions import LimitExceeded, PermissionDenied
from app.routers.subscription import router as subscription_router
from app.settings import refresh_caches
from app.subscription.client_templates import refresh_client_templates_cache
from app.utils.logger import get_logger


SOCKET_PATH = Path("/var/lib/pasarguard/sub-renderer-canary.socket")
REFRESH_HEARTBEAT_PATH = Path("/tmp/pasarguard-sub-renderer-refresh-generation")
logger = get_logger("subscription-renderer")
_refresh_generation = 0


def _record_refresh_success() -> None:
    """Expose a credential-free, container-local refresh generation for health proof."""
    global _refresh_generation
    _refresh_generation += 1
    temporary = REFRESH_HEARTBEAT_PATH.with_name(f".{REFRESH_HEARTBEAT_PATH.name}.tmp")
    temporary.write_text(f"{_refresh_generation}\n", encoding="ascii")
    temporary.replace(REFRESH_HEARTBEAT_PATH)


def _harden_socket() -> None:
    """Restrict the already-bound UDS before the app accepts requests."""
    if not SOCKET_PATH.is_socket():
        raise RuntimeError("subscription renderer UDS is unavailable during startup")
    SOCKET_PATH.chmod(0o660)
    socket_stat = SOCKET_PATH.stat()
    if S_IMODE(socket_stat.st_mode) != 0o660 or socket_stat.st_uid != 0 or socket_stat.st_gid != 0:
        raise RuntimeError("subscription renderer UDS ownership or mode is unsafe")


def _bind_socket() -> socket.socket:
    """Bind the private UDS before Uvicorn starts the application lifespan."""
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        stale = SOCKET_PATH.lstat()
        if not S_ISSOCK(stale.st_mode) or stale.st_uid != 0 or stale.st_gid != 0:
            raise RuntimeError("refusing to replace an unsafe subscription renderer UDS path")
        SOCKET_PATH.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(SOCKET_PATH))
        listener.listen(2048)
        _harden_socket()
        return listener
    except Exception:
        listener.close()
        if SOCKET_PATH.is_socket():
            SOCKET_PATH.unlink()
        raise


def main() -> None:
    listener = _bind_socket()
    try:
        uvicorn.run(
            app,
            fd=listener.fileno(),
            workers=1,
            proxy_headers=True,
            forwarded_allow_ips="*",
            log_level="warning",
        )
    finally:
        listener.close()
        if SOCKET_PATH.is_socket():
            socket_stat = SOCKET_PATH.lstat()
            if socket_stat.st_uid == 0 and socket_stat.st_gid == 0:
                SOCKET_PATH.unlink()


async def _refresh_catalog() -> None:
    """Refresh the renderer's in-memory core/host view without DB mutations."""
    async with GetDB() as db:
        await core_manager.initialize(db)
        inbound_tags = await core_manager.get_inbounds()
        prepared = []
        for db_host in await get_hosts(db):
            host = BaseHost.model_validate(db_host)
            entry = await host_manager._prepare_host_entry(db, host, inbound_tags)
            if entry is not None:
                prepared.append(entry)

    replacement = {host_id: host_data for host_id, host_data in prepared}
    async with host_manager._lock:
        host_manager._hosts = replacement
        await host_manager._reset_cache()


async def _refresh_read_caches() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            await _refresh_catalog()
            await refresh_caches()
            await refresh_client_templates_cache()
            _record_refresh_success()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A transient DB/pool error must not permanently freeze the
            # subscription catalog. Keep the log bounded and retry later.
            logger.error(f"Subscription renderer refresh failed; retrying: {type(exc).__name__}")
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await _refresh_catalog()
    await refresh_caches()
    await refresh_client_templates_cache()
    _record_refresh_success()
    _harden_socket()
    task = asyncio.create_task(_refresh_read_caches())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(
    title="PasarGuard subscription renderer",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.include_router(subscription_router)


@app.exception_handler(RequestValidationError)
def validation_exception_handler(request: Request, exc: RequestValidationError):
    details = {}
    for error in exc.errors():
        details[error["loc"][-1]] = error.get("msg")
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=jsonable_encoder({"detail": details}),
    )


app.add_exception_handler(DBAPIError, database_operational_error_handler)


@app.exception_handler(PermissionDenied)
async def permission_denied_handler(request: Request, exc: PermissionDenied):
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": exc.detail},
    )


@app.exception_handler(LimitExceeded)
async def limit_exceeded_handler(request: Request, exc: LimitExceeded):
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"detail": exc.detail},
    )


if __name__ == "__main__":
    main()
