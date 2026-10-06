"""Subscription-only renderer entrypoint: exception contract, socket hardening and catalog refresh."""

from __future__ import annotations

import importlib
import json
import os
import socket
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import DBAPIError

import sub_renderer
from app.core.hosts import host_manager
from app.operation.permissions import LimitExceeded, PermissionDenied
from config import subscription_env_settings

IS_ROOT = os.geteuid() == 0


def _request() -> Request:
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


# ---------------------------------------------------------------- app contract


def test_app_is_subscription_only_without_api_docs():
    assert sub_renderer.app.docs_url is None
    assert sub_renderer.app.redoc_url is None
    assert sub_renderer.app.openapi_url is None
    paths = set()
    for route in sub_renderer.app.routes:
        included = getattr(route, "original_router", None)
        paths.update(getattr(inner, "path", "") for inner in (included.routes if included else [route]))
    prefix = f"/{subscription_env_settings.path}"
    assert any(path.startswith(f"{prefix}/") for path in paths)
    assert not any(path.startswith("/api/") for path in paths)
    assert not any(path.startswith("/dashboard") for path in paths)


async def test_exception_handlers_match_the_production_contract():
    handlers = sub_renderer.app.exception_handlers
    for exception_type in (RequestValidationError, DBAPIError, PermissionDenied, LimitExceeded):
        assert exception_type in handlers, exception_type.__name__

    simulated = DBAPIError("SELECT 1", {}, RuntimeError("simulated read-only failure"), connection_invalidated=False)
    response = await handlers[DBAPIError](_request(), simulated)
    assert response.status_code == 503
    assert json.loads(response.body) == {"detail": "Database temporarily unavailable"}

    response = await handlers[PermissionDenied](_request(), PermissionDenied("nope"))
    assert response.status_code == 403
    assert json.loads(response.body) == {"detail": "nope"}

    response = await handlers[LimitExceeded](_request(), LimitExceeded("too many"))
    assert response.status_code == 400
    assert json.loads(response.body) == {"detail": "too many"}

    validation = RequestValidationError([{"loc": ("query", "token"), "msg": "field required"}])
    response = handlers[RequestValidationError](_request(), validation)
    assert response.status_code == 422
    assert json.loads(response.body) == {"detail": {"token": "field required"}}


# ---------------------------------------------------------------- socket path and hardening


def test_socket_path_defaults_to_the_production_name_and_honours_env(monkeypatch):
    assert sub_renderer.DEFAULT_SOCKET_PATH == "/var/lib/pasarguard/sub-renderer-canary.socket"
    assert sub_renderer.REFRESH_INTERVAL_SECONDS == 30
    assert sub_renderer.SOCKET_MODE == 0o660
    original_env = os.environ.get("SUB_RENDERER_SOCKET")
    try:
        monkeypatch.delenv("SUB_RENDERER_SOCKET", raising=False)
        module = importlib.reload(sub_renderer)
        assert module.SOCKET_PATH == Path(sub_renderer.DEFAULT_SOCKET_PATH)
        monkeypatch.setenv("SUB_RENDERER_SOCKET", "/run/custom.socket")
        module = importlib.reload(sub_renderer)
        assert module.SOCKET_PATH == Path("/run/custom.socket")
    finally:
        if original_env is None:
            monkeypatch.delenv("SUB_RENDERER_SOCKET", raising=False)
        else:
            monkeypatch.setenv("SUB_RENDERER_SOCKET", original_env)
        importlib.reload(sub_renderer)


@pytest.mark.skipif(not IS_ROOT, reason="socket ownership check needs root")
def test_bind_socket_creates_a_root_owned_0660_socket(monkeypatch, tmp_path):
    path = tmp_path / "renderer.socket"
    monkeypatch.setattr(sub_renderer, "SOCKET_PATH", path)
    listener = sub_renderer._bind_socket()
    try:
        assert path.is_socket()
        socket_stat = path.stat()
        assert S_IMODE(socket_stat.st_mode) == 0o660
        assert socket_stat.st_uid == 0 and socket_stat.st_gid == 0
        assert listener.family == socket.AF_UNIX
        # The listener accepts connections before uvicorn starts.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(path))
    finally:
        listener.close()

    # A stale root-owned socket is replaced on the next start.
    listener = sub_renderer._bind_socket()
    listener.close()
    assert path.is_socket()


@pytest.mark.skipif(not IS_ROOT, reason="socket ownership check needs root")
def test_harden_socket_rejects_wrong_mode_or_owner(monkeypatch, tmp_path):
    path = tmp_path / "renderer.socket"
    monkeypatch.setattr(sub_renderer, "SOCKET_PATH", path)
    listener = sub_renderer._bind_socket()
    try:
        path.chmod(0o666)
        sub_renderer._harden_socket()  # re-tightens the mode
        assert S_IMODE(path.stat().st_mode) == 0o660
        os.chown(path, 65534, 65534)
        with pytest.raises(RuntimeError, match="unsafe"):
            sub_renderer._harden_socket()
    finally:
        listener.close()


def test_bind_socket_refuses_to_replace_a_non_socket_path(monkeypatch, tmp_path):
    path = tmp_path / "renderer.socket"
    path.write_text("not a socket")
    monkeypatch.setattr(sub_renderer, "SOCKET_PATH", path)
    with pytest.raises(RuntimeError, match="refusing to replace"):
        sub_renderer._bind_socket()
    assert path.read_text() == "not a socket"


def test_harden_socket_requires_a_bound_socket(monkeypatch, tmp_path):
    monkeypatch.setattr(sub_renderer, "SOCKET_PATH", tmp_path / "missing.socket")
    with pytest.raises(RuntimeError, match="unavailable"):
        sub_renderer._harden_socket()


# ---------------------------------------------------------------- refresh


def test_refresh_heartbeat_is_written_atomically(monkeypatch, tmp_path):
    heartbeat = tmp_path / "generation"
    monkeypatch.setattr(sub_renderer, "REFRESH_HEARTBEAT_PATH", heartbeat)
    monkeypatch.setattr(sub_renderer, "_refresh_generation", 0)
    sub_renderer._record_refresh_success()
    sub_renderer._record_refresh_success()
    assert heartbeat.read_text() == "2\n"
    assert not (tmp_path / ".generation.tmp").exists()


async def test_refresh_catalog_replaces_the_host_view_without_db_writes(monkeypatch):
    class FakeDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db_rows = [object(), object(), object()]
    seven = SimpleNamespace(priority=2)
    nine = SimpleNamespace(priority=1)
    prepared = {id(db_rows[0]): (7, seven), id(db_rows[1]): None, id(db_rows[2]): (9, nine)}

    monkeypatch.setattr(sub_renderer, "GetDB", FakeDB)
    monkeypatch.setattr(sub_renderer.core_manager, "initialize", AsyncMock())
    monkeypatch.setattr(sub_renderer.core_manager, "get_inbounds", AsyncMock(return_value=["in-a"]))
    monkeypatch.setattr(sub_renderer, "get_hosts", AsyncMock(return_value=db_rows))
    monkeypatch.setattr(sub_renderer.BaseHost, "model_validate", staticmethod(lambda row: row))

    async def prepare(db, host, inbound_tags):
        assert inbound_tags == ["in-a"]
        return prepared[id(host)]

    monkeypatch.setattr(host_manager, "_prepare_host_entry", prepare)
    monkeypatch.setattr(host_manager, "_hosts", {1: "stale"})
    await host_manager._reset_cache()

    await sub_renderer._refresh_catalog()

    assert host_manager._hosts == {7: seven, 9: nine}
    # The get_hosts cache was reset: the new catalog is served, priority-sorted.
    assert list((await host_manager.get_hosts()).items()) == [(9, nine), (7, seven)]
    sub_renderer.core_manager.initialize.assert_awaited_once()
    await host_manager._reset_cache()
