import json

from app.utils import fleet_live
from app.utils.fleet_live import FleetLiveStore


def _sample(rx=1_000_000, tx=2_000_000, tcp=100, udp=10, **extra):
    return {"rx_bps": rx, "tx_bps": tx, "tcp": tcp, "udp": udp, "cpu": 40, "steal": 5, "cores": 4,
            "mem_used": 1, "mem_total": 2, "load1": 1.5, **extra}  # fmt: skip


def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_live, "SNAPSHOT_PATH", tmp_path / "fleet-live.json")
    return FleetLiveStore()


def test_ingest_keeps_latest_sample_and_ports(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    store.ingest("1.2.3.4", _sample(ports={"443": 7, "bad": "x", "8080": "3"}), now=1000.0)
    store.ingest("1.2.3.4", _sample(rx=5, tcp=-4), now=1005.0)
    state = store.servers["1.2.3.4"]
    assert state.last["rx_bps"] == 5
    assert state.last["tcp"] == 0  # negative and non-numeric values are clamped, never stored
    assert state.ports == {443: 7, 8080: 3}  # a sample without ports keeps the last port table
    assert state.cores == 4
    assert store.status(state, 1010.0) == "live"
    assert store.status(state, 1005.0 + fleet_live.STALE_SECONDS + 1) == "stale"
    assert store.status(state, 1005.0 + fleet_live.OFFLINE_SECONDS + 1) == "offline"


def test_minute_rollup_and_fleet_history(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    base = 60_000.0
    for second in range(0, 180, 5):  # three full minutes for two servers
        store.ingest("a", _sample(rx=100, tx=300, tcp=10, udp=1), now=base + second)
        store.ingest("b", _sample(rx=50, tx=50, tcp=5, udp=2), now=base + second)
    store.ingest("a", _sample(), now=base + 185)
    store.ingest("b", _sample(), now=base + 185)
    one = store.history("a", 60, now=base + 190)
    assert [point[1:] for point in one] == [[100, 300, 10, 1]] * 3
    fleet = store.history(None, 60, now=base + 190)
    assert [point[1:] for point in fleet] == [[150, 350, 15, 3]] * 3
    assert store.history("missing", 60, now=base + 190) == []


def test_raw_window_and_sparkline(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    start = 100_000.0
    store.ingest("a", _sample(rx=1, tx=1), now=start)
    store.ingest("a", _sample(rx=10, tx=30), now=start + fleet_live.RAW_SECONDS + 10)
    state = store.servers["a"]
    assert len(state.raw) == 1  # the old sample fell out of the 30-minute window
    spark = state.spark(start + fleet_live.RAW_SECONDS + 10, points=60)
    assert len(spark) == 60 and spark[-1] == 40 and spark[0] == -1


def test_snapshot_survives_restart(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    import time as real_time

    now = real_time.time()
    base = (int(now // 60) - 5) * 60.0
    for second in range(0, 125, 5):
        store.ingest("a", _sample(rx=8, tx=8, tcp=2, udp=0), now=base + second)
    store.save(base + 125)
    assert json.loads((tmp_path / "fleet-live.json").read_text())["minutes"]["a"]
    fresh = FleetLiveStore()
    assert [point[1:] for point in fresh.history("a", 60)] == [[8, 8, 2, 0]] * 2


def test_server_limit(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    for index in range(250):
        store.ingest(f"s{index}", _sample(), now=1000.0)
    assert len(store.servers) == 200


def test_live_users_ranks_by_speed_and_keeps_ten_minutes():
    users = fleet_live.LiveUsers()
    users.record({1: [{"uid": 99, "value": 9_000_000_000}]}, now=970.0)  # first report after a start: ignored
    assert users.top(now=971.0)["users"] == []
    users.record({1: [{"uid": 10, "value": 3_000_000}, {"uid": 11, "value": 30_000_000}]}, now=1000.0)
    users.record(
        {1: [{"uid": 10, "value": 6_000_000}], 2: [{"uid": 10, "value": 1_500_000}, {"uid": 12, "value": 0}]},
        now=1030.0,
    )

    now = users.top(limit=10, span="now", now=1035.0)
    assert now["active"] == 1 and now["seconds"] == 30
    assert [user["uid"] for user in now["users"]] == [10]
    assert now["users"][0]["bps"] == 7_500_000 * 8 // 30
    assert now["users"][0]["nodes"] == [(1, 6_000_000), (2, 1_500_000)]
    assert now["users"][0]["kept_bytes"] == 10_500_000
    assert len(now["users"][0]["spark"]) == 2

    ten = users.top(limit=1, span="10m", now=1035.0)
    assert [user["uid"] for user in ten["users"]] == [11] and ten["seconds"] == 60

    assert users.top(now=1030.0 + fleet_live.USER_STALE_SECONDS + 1)["users"] == []
