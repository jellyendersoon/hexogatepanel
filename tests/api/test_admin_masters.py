from fastapi import status

from tests.api import client
from tests.api.helpers import auth_headers, create_admin, delete_admin, strong_password, unique_name


def _login(username: str, password: str) -> str:
    response = client.post("/api/admin/token", data={"username": username, "password": password})
    assert response.status_code == status.HTTP_200_OK
    return response.json()["access_token"]


def _create_role(access_token: str, name: str, permissions: dict, access: dict | None = None) -> dict:
    response = client.post(
        "/api/admin-role",
        headers=auth_headers(access_token),
        json={
            "name": unique_name(name),
            "permissions": permissions,
            "limits": {},
            "features": {},
            "access": access or {},
        },
    )
    assert response.status_code == status.HTTP_201_CREATED, response.text
    return response.json()


def _master_role(access_token: str, *, can_create: bool = False) -> dict:
    admins = {"read": True, "read_simple": True, "update": True, "reset_usage": True}
    if can_create:
        admins["create"] = True
    return _create_role(
        access_token,
        "master",
        {"admins": admins, "users": {"read": {"scope": 1}, "create": True}},
        {"admins_scope": "own"},
    )


def _seller_role(access_token: str) -> dict:
    return _create_role(access_token, "seller", {"users": {"read": {"scope": 1}, "create": True}})


def _set_parent(access_token: str, username: str, parent_id: int) -> dict:
    response = client.put(f"/api/admin/{username}", headers=auth_headers(access_token), json={"parent_id": parent_id})
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def _admins(token: str, **params) -> dict:
    response = client.get("/api/admins", headers=auth_headers(token), params=params)
    assert response.status_code == status.HTTP_200_OK, response.text
    return response.json()


def test_owner_filters_admins_by_master_and_lists_masters(access_token):
    seller_role = _seller_role(access_token)
    master = create_admin(access_token, role_id=_master_role(access_token)["id"])
    mine = create_admin(access_token, role_id=seller_role["id"])
    other = create_admin(access_token, role_id=seller_role["id"])
    try:
        # The test owner is an environment sudoer without an admin row, so its new admins have no
        # master; a database admin's new admins belong to it (covered by the master-create test).
        assert mine["parent_id"] is None and other["parent_id"] is None
        assert _set_parent(access_token, mine["username"], master["id"])["parent_id"] == master["id"]

        # A master's tab holds the master account itself and its admins.
        under_master = _admins(access_token, parent_id=master["id"])
        assert {a["username"] for a in under_master["admins"]} == {master["username"], mine["username"]}
        assert under_master["total"] == 2 and under_master["active"] == 2

        without_master = {a["username"] for a in _admins(access_token, parent_id=0)["admins"]}
        assert other["username"] in without_master and mine["username"] not in without_master
        assert master["username"] not in without_master

        masters = client.get("/api/admins/masters", headers=auth_headers(access_token)).json()["masters"]
        by_id = {m["id"]: m for m in masters}
        assert by_id[master["id"]]["admins"] == 2 and by_id[master["id"]]["username"] == master["username"]
        assert by_id[0]["admins"] >= 1 and masters[-1]["id"] == 0
        # Every admin is in exactly one tab.
        assert sum(m["admins"] for m in masters) == _admins(access_token)["total"]

        # Clearing the master (0) puts the admin in the "no master" bucket.
        assert _set_parent(access_token, mine["username"], 0)["parent_id"] is None
        assert mine["username"] in {a["username"] for a in _admins(access_token, parent_id=0)["admins"]}
    finally:
        for admin in (mine, other, master):
            delete_admin(access_token, admin["username"])


def test_master_reseller_sees_and_reaches_only_its_own_admins(access_token):
    seller_role = _seller_role(access_token)
    master = create_admin(access_token, role_id=_master_role(access_token)["id"])
    mine = create_admin(access_token, role_id=seller_role["id"])
    other = create_admin(access_token, role_id=seller_role["id"])
    try:
        _set_parent(access_token, mine["username"], master["id"])
        token = _login(master["username"], master["password"])

        listed = _admins(token)
        assert [a["username"] for a in listed["admins"]] == [mine["username"]] and listed["total"] == 1
        # A filter cannot widen its view.
        assert [a["username"] for a in _admins(token, parent_id=0)["admins"]] == [mine["username"]]
        simple = client.get("/api/admins/simple", headers=auth_headers(token), params={"all": True}).json()
        assert [a["username"] for a in simple["admins"]] == [mine["username"]]
        assert client.get("/api/admins/masters", headers=auth_headers(token)).json() == {"masters": [], "total": 0}

        # Its own admin: contact/status fields are fine.
        ok = client.put(f"/api/admin/{mine['username']}", headers=auth_headers(token), json={"note": "hello"})
        assert ok.status_code == status.HTTP_200_OK and ok.json()["note"] == "hello"
        disabled = client.put(
            f"/api/admin/{mine['username']}", headers=auth_headers(token), json={"status": "disabled"}
        )
        assert disabled.status_code == status.HTTP_200_OK and disabled.json()["status"] == "disabled"
        client.put(f"/api/admin/{mine['username']}", headers=auth_headers(token), json={"status": "active"})

        # Someone else's admin does not exist for it, on any route.
        for method, url, body in (
            ("put", f"/api/admin/{other['username']}", {"note": "x"}),
            ("put", f"/api/admin/by-id/{other['id']}", {"note": "x"}),
            ("post", f"/api/admin/{other['username']}/users/disable", None),
            ("post", f"/api/admin/{other['username']}/reset", None),
            ("get", f"/api/admin/by-id/{other['id']}/usage", None),
        ):
            response = getattr(client, method)(url, headers=auth_headers(token), **({"json": body} if body else {}))
            assert response.status_code in (status.HTTP_404_NOT_FOUND, status.HTTP_403_FORBIDDEN), (
                url,
                response.status_code,
            )
        assert _admins(access_token, usernames=[other["username"]])["admins"][0]["note"] != "x"
    finally:
        for admin in (mine, other, master):
            delete_admin(access_token, admin["username"])


def test_master_reseller_cannot_change_limits_role_or_master(access_token):
    seller_role = _seller_role(access_token)
    master_role = _master_role(access_token)
    master = create_admin(access_token, role_id=master_role["id"])
    mine = create_admin(access_token, role_id=seller_role["id"])
    try:
        _set_parent(access_token, mine["username"], master["id"])
        client.put(
            f"/api/admin/{mine['username']}",
            headers=auth_headers(access_token),
            json={"permission_overrides": {"max_users": 10}, "data_limit": 1000},
        )
        token = _login(master["username"], master["password"])
        url = f"/api/admin/{mine['username']}"
        for body in (
            {"permission_overrides": {"max_users": 500}},
            {"data_limit": 999999},
            {"data_limit": 0},
            {"role_id": master_role["id"]},
            {"parent_id": 0},
            {"parent_id": master["id"]},
        ):
            response = client.put(url, headers=auth_headers(token), json=body)
            assert response.status_code == status.HTTP_403_FORBIDDEN, (body, response.status_code, response.text)
        # Re-sending the unchanged values (what the dashboard form does) is not a change.
        same = client.put(
            url,
            headers=auth_headers(token),
            json={
                "permission_overrides": {"max_users": 10},
                "data_limit": 1000,
                "role_id": seller_role["id"],
                "note": "n",
            },
        )
        assert same.status_code == status.HTTP_200_OK, same.text
        after = _admins(access_token, usernames=[mine["username"]])["admins"][0]
        assert after["permission_overrides"]["max_users"] == 10 and after["data_limit"] == 1000
        assert after["parent_id"] == master["id"] and after["role"]["id"] == seller_role["id"]
    finally:
        for admin in (mine, master):
            delete_admin(access_token, admin["username"])


def test_master_reseller_created_admins_belong_to_it_without_limits_of_its_choosing(access_token):
    seller_role = _seller_role(access_token)
    master = create_admin(access_token, role_id=_master_role(access_token, can_create=True)["id"])
    token = _login(master["username"], master["password"])
    created = None
    try:
        body = {"username": unique_name("sub"), "password": strong_password("SubADMIN"), "role_id": seller_role["id"]}
        denied = client.post(
            "/api/admin", headers=auth_headers(token), json={**body, "permission_overrides": {"max_users": 5}}
        )
        assert denied.status_code == status.HTTP_403_FORBIDDEN, denied.text
        response = client.post("/api/admin", headers=auth_headers(token), json={**body, "parent_id": 0})
        assert response.status_code == status.HTTP_201_CREATED, response.text
        created = response.json()
        assert created["parent_id"] == master["id"]
    finally:
        if created:
            delete_admin(access_token, created["username"])
        delete_admin(access_token, master["username"])


def test_master_assignment_is_validated(access_token):
    seller_role = _seller_role(access_token)
    master = create_admin(access_token, role_id=_master_role(access_token)["id"])
    first = create_admin(access_token, role_id=seller_role["id"])
    second = create_admin(access_token, role_id=seller_role["id"])
    try:
        url = f"/api/admin/{first['username']}"
        headers = auth_headers(access_token)
        assert (
            client.put(url, headers=headers, json={"parent_id": first["id"]}).status_code == status.HTTP_400_BAD_REQUEST
        )
        assert client.put(url, headers=headers, json={"parent_id": 99999999}).status_code == status.HTTP_404_NOT_FOUND
        _set_parent(access_token, second["username"], master["id"])
        # No chains: an admin that has a master cannot be a master.
        assert (
            client.put(url, headers=headers, json={"parent_id": second["id"]}).status_code
            == status.HTTP_400_BAD_REQUEST
        )
    finally:
        for admin in (first, second, master):
            delete_admin(access_token, admin["username"])
