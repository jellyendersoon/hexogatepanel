"""Hexogate time guard (2026-10-06, owner request).

Admins listed in /var/lib/pasarguard/hexo-time-guard.json may still edit their
users (data limit, reset usage, notes ...) but may not give an existing user
more time: no later expire, no unlimited expire, no longer on-hold duration,
no new on-hold, no later on-hold timeout. Creating new users is unchanged.

Loaded by Python at startup (mounted as site-packages/sitecustomize.py) and
wraps UserOperation._prepare_modified_user once app.operation.user is
imported. That method backs PUT /api/user/*, PUT /api/user/from_template/*
and POST /api/users/bulk/apply_template.
Rollback: drop the mount from /opt/pasarguard/docker-compose.yml and recreate.
"""

import importlib.abc
import importlib.machinery
import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta

_TARGET = "app.operation.user"
_CONFIG = "/var/lib/pasarguard/hexo-time-guard.json"
_SLACK = timedelta(minutes=10)
_MSG = (
    "Adding time to existing configs is not allowed for this admin; you can still add data. "
    "افزودن زمان به کانفیگ‌های موجود برای این ادمین مجاز نیست؛ فقط حجم قابل افزایش است."
)
_log = logging.getLogger("hexo-time-guard")
_cache = {"mtime": None, "admins": frozenset()}


def _guarded_admins():
    try:
        mtime = os.stat(_CONFIG).st_mtime
    except OSError:
        return frozenset()
    if mtime != _cache["mtime"]:
        try:
            with open(_CONFIG) as fh:
                names = json.load(fh).get("admins", [])
            _cache["admins"] = frozenset(str(n).lower() for n in names)
        except Exception:
            _log.exception("hexo-time-guard: bad config, keeping previous list")
        _cache["mtime"] = mtime
    return _cache["admins"]


def _utc(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _status(value):
    return getattr(value, "value", value)


def time_violation(db_user, modified_user):
    """Return a reason string if this edit gives the user more time, else None."""
    fields = modified_user.model_fields_set
    old_status = _status(db_user.status)

    if "expire" in fields:
        new, old = _utc(modified_user.expire), _utc(db_user.expire)
        if new is None or (isinstance(modified_user.expire, (int, float)) and modified_user.expire == 0):
            if old is not None:
                return "expire made unlimited"
        elif old is None:
            # on-hold or unlimited user getting a fixed date: only allowed for on-hold users
            # whose new expire is no later than their remaining on-hold duration would give
            dur = db_user.on_hold_expire_duration
            if old_status != "on_hold" or not dur or new > datetime.now(UTC) + timedelta(seconds=dur) + _SLACK:
                return "expire set on a user without one"
        elif new > old + _SLACK:
            return f"expire moved later ({old:%Y-%m-%d %H:%M} -> {new:%Y-%m-%d %H:%M})"

    new_status = _status(modified_user.status) if modified_user.status is not None else None
    if new_status == "on_hold" and old_status != "on_hold":
        return "status changed to on_hold"

    if "on_hold_expire_duration" in fields and modified_user.on_hold_expire_duration is not None:
        old = db_user.on_hold_expire_duration or 0
        if modified_user.on_hold_expire_duration > old:
            return "on-hold duration increased"

    if "on_hold_timeout" in fields:
        new, old = _utc(modified_user.on_hold_timeout), _utc(db_user.on_hold_timeout)
        if old is not None and (new is None or new > old + _SLACK):
            return "on-hold timeout moved later"
        if old is None and new is not None and old_status != "on_hold":
            return "on-hold timeout set"

    return None


def _patch(module):
    cls = module.UserOperation
    original = cls._prepare_modified_user
    if getattr(original, "_hexo_time_guard", False):
        return

    async def _prepare_modified_user(self, db, db_user, modified_user, admin, *args, **kwargs):
        name = (getattr(admin, "username", "") or "").lower()
        if name and name in _guarded_admins() and not getattr(admin, "is_owner", False):
            reason = time_violation(db_user, modified_user)
            if reason:
                _log.warning(
                    'hexo-time-guard: blocked admin "%s" on user "%s": %s', admin.username, db_user.username, reason
                )
                await self.raise_error(message=_MSG, code=403, db=db)
        return await original(self, db, db_user, modified_user, admin, *args, **kwargs)

    _prepare_modified_user._hexo_time_guard = True
    cls._prepare_modified_user = _prepare_modified_user
    _log.warning("hexo-time-guard: active")


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != _TARGET:
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path, target)
        if spec is None or spec.loader is None:
            return spec
        loader_exec = spec.loader.exec_module

        def exec_module(module):
            loader_exec(module)
            try:
                _patch(module)
            except Exception:
                _log.exception("hexo-time-guard: FAILED to patch, guard is OFF")

        spec.loader.exec_module = exec_module
        return spec


if os.environ.get("HEXO_TIME_GUARD_OFF") != "1":
    sys.meta_path.insert(0, _Finder())
