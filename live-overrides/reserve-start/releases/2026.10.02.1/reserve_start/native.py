"""Reuse the exact running panel source; never start its lifecycle or scheduler."""

import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request

from app.db import GetDB
from app.db.crud.user import _review_user_select_stmt, start_users_expire
from app.db.models import User, UserStatus
from app.models.validators import MAX_ON_HOLD_EXPIRE_DURATION_SECONDS
from app.operation import OperatorType
from app.operation.permissions import is_scope_all
from app.operation.user import UserOperation
from app.routers.authentication import require_permission_for_request

from .engine import ActivationError, utc


REFUND_NOTE = re.compile(r"refund|بازگشت\s*وجه|بازپرداخت|مسترد", re.IGNORECASE)


def credential_request(headers):
    safe = {key.lower(): value for key, value in headers.items() if key.lower() in {"authorization", "x-api-key"}}
    return Request({"type": "http", "headers": [(key.encode(), value.encode()) for key, value in safe.items()]})


class NativeHandle:
    def __init__(self, backend, db, user, admin):
        self.backend, self.db, self.user, self.admin = backend, db, user, admin

    async def snapshot(self):
        result = await self.backend.operator.validate_user(self.user)
        snapshot = result.model_dump(mode="json")
        snapshot["_next_plan_id"] = self.user.next_plan.id if self.user.next_plan else None
        snapshot["_admin_id"] = self.user.admin_id
        snapshot["_native_eligible"] = bool(self.user.become_online)
        snapshot["_refunded"] = bool(REFUND_NOTE.search(self.user.note or ""))
        return snapshot

    async def start(self):
        await start_users_expire(self.db, [self.user])
        # MySQL's current DATETIME columns persist whole seconds, while the
        # helper retains its microsecond clock in expire_on_commit=False ORM
        # state. Read the actual persisted clock before recording or comparing
        # it; keep pending-sync expiry comparisons exact rather than tolerant.
        await self.db.refresh(self.user, attribute_names=[
            "_expire", "on_hold_expire_duration", "on_hold_timeout", "status",
        ])


class NativeBackend:
    max_duration = MAX_ON_HOLD_EXPIRE_DURATION_SECONDS

    def __init__(self, *, socket_path=None, allowlist_path=None):
        self.operator = UserOperation(operator_type=OperatorType.API)
        self.socket_path = socket_path or os.environ.get(
            "RESERVE_PANEL_SOCKET", "/var/lib/pasarguard/pasarguard.socket"
        )
        self.allowlist_path = allowlist_path or os.environ.get("RESERVE_DUE_ALLOWLIST")

    @staticmethod
    async def authenticate(db, credentials):
        request = credential_request(credentials)
        scheme, _, token = credentials.get("authorization", "").partition(" ")
        bearer = token if scheme.casefold() == "bearer" else None
        return await require_permission_for_request(request, db, bearer, "users", "update")

    async def authorized_snapshot(self, username, credentials):
        async with GetDB() as db:
            admin = await self.authenticate(db, credentials)
            user = await self.operator.get_validated_user(
                db, username, admin, scope_action="update", load_lifetime_used_traffic=True,
            )
            return await NativeHandle(self, db, user, admin).snapshot()

    @asynccontextmanager
    async def locked_user(self, username, credentials):
        async with GetDB() as db:
            admin = await self.authenticate(db, credentials)
            validated = await self.operator.get_validated_user(
                db, username, admin, scope_action="update", load_lifetime_used_traffic=True,
            )
            # Re-read all scalar/relationship data under the native DB row lock.
            # A file lock also serializes adapter processes. The healthy native
            # scheduler does not take this lock, so engine.py separately defers
            # rows already eligible for its unlocked selection.
            stmt = (
                _review_user_select_stmt()
                .where(User.id == validated.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            user = (await db.execute(stmt)).unique().scalar_one()
            # Validate owner scope again against the freshly locked owner id.
            from app.operation.permissions import get_scope_admin_id
            owner_id = get_scope_admin_id(admin, "users", "update")
            if owner_id is not None and user.admin_id != owner_id:
                raise HTTPException(404, "User not found")
            yield NativeHandle(self, db, user, admin)

    async def sync(self, user_id, credentials):
        # This calls the RUNNING panel, whose NodeManager has live node clients.
        # Importing sync_user in this external process would silently do nothing.
        connector = aiohttp.UnixConnector(path=self.socket_path)
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=15)) as client:
            async with client.put(
                f"http://panel/api/user/by-id/{int(user_id)}/disabled",
                json={"disabled": False}, headers=credentials,
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def notify(self, user, credentials):
        # Native NATS is disabled and external notification queues would be
        # process-local with no consumer here. Record only the durable adapter
        # audit; the existing application bot owns customer/report messages.
        return {"event_delivery": "journal_only", "node_sync": "native_api_confirmed"}

    async def authorize_reconciler(self, credentials):
        async with GetDB() as db:
            admin = await self.authenticate(db, credentials)
            if not is_scope_all(admin, "users", "update"):
                raise HTTPException(403, "Reserve reconciliation requires users:update scope all")

    async def due_reserves(self, credentials, limit):
        # Historical paid/refunded eligibility is owned by the bot accounting
        # system, not native panel User. Automatic historical repair is disabled
        # unless the operator provides a verified, explicit paid-user allowlist.
        if not self.allowlist_path:
            return []
        allowlist = json.loads(Path(self.allowlist_path).read_text())
        if not isinstance(allowlist, dict) or utc(allowlist.get("valid_until")) is None:
            raise ActivationError("Due-reserve allowlist must have a validity deadline.", 503)
        if utc(allowlist["valid_until"]) <= datetime.now(UTC):
            return []
        names = allowlist.get("paid_unrefunded_usernames", [])
        if not names:
            return []
        await self.authorize_reconciler(credentials)
        async with GetDB() as db:
            stmt = (
                select(User.username)
                .where(User.status == UserStatus.on_hold)
                .where(User.on_hold_timeout.is_not(None))
                .where(User.on_hold_timeout <= datetime.now(UTC))
                .where(User.on_hold_expire_duration > 0)
                .where(User.on_hold_expire_duration <= self.max_duration)
                .where(User.username.in_(names))
                .order_by(User.on_hold_timeout, User.id).limit(limit)
            )
            return list((await db.execute(stmt)).scalars().all())


_service_token = None
_service_token_deadline = 0


async def service_credentials():
    global _service_token, _service_token_deadline
    authorization = os.environ.get("RESERVE_SERVICE_AUTHORIZATION")
    api_key = os.environ.get("RESERVE_SERVICE_API_KEY")
    if authorization:
        return {"authorization": authorization}
    if api_key:
        return {"x-api-key": api_key}
    username = os.environ.get("RESERVE_SERVICE_USERNAME")
    password = os.environ.get("RESERVE_SERVICE_PASSWORD")
    if not username or not password:
        # Reuse the inherited native env-admin configuration without copying
        # credentials. This branch only retries journaled manual requests unless
        # a separate verified historical allowlist is explicitly configured.
        from config import auth_settings
        username, password = auth_settings.sudo_username, auth_settings.sudo_password
    if not username or not password:
        raise ActivationError("Reconciler credentials are not configured.", 503)
    if _service_token and time.monotonic() < _service_token_deadline:
        return {"authorization": _service_token}
    connector = aiohttp.UnixConnector(path=os.environ.get("RESERVE_PANEL_SOCKET", "/var/lib/pasarguard/pasarguard.socket"))
    async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=10)) as client:
        async with client.post("http://panel/api/admin/token", data={"username": username, "password": password}) as response:
            response.raise_for_status()
            payload = await response.json()
            _service_token = f"Bearer {payload['access_token']}"
            _service_token_deadline = time.monotonic() + 240
            return {"authorization": _service_token}
