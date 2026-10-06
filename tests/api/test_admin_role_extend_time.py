"""Tests for the ``can_extend_time`` role feature.

Admins whose role disables it may add DATA to existing users but never TIME:
later/unlimited expire, new or longer on-hold reservations, later on-hold timeouts,
template flows that add time and bulk expire increases are rejected with a bilingual 403.
Creating new users (with expiry) and the owner are untouched.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import status

from tests.api import client
from tests.api.helpers import (
    auth_headers,
    create_admin,
    create_core,
    create_group,
    create_user,
    create_user_template,
    delete_admin,
    delete_core,
    delete_group,
    delete_user,
    delete_user_template,
    unique_name,
)

EN_FRAGMENT = "not allowed to extend time"
FA_FRAGMENT = "افزایش زمان"

USERS_FULL = {"create": True, "read": True, "update": True, "delete": True, "reset_usage": True}


def _login(username: str, password: str) -> str:
    response = client.post(
        "/api/admin/token",
        data={"username": username, "password": password, "grant_type": "password"},
    )
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_role(access_token: str, *, can_extend_time: bool | None, name_prefix: str = "no_time_role") -> dict:
    payload: dict = {
        "name": unique_name(name_prefix),
        "permissions": {"users": USERS_FULL, "templates": {"read": True, "read_simple": True}},
        "access": {"require_template": False, "allowed_template_ids": None, "allowed_group_ids": None},
    }
    if can_extend_time is not None:
        payload["features"] = {
            "can_use_reset_strategy": True,
            "can_use_next_plan": True,
            "can_extend_time": can_extend_time,
        }
    response = client.post("/api/admin-role", headers=auth_headers(access_token), json=payload)
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _delete_role(access_token: str, role_id: int) -> None:
    client.delete(f"/api/admin-role/{role_id}", headers=auth_headers(access_token))


@contextmanager
def _restricted_admin(access_token: str) -> Iterator[str]:
    """Yield a token for an admin whose role has ``can_extend_time=False``."""
    role = _create_role(access_token, can_extend_time=False)
    admin = create_admin(access_token, role_id=role["id"])
    try:
        yield _login(admin["username"], admin["password"])
    finally:
        delete_admin(access_token, admin["username"])
        _delete_role(access_token, role["id"])


def _modify(token: str, user_id: int, payload: dict):
    return client.put(f"/api/user/by-id/{user_id}", headers=auth_headers(token), json=payload)


def _assert_time_forbidden(response) -> None:
    assert response.status_code == status.HTTP_403_FORBIDDEN, response.text
    detail = response.json()["detail"]
    assert EN_FRAGMENT in detail
    assert FA_FRAGMENT in detail


def _iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _active_user(token: str, *, days: int = 10, data_limit: int = 1024 * 1024) -> dict:
    expire = datetime.now(UTC) + timedelta(days=days)
    return create_user(
        token,
        payload={
            "username": unique_name("time_user"),
            "expire": _iso(expire),
            "data_limit": data_limit,
            "status": "active",
        },
    )


def _on_hold_user(token: str, *, duration: int = 86400 * 10, timeout_days: int = 5) -> dict:
    timeout = datetime.now(UTC) + timedelta(days=timeout_days)
    return create_user(
        token,
        payload={
            "username": unique_name("hold_user"),
            "status": "on_hold",
            "on_hold_expire_duration": duration,
            "on_hold_timeout": _iso(timeout),
            "data_limit": 1024 * 1024,
        },
    )


# ---------------------------------------------------------------------------
# Role model
# ---------------------------------------------------------------------------


def test_role_feature_defaults_to_true_and_round_trips(access_token):
    """Roles created without the key default to True; an explicit False is stored and returned."""
    default_role = _create_role(access_token, can_extend_time=None, name_prefix="default_time_role")
    strict_role = _create_role(access_token, can_extend_time=False)
    try:
        assert default_role["features"]["can_extend_time"] is True

        response = client.get(f"/api/admin-role/{strict_role['id']}", headers=auth_headers(access_token))
        assert response.status_code == status.HTTP_200_OK
        assert response.json()["features"]["can_extend_time"] is False
    finally:
        _delete_role(access_token, default_role["id"])
        _delete_role(access_token, strict_role["id"])


# ---------------------------------------------------------------------------
# Plain modify
# ---------------------------------------------------------------------------


def test_restricted_admin_can_create_user_with_expiry(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=30)
        try:
            assert user["expire"] is not None
            assert user["status"] == "active"
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_set_later_expire(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10)
        try:
            later = datetime.now(UTC) + timedelta(days=20)
            _assert_time_forbidden(_modify(token, user["id"], {"expire": _iso(later)}))
            # unchanged value still works
            response = _modify(token, user["id"], {"expire": user["expire"]})
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_set_unlimited_expire(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10)
        try:
            _assert_time_forbidden(_modify(token, user["id"], {"expire": 0}))
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_can_reduce_expire_and_add_data(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10, data_limit=1024 * 1024)
        try:
            earlier = datetime.now(UTC) + timedelta(days=5)
            response = _modify(token, user["id"], {"expire": _iso(earlier)})
            assert response.status_code == status.HTTP_200_OK, response.text

            response = _modify(token, user["id"], {"data_limit": 50 * 1024 * 1024})
            assert response.status_code == status.HTTP_200_OK, response.text
            assert response.json()["data_limit"] == 50 * 1024 * 1024

            # the usual full-form payload echoing the current expire with a bigger data limit passes
            response = _modify(
                token,
                user["id"],
                {"expire": response.json()["expire"], "data_limit": 100 * 1024 * 1024, "status": "active"},
            )
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_can_reset_usage(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token)
        try:
            response = client.post(f"/api/user/by-id/{user['id']}/reset", headers=auth_headers(token))
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_add_on_hold_to_active_user(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token)
        try:
            _assert_time_forbidden(_modify(token, user["id"], {"status": "on_hold", "on_hold_expire_duration": 86400}))
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_lengthen_on_hold(access_token):
    with _restricted_admin(access_token) as token:
        user = _on_hold_user(token, duration=86400 * 10)
        try:
            _assert_time_forbidden(
                _modify(token, user["id"], {"status": "on_hold", "on_hold_expire_duration": 86400 * 20})
            )
            # shorter reservation and more data are fine
            response = _modify(
                token,
                user["id"],
                {"status": "on_hold", "on_hold_expire_duration": 86400 * 5, "data_limit": 20 * 1024 * 1024},
            )
            assert response.status_code == status.HTTP_200_OK, response.text
            assert response.json()["on_hold_expire_duration"] == 86400 * 5
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_set_later_on_hold_timeout(access_token):
    with _restricted_admin(access_token) as token:
        user = _on_hold_user(token, timeout_days=5)
        try:
            later = datetime.now(UTC) + timedelta(days=15)
            _assert_time_forbidden(_modify(token, user["id"], {"on_hold_timeout": _iso(later)}))
            # 0/None cannot clear a timeout through modify (validator + CRUD treat it as no-op)
            response = _modify(token, user["id"], {"on_hold_timeout": 0})
            assert response.status_code == status.HTTP_200_OK, response.text
            assert response.json()["on_hold_timeout"] == user["on_hold_timeout"]

            earlier = datetime.now(UTC) + timedelta(days=2)
            response = _modify(token, user["id"], {"on_hold_timeout": _iso(earlier)})
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_cannot_activate_on_hold_user_without_expire(access_token):
    with _restricted_admin(access_token) as token:
        user = _on_hold_user(token, duration=86400 * 10)
        try:
            _assert_time_forbidden(_modify(token, user["id"], {"status": "active"}))
            later = datetime.now(UTC) + timedelta(days=30)
            _assert_time_forbidden(_modify(token, user["id"], {"status": "active", "expire": _iso(later)}))
            within = datetime.now(UTC) + timedelta(days=9)
            response = _modify(token, user["id"], {"status": "active", "expire": _iso(within)})
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


# ---------------------------------------------------------------------------
# Owner / permissive role bypass
# ---------------------------------------------------------------------------


def test_owner_bypasses_time_guard(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10)
        try:
            later = datetime.now(UTC) + timedelta(days=40)
            response = _modify(access_token, user["id"], {"expire": _iso(later)})
            assert response.status_code == status.HTTP_200_OK, response.text
            response = _modify(access_token, user["id"], {"expire": 0})
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(access_token, user["username"])


def test_role_with_feature_enabled_can_extend(access_token):
    role = _create_role(access_token, can_extend_time=True, name_prefix="time_ok_role")
    admin = create_admin(access_token, role_id=role["id"])
    try:
        token = _login(admin["username"], admin["password"])
        user = _active_user(token, days=10)
        try:
            later = datetime.now(UTC) + timedelta(days=40)
            response = _modify(token, user["id"], {"expire": _iso(later)})
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])
    finally:
        delete_admin(access_token, admin["username"])
        _delete_role(access_token, role["id"])


# ---------------------------------------------------------------------------
# Bulk expire
# ---------------------------------------------------------------------------


def test_restricted_admin_bulk_expire_increase_forbidden(access_token):
    with _restricted_admin(access_token) as token:
        user = _active_user(token)
        try:
            response = client.post(
                "/api/users/bulk/expire",
                headers=auth_headers(token),
                json={"amount": 3600, "users": [user["id"]]},
            )
            _assert_time_forbidden(response)
            # dry run is blocked the same way
            response = client.post(
                "/api/users/bulk/expire",
                headers=auth_headers(token),
                json={"amount": 3600, "users": [user["id"]], "dry_run": True},
            )
            _assert_time_forbidden(response)
            # reducing is still allowed
            response = client.post(
                "/api/users/bulk/expire",
                headers=auth_headers(token),
                json={"amount": -3600, "users": [user["id"]]},
            )
            assert response.status_code == status.HTTP_200_OK, response.text
            # data limit bulk is untouched
            response = client.post(
                "/api/users/bulk/data_limit",
                headers=auth_headers(token),
                json={"amount": 1024 * 1024, "users": [user["id"]]},
            )
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


# ---------------------------------------------------------------------------
# Template flows
# ---------------------------------------------------------------------------


@pytest.fixture
def template_env(access_token):
    core = create_core(access_token)
    group = create_group(access_token, name=unique_name("time_group"))
    long_template = create_user_template(
        access_token,
        group_ids=[group["id"]],
        expire_duration=86400 * 365,
        reset_usages=False,
    )
    short_template = create_user_template(
        access_token,
        group_ids=[group["id"]],
        expire_duration=86400,
        reset_usages=False,
    )
    hold_template = create_user_template(
        access_token,
        group_ids=[group["id"]],
        expire_duration=86400 * 30,
        status_value="on_hold",
        reset_usages=False,
    )
    try:
        yield {"group": group, "long": long_template, "short": short_template, "hold": hold_template}
    finally:
        delete_user_template(access_token, long_template["id"])
        delete_user_template(access_token, short_template["id"])
        delete_user_template(access_token, hold_template["id"])
        delete_group(access_token, group["id"])
        delete_core(access_token, core["id"])


def _apply_template(token: str, user_id: int, template_id: int):
    return client.put(
        f"/api/user/from_template/by-id/{user_id}",
        headers=auth_headers(token),
        json={"user_template_id": template_id},
    )


def test_restricted_admin_modify_with_template_adding_time_forbidden(access_token, template_env):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10)
        try:
            _assert_time_forbidden(_apply_template(token, user["id"], template_env["long"]["id"]))
            _assert_time_forbidden(_apply_template(token, user["id"], template_env["hold"]["id"]))
            # a template that shortens the deadline is fine
            response = _apply_template(token, user["id"], template_env["short"]["id"])
            assert response.status_code == status.HTTP_200_OK, response.text
            # owner may apply the long template
            response = _apply_template(access_token, user["id"], template_env["long"]["id"])
            assert response.status_code == status.HTTP_200_OK, response.text
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_bulk_apply_template_adding_time_forbidden(access_token, template_env):
    with _restricted_admin(access_token) as token:
        user = _active_user(token, days=10)
        try:
            response = client.post(
                "/api/users/bulk/apply_template",
                headers=auth_headers(token),
                json={"ids": [user["id"]], "user_template_id": template_env["long"]["id"]},
            )
            _assert_time_forbidden(response)
        finally:
            delete_user(token, user["username"])


def test_restricted_admin_can_create_user_from_template(access_token, template_env):
    with _restricted_admin(access_token) as token:
        response = client.post(
            "/api/user/from_template",
            headers=auth_headers(token),
            json={"username": unique_name("tpl_user"), "user_template_id": template_env["long"]["id"]},
        )
        assert response.status_code == status.HTTP_201_CREATED, response.text
        delete_user(token, response.json()["username"])
