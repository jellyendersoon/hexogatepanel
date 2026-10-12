from fastapi import status
from pytest import MonkeyPatch

from app.routers import fleet_live as fleet_router
from app.utils import fleet_live as fleet_store
from tests.api import client
from tests.api.helpers import auth_headers


def _fresh_store(monkeypatch: MonkeyPatch, tmp_path):
    monkeypatch.setattr(fleet_store, "SNAPSHOT_PATH", tmp_path / "fleet-live.json")
    store = fleet_store.FleetLiveStore()
    monkeypatch.setattr(fleet_router, "store", store)
    monkeypatch.setattr(fleet_router, "_topology", {"at": 0.0, "servers": {}})
    return store


def _sample(server="203.0.113.9", **extra):
    return {"server": server, "rx_bps": 8_000_000, "tx_bps": 16_000_000, "tcp": 1200, "udp": 34, "cpu": 55.5,
            "steal": 12.0, "cores": 4, "mem_used": 2_000, "mem_total": 8_000, "load1": 1.25, **extra}  # fmt: skip


def test_ingest_requires_the_shared_token(monkeypatch: MonkeyPatch, tmp_path):
    store = _fresh_store(monkeypatch, tmp_path)
    monkeypatch.delenv("FLEET_LIVE_TOKEN", raising=False)
    assert client.post("/api/fleet/live/ingest", json=_sample()).status_code == status.HTTP_403_FORBIDDEN

    monkeypatch.setenv("FLEET_LIVE_TOKEN", "s3cret-token")
    wrong = client.post("/api/fleet/live/ingest", json=_sample(), headers={"X-Fleet-Token": "nope"})
    assert wrong.status_code == status.HTTP_403_FORBIDDEN
    missing_server = client.post("/api/fleet/live/ingest", json={"tcp": 1}, headers={"X-Fleet-Token": "s3cret-token"})
    assert missing_server.status_code == status.HTTP_400_BAD_REQUEST
    assert store.servers == {}

    ok = client.post("/api/fleet/live/ingest", json=_sample(), headers={"X-Fleet-Token": "s3cret-token"})
    assert ok.status_code == status.HTTP_204_NO_CONTENT
    assert store.servers["203.0.113.9"].last["tcp"] == 1200


def test_fleet_live_needs_an_admin_and_reports_servers(access_token, monkeypatch: MonkeyPatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    monkeypatch.setenv("FLEET_LIVE_TOKEN", "s3cret-token")
    assert client.get("/api/fleet/live").status_code == status.HTTP_401_UNAUTHORIZED

    async def topology():
        return {
            "203.0.113.9": {
                "nodes": [{"id": 7, "name": "🇩🇪 DE-01 ▸ Full", "status": "connected"}],
                "ports": {
                    443: [
                        {
                            "tag": "REALITY-443",
                            "protocol": "vless",
                            "network": "tcp",
                            "security": "reality",
                            "node_id": 7,
                        }
                    ]
                },
            },
            "203.0.113.10": {"nodes": [{"id": 8, "name": "Old box", "status": "disabled"}], "ports": {}},
        }

    monkeypatch.setattr(fleet_router, "_load_topology", topology)
    headers = {"X-Fleet-Token": "s3cret-token"}
    assert (
        client.post("/api/fleet/live/ingest", json=_sample(ports={"443": 900, "22": 3}), headers=headers).status_code
        == 204
    )

    response = client.get("/api/fleet/live", headers=auth_headers(access_token))
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert [server["key"] for server in body["servers"]] == ["203.0.113.9"]  # the disabled, silent server is left out
    server = body["servers"][0]
    assert server["name"] == "🇩🇪 DE-01" and server["status"] == "live"
    assert (server["rx_bps"], server["tx_bps"], server["tcp"], server["udp"]) == (8_000_000, 16_000_000, 1200, 34)
    assert server["inbounds"][0] == {"port": 443, "connections": 900, "tag": "REALITY-443", "protocol": "vless",
                                     "network": "tcp", "security": "reality", "node_id": 7}  # fmt: skip
    assert server["inbounds"][1]["tag"] == "other" and server["inbounds"][1]["connections"] == 3
    assert body["totals"] == {
        "rx_bps": 8_000_000,
        "tx_bps": 16_000_000,
        "tcp": 1200,
        "udp": 34,
        "servers": 1,
        "reporting": 1,
    }

    history = client.get("/api/fleet/live/history?minutes=30", headers=auth_headers(access_token))
    assert history.status_code == status.HTTP_200_OK and history.json()["points"] == []
    assert client.get("/api/fleet/live/history?minutes=5", headers=auth_headers(access_token)).status_code == 422


def test_fleet_live_users_lists_the_busiest_users(access_token, monkeypatch: MonkeyPatch, tmp_path):
    _fresh_store(monkeypatch, tmp_path)
    live = fleet_store.LiveUsers()
    monkeypatch.setattr(fleet_router, "live_users", live)

    async def topology():
        return {"203.0.113.9": {"nodes": [{"id": 7, "name": "🇩🇪 DE-01 ▸ Full", "status": "connected"}], "ports": {}}}

    async def users(uids):
        return {41: {"username": "alice", "status": "active", "used_traffic": 5, "data_limit": None, "admin": "boss"}}

    monkeypatch.setattr(fleet_router, "_load_topology", topology)
    monkeypatch.setattr(fleet_router, "_load_users", users)
    assert client.get("/api/fleet/live/users").status_code == status.HTTP_401_UNAUTHORIZED
    empty = client.get("/api/fleet/live/users", headers=auth_headers(access_token))
    assert empty.status_code == status.HTTP_200_OK and empty.json()["users"] == []

    live.record({7: [{"uid": 41, "value": 3_750_000}], 9: [{"uid": 42, "value": 1_000}]})
    body = client.get("/api/fleet/live/users?limit=5", headers=auth_headers(access_token)).json()
    assert body["active"] == 2 and body["span"] == "now"
    first, second = body["users"]
    assert (first["username"], first["admin"], first["bps"]) == ("alice", "boss", 1_000_000)
    assert first["servers"] == [{"name": "🇩🇪 DE-01", "bytes": 3_750_000}]
    assert second["username"] == "#42" and second["servers"][0]["name"] == "node 9"
    assert client.get("/api/fleet/live/users?span=1h", headers=auth_headers(access_token)).status_code == 422
