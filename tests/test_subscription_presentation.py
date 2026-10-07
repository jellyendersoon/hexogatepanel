"""Subscription rendering: stable per-user address pick with SNI pairing, IPv6 link bracketing,
xray allowInsecure removal and the group presentation hook in process_inbounds_and_tags."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlparse

import pytest

from app.db.models import UserStatus
from app.models.proxy import ProxyTable
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription import presentation_policy
from app.subscription.base import bracket_ipv6, format_host_port
from app.subscription.links import StandardLinks
from app.subscription.share import _hexogate_stable_pick, _stable_pick_key, process_host, process_inbounds_and_tags
from app.subscription.wireguard import WireGuardConfiguration
from app.subscription.xray import XrayConfiguration
from config import subscription_env_settings

USER_ID = "11111111-1111-1111-1111-111111111111"
IPV6 = "2001:db8::1"
WG_PRIVATE = "cGrA7ZMnpR9D8KxkkcIuZb4PZ0a5t7kIG5tsWLeMsVk="
WG_PUBLIC = "aF6tjhYOtK/9uhx3VPx3Jk3gsHcdfjAwE8dW9PrK5Vg="


def _fmt(username: str = "u1") -> defaultdict:
    return defaultdict(lambda: "<missing>", {"USERNAME": username})


def _host(
    remark: str = "node",
    tag: str = "in1",
    protocol: str = "vless",
    address: list[str] | str = "edge.example.com",
    port: list[int] | int = 443,
    sni: list[str] | str = "cert.example.com",
    req_hosts: list[str] | str | None = None,
    short_ids: list[str] | None = None,
    **extra,
) -> SubscriptionInboundData:
    tls_kwargs = {"tls": "tls", "sni": sni}
    if short_ids:
        tls_kwargs.update({"tls": "reality", "reality_short_ids": short_ids, "reality_short_id": short_ids[0]})
    return SubscriptionInboundData(
        remark=remark,
        inbound_tag=tag,
        protocol=protocol,
        address=address,
        port=port,
        network="tcp",
        tls_config=TLSConfig(**tls_kwargs),
        transport_config=TCPTransportConfig(host=req_hosts if req_hosts is not None else []),
        priority=0,
        **extra,
    )


def _production_pick(values, user_id, key):
    """Reference implementation of the production digest; the fork must never drift from it."""
    vals = sorted(values)
    return vals[int(hashlib.sha256(f"{user_id}|{key}".encode()).hexdigest(), 16) % len(vals)]


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    presentation_policy.clear_presentation_policy_cache()
    yield
    presentation_policy.clear_presentation_policy_cache()


# ---------------------------------------------------------------- stable picks


def test_stable_pick_edge_cases():
    assert _hexogate_stable_pick(["only"], 1, "k") == "only"
    assert _hexogate_stable_pick([8443], 1, "k") == 8443
    assert _hexogate_stable_pick("plain", 1, "k") == "plain"
    assert _hexogate_stable_pick([], 1, "k") == ""
    # No user id: stock random choice.
    assert _hexogate_stable_pick(["a", "b"], None, "k") in {"a", "b"}


def test_stable_pick_matches_the_production_digest_and_spreads():
    options = [f"opt{i}" for i in range(6)]
    for user_id in range(50):
        expected = _production_pick(options, user_id, "|remark")
        for _ in range(3):
            assert _hexogate_stable_pick(options, user_id, "|remark") == expected
    # Input order never matters, only the sorted values.
    shuffled = list(options)
    random.Random(1).shuffle(shuffled)
    assert _hexogate_stable_pick(shuffled, 42, "|remark") == _hexogate_stable_pick(options, 42, "|remark")

    picks = {_hexogate_stable_pick(options, user_id, "|remark") for user_id in range(200)}
    assert len(picks) == len(options)
    per_row = {_hexogate_stable_pick(options, 42, f"|remark{i}") for i in range(100)}
    assert len(per_row) > 1


def test_stable_pick_key_is_the_production_shape():
    # SubscriptionInboundData has no `tag`, so production digests "<user>|" + "|" + remark.
    assert _stable_pick_key(_host(remark="🇩🇪 Germany ▸ Reality", tag="in-de")) == "|🇩🇪 Germany ▸ Reality"


async def _render(inbound: SubscriptionInboundData, user_id: int | None, fmt=None):
    proxies = {"vless": {"id": USER_ID}}
    if user_id is not None:
        proxies["_user_id"] = user_id
    result = await process_host(inbound, fmt or _fmt(), ["in1"], proxies)
    assert result is not None
    copy, _ = result
    return copy


async def test_process_host_address_is_the_production_pick_and_stable():
    addresses = [f"a{i}.example.com" for i in range(4)]
    inbound = _host(address=addresses, remark="{USERNAME} ▸ Reality")
    for user_id in range(40):
        expected = _production_pick(addresses, user_id, "|{USERNAME} ▸ Reality")
        for _ in range(3):
            assert (await _render(inbound, user_id)).address == expected
    assert len({(await _render(inbound, user_id)).address for user_id in range(120)}) == len(addresses)


async def test_process_host_pairs_sni_with_the_address_when_listed():
    names = [f"n{i}.example.com" for i in range(4)]
    inbound = _host(address=names, sni=list(reversed(names)))
    for user_id in range(40):
        copy = await _render(inbound, user_id)
        assert copy.tls_config.sni == copy.address

    # Address not listed as an SNI: the SNI is an independent stable pick.
    inbound = _host(address=names, sni=[f"s{i}.example.com" for i in range(4)])
    first = await _render(inbound, 7)
    assert first.tls_config.sni.startswith("s")
    for _ in range(5):
        assert (await _render(inbound, 7)).tls_config.sni == first.tls_config.sni


async def test_extra_stable_picks_never_change_the_address():
    addresses = [f"a{i}.example.com" for i in range(4)]
    plain = _host(address=addresses)
    rich = _host(
        address=addresses,
        port=[443, 8443, 2053, 2083],
        sni=[f"s{i}.example.com" for i in range(4)],
        req_hosts=[f"h{i}.example.com" for i in range(4)],
        short_ids=["0123", "4567", "89ab", "cdef"],
    )
    seen = set()
    for user_id in range(120):
        plain_copy = await _render(plain, user_id)
        rich_copy = await _render(rich, user_id)
        assert rich_copy.address == plain_copy.address
        again = await _render(rich, user_id)
        assert (again.port, again.tls_config.reality_short_id, again.transport_config.host) == (
            rich_copy.port,
            rich_copy.tls_config.reality_short_id,
            rich_copy.transport_config.host,
        )
        seen.add((rich_copy.address, rich_copy.port, rich_copy.tls_config.reality_short_id))
    assert len({item[1] for item in seen}) == 4
    assert len({item[2] for item in seen}) == 4
    assert len(seen) > 4  # independent slots, not one diagonal


async def test_process_host_without_user_id_falls_back_to_random():
    addresses = [f"a{i}.example.com" for i in range(4)]
    inbound = _host(address=addresses)
    picks = {(await _render(inbound, None)).address for _ in range(60)}
    assert picks <= set(addresses)
    assert len(picks) > 1


async def test_process_host_keeps_wildcard_salt_replacement():
    inbound = _host(address=["*.cdn.example.com"], sni=["*.cdn.example.com"])
    copy = await _render(inbound, 1)
    assert "*" not in copy.address
    assert copy.address.endswith(".cdn.example.com")
    assert copy.tls_config.sni == copy.address


# ---------------------------------------------------------------- IPv6 brackets


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (IPV6, f"[{IPV6}]"),
        (f"[{IPV6}]", f"[{IPV6}]"),
        ("::1", "[::1]"),
        ("1.2.3.4", "1.2.3.4"),
        ("edge.example.com", "edge.example.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_bracket_ipv6(address, expected):
    assert bracket_ipv6(address) == expected


def test_format_host_port():
    assert format_host_port(IPV6, 443) == f"[{IPV6}]:443"
    assert format_host_port("edge.example.com", 443) == "edge.example.com:443"


def _links_for(protocol: str, settings: dict, **host_kwargs) -> str:
    links = StandardLinks()
    links.add("remark", IPV6, _host(protocol=protocol, address=IPV6, **host_kwargs), settings)
    assert len(links.links) == 1
    return links.links[0]


@pytest.mark.parametrize(
    ("protocol", "settings", "scheme"),
    [
        ("vless", {"id": USER_ID}, "vless"),
        ("trojan", {"password": "secret"}, "trojan"),
        ("shadowsocks", {"method": "chacha20-ietf-poly1305", "password": "secret"}, "ss"),
        ("hysteria", {"auth": "auth-token"}, "hysteria2"),
    ],
)
def test_links_bracket_ipv6_address(protocol, settings, scheme):
    link = _links_for(protocol, settings)
    assert link.startswith(f"{scheme}://")
    assert f"@[{IPV6}]:443" in link
    parsed = urlparse(link)
    assert parsed.hostname == IPV6
    assert parsed.port == 443


def test_links_leave_hostnames_and_ipv4_alone():
    for address in ("edge.example.com", "203.0.113.9"):
        links = StandardLinks()
        links.add("remark", address, _host(address=address), {"id": USER_ID})
        assert f"@{address}:443?" in links.links[0]


def test_vmess_add_field_is_not_bracketed():
    links = StandardLinks()
    links.add("remark", IPV6, _host(protocol="vmess", address=IPV6), {"id": USER_ID})
    payload = json.loads(base64.b64decode(links.links[0].removeprefix("vmess://")))
    assert payload["add"] == IPV6


def _wireguard_host() -> SubscriptionInboundData:
    return _host(
        protocol="wireguard",
        address=IPV6,
        port=51820,
        wireguard_public_key=WG_PUBLIC,
        wireguard_allowed_ips=["0.0.0.0/0", "::/0"],
    )


def test_wireguard_link_and_conf_bracket_ipv6_endpoint():
    settings = {"private_key": WG_PRIVATE, "peer_ips": ["10.0.0.2/32"]}

    links = StandardLinks()
    links.add("wg", IPV6, _wireguard_host(), settings)
    assert f"@[{IPV6}]:51820/" in links.links[0]

    conf = WireGuardConfiguration()
    conf.add("wg", IPV6, _wireguard_host(), settings)
    rendered = "\n".join(text for _, text in conf.configs)
    assert f"Endpoint = [{IPV6}]:51820" in rendered

    xray = XrayConfiguration(xray_template_content='{"outbounds": []}')
    xray.add("wg", IPV6, _wireguard_host(), settings)
    endpoint = xray.config[0]["outbounds"][0]["settings"]["peers"][0]["endpoint"]
    assert endpoint == f"[{IPV6}]:51820"


# ---------------------------------------------------------------- xray allowInsecure


@pytest.mark.parametrize("allowinsecure", [True, False])
def test_xray_tls_settings_never_emit_allow_insecure(allowinsecure):
    inbound = _host(address="edge.example.com")
    inbound = inbound.model_copy(
        update={"tls_config": inbound.tls_config.model_copy(update={"allowinsecure": allowinsecure})}
    )
    xray = XrayConfiguration(xray_template_content='{"outbounds": []}')
    xray.add("remark", "edge.example.com", inbound, {"id": USER_ID})
    tls_settings = xray.config[0]["outbounds"][0]["streamSettings"]["tlsSettings"]
    assert tls_settings["serverName"] == "cert.example.com"
    assert "allowInsecure" not in tls_settings
    assert "allowInsecure" not in xray.render()


def test_links_still_emit_allow_insecure():
    inbound = _host(address="edge.example.com")
    inbound = inbound.model_copy(update={"tls_config": inbound.tls_config.model_copy(update={"allowinsecure": True})})
    links = StandardLinks()
    links.add("remark", "edge.example.com", inbound, {"id": USER_ID})
    assert parse_qs(urlparse(links.links[0]).query)["allowInsecure"] == ["1"]


# ---------------------------------------------------------------- presentation hook


def _user(user_id: int, group_ids: list[int], inbounds: list[str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        status=UserStatus.active,
        inbounds=inbounds or ["in-a", "in-b", "in-c", "in-d"],
        proxy_settings=ProxyTable(),
        group_ids=group_ids,
        data_limit=None,
        expire=None,
    )


HOSTS = {
    10: _host(remark="👤 Account", tag="in-a"),
    11: _host(remark="🇳🇱 Netherlands ▸ Reality", tag="in-b"),
    12: _host(remark="🇩🇪 Germany ▸ Fastly", tag="in-c"),
    13: _host(remark="🇩🇪 Germany ▸ Reality", tag="in-b"),
    14: _host(remark="🇹🇷 Turkey ▸ VIP 👑", tag="in-d"),
    15: _host(remark="🇩🇪 Germany ▸ HTTP TLS", tag="in-c", status=["on_hold"]),
}
POLICY = {
    "version": 1,
    "host_group_visibility": [{"any_group_ids": [7, 8], "host_ids": [14]}],
    "groups": {
        "8": {
            "non_target_group_inbound_tags": {"1": ["in-b", "in-c"]},
            "scopes": [
                {
                    "name": "germany",
                    "match": {"remark_regex": "🇩🇪"},
                    "selected": [{"host_id": 13, "remark": "🇩🇪 Germany ▸ Reality ⚡"}],
                }
            ],
        }
    },
}


async def _remarks_for(user, randomize_order=False) -> list[str]:
    conf = StandardLinks()
    rendered = await process_inbounds_and_tags(user, _fmt(), conf, {}, randomize_order=randomize_order)
    return [unquote(urlparse(line).fragment) for line in rendered.splitlines() if line]


@pytest.fixture
def hosts_and_policy(tmp_path, monkeypatch):
    policy_file = tmp_path / "group-presentation-policy.json"
    policy_file.write_text(json.dumps(POLICY, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", str(policy_file))
    monkeypatch.setattr("app.subscription.share.host_manager.get_hosts", AsyncMock(return_value=HOSTS))
    presentation_policy.clear_presentation_policy_cache()
    return policy_file


async def test_process_inbounds_applies_policy_then_group_subset_ordering(hosts_and_policy):
    # Paid group 1: stock country order, Turkey row hidden by the visibility rule, status filter applied.
    assert await _remarks_for(_user(1, [1])) == [
        "👤 Account",
        "🇩🇪 Germany ▸ Fastly",
        "🇩🇪 Germany ▸ Reality",
        "🇳🇱 Netherlands ▸ Reality",
    ]
    # Economy only: method first (VIP, Reality, Fastly), country inside the method.
    assert await _remarks_for(_user(1, [7])) == [
        "👤 Account",
        "🇹🇷 Turkey ▸ VIP 👑",
        "🇩🇪 Germany ▸ Reality",
        "🇳🇱 Netherlands ▸ Reality",
        "🇩🇪 Germany ▸ Fastly",
    ]
    # VIP only: scope projection (selected row renamed, unselected German rows dropped), then method order.
    assert await _remarks_for(_user(1, [8])) == [
        "👤 Account",
        "🇹🇷 Turkey ▸ VIP 👑",
        "🇩🇪 Germany ▸ Reality ⚡",
        "🇳🇱 Netherlands ▸ Reality",
    ]
    # Mixed paid + VIP: the mapped group-1 tags keep the unselected German row behind the selected
    # block, the Turkey row stays visible (group 8) and the order is the stock country order.
    assert await _remarks_for(_user(1, [1, 8])) == [
        "👤 Account",
        "🇩🇪 Germany ▸ Reality ⚡",
        "🇩🇪 Germany ▸ Fastly",
        "🇳🇱 Netherlands ▸ Reality",
        "🇹🇷 Turkey ▸ VIP 👑",
    ]
    # No groups: nothing hidden except by visibility, stock country order.
    assert await _remarks_for(_user(1, [])) == [
        "👤 Account",
        "🇩🇪 Germany ▸ Fastly",
        "🇩🇪 Germany ▸ Reality",
        "🇳🇱 Netherlands ▸ Reality",
    ]


async def test_randomize_order_survives_only_inside_a_country(hosts_and_policy):
    seen = set()
    for _ in range(30):
        remarks = await _remarks_for(_user(1, [1]), randomize_order=True)
        assert remarks[0] == "👤 Account"
        assert remarks[-1] == "🇳🇱 Netherlands ▸ Reality"
        assert set(remarks[1:3]) == {"🇩🇪 Germany ▸ Fastly", "🇩🇪 Germany ▸ Reality"}
        seen.add(tuple(remarks))
    assert len(seen) == 2


async def test_process_inbounds_without_policy_file_keeps_stock_rendering(hosts_and_policy):
    hosts_and_policy.unlink()
    presentation_policy.clear_presentation_policy_cache()
    assert await _remarks_for(_user(1, [8])) == [
        "👤 Account",
        "🇹🇷 Turkey ▸ VIP 👑",
        "🇩🇪 Germany ▸ Reality",
        "🇳🇱 Netherlands ▸ Reality",
        "🇩🇪 Germany ▸ Fastly",
    ]
    assert await _remarks_for(_user(1, [1])) == [
        "👤 Account",
        "🇩🇪 Germany ▸ Fastly",
        "🇩🇪 Germany ▸ Reality",
        "🇳🇱 Netherlands ▸ Reality",
        "🇹🇷 Turkey ▸ VIP 👑",
    ]


async def test_policy_remark_override_feeds_the_stable_address_digest(hosts_and_policy, monkeypatch):
    addresses = [f"a{i}.example.com" for i in range(4)]
    hosts = {13: _host(remark="🇩🇪 Germany ▸ Reality", tag="in-b", address=addresses)}
    monkeypatch.setattr("app.subscription.share.host_manager.get_hosts", AsyncMock(return_value=hosts))

    async def address_for(user):
        conf = StandardLinks()
        rendered = await process_inbounds_and_tags(user, _fmt(), conf, {})
        return urlparse(rendered.strip()).hostname

    for user_id in range(30):
        # The policy renames the row for VIP users; the digest uses the rendered remark, as in production.
        assert await address_for(_user(user_id, [8])) == _production_pick(
            addresses, user_id, "|🇩🇪 Germany ▸ Reality ⚡"
        )
        assert await address_for(_user(user_id, [1])) == _production_pick(addresses, user_id, "|🇩🇪 Germany ▸ Reality")
