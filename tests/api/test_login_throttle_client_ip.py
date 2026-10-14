"""The login throttle must key on the real client, not on the local proxy.

Live, the panel listens on a unix socket behind Caddy: the ASGI peer is None,
so every login used to share the key "ip:Unknown" and ten failures by anyone
locked out every admin (and the bot) for five minutes.
"""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from app.utils.request import get_client_ip
from config import server_settings
from tests.api import app


def _request(client, headers=()):
    scope = {
        "type": "http",
        "client": client,
        "headers": [(name.lower().encode(), value.encode()) for name, value in headers],
    }
    return Request(scope=scope)


@pytest.fixture
def live_trust(monkeypatch: pytest.MonkeyPatch):
    """The production default: only loopback (and the unix socket) is a trusted proxy."""
    monkeypatch.setattr(server_settings, "forwarded_allow_ips", "127.0.0.1")


def test_unix_socket_peer_uses_forwarded_for(live_trust):
    assert get_client_ip(_request(None, [("X-Forwarded-For", "203.0.113.7")])) == "203.0.113.7"


def test_unix_socket_peer_with_empty_host_uses_forwarded_for(live_trust):
    assert get_client_ip(_request(("", 0), [("X-Forwarded-For", "203.0.113.7")])) == "203.0.113.7"


def test_loopback_peer_uses_real_ip_when_no_forwarded_for(live_trust):
    request = _request(("127.0.0.1", 5000), [("X-Real-IP", "198.51.100.4")])
    assert get_client_ip(request) == "198.51.100.4"


def test_forwarded_for_wins_over_real_ip(live_trust):
    request = _request(None, [("X-Forwarded-For", "203.0.113.7"), ("X-Real-IP", "198.51.100.4")])
    assert get_client_ip(request) == "203.0.113.7"


def test_rightmost_untrusted_hop_is_used(live_trust):
    # A client cannot pick its key by sending its own X-Forwarded-For: the proxy appends
    # the real peer last, and that is the address used.
    request = _request(None, [("X-Forwarded-For", "10.9.9.9, 203.0.113.7")])
    assert get_client_ip(request) == "203.0.113.7"


def test_trusted_hops_are_skipped(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_settings, "forwarded_allow_ips", "127.0.0.1, 172.16.0.0/12")
    request = _request(None, [("X-Forwarded-For", "203.0.113.7, 172.20.0.1")])
    assert get_client_ip(request) == "203.0.113.7"


def test_ports_and_brackets_are_stripped(live_trust):
    assert get_client_ip(_request(None, [("X-Forwarded-For", "203.0.113.7:4431")])) == "203.0.113.7"
    assert get_client_ip(_request(None, [("X-Forwarded-For", "[2001:db8::5]:443")])) == "2001:db8::5"


def test_untrusted_peer_ignores_forwarded_headers(live_trust):
    request = _request(("198.51.100.20", 4000), [("X-Forwarded-For", "203.0.113.7"), ("X-Real-IP", "203.0.113.8")])
    assert get_client_ip(request) == "198.51.100.20"


def test_garbage_forwarded_values_fall_back(live_trust):
    request = _request(None, [("X-Forwarded-For", "not-an-ip"), ("X-Real-IP", "also bad")])
    assert get_client_ip(request) == "Unknown"
    assert get_client_ip(_request(("127.0.0.1", 1), [("X-Forwarded-For", "nope")])) == "127.0.0.1"


def test_configured_proxy_peer_is_trusted(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(server_settings, "forwarded_allow_ips", "10.0.0.0/8")
    assert get_client_ip(_request(("10.1.2.3", 1), [("X-Forwarded-For", "203.0.113.9")])) == "203.0.113.9"


def test_one_client_failing_does_not_lock_out_others(access_token, live_trust):
    """End to end over a unix-socket-like peer: ten failures from one client lock only that client."""

    async def unix_socket_app(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=None)
        await app(scope, receive, send)

    uds_client = TestClient(unix_socket_app)

    def login(ip: str, username: str, password: str):
        return uds_client.post(
            "/api/admin/token",
            data={"username": username, "password": password, "grant_type": "password"},
            headers={"X-Real-IP": ip},
        )

    for _ in range(10):
        assert login("203.0.113.66", "nobody-here", "wrong").status_code == 401
    assert login("203.0.113.66", "nobody-here", "wrong").status_code == 429

    # Another client (e.g. the Hexogate bot) still logs in.
    response = login("198.51.100.10", "testadmin", "testadmin")
    assert response.status_code == 200, response.text
