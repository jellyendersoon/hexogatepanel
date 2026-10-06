"""Stable per-user picks, IPv6 link bracketing, xray allowInsecure and the group presentation policy."""

from __future__ import annotations

import base64
import json
import os
import time
from collections import defaultdict
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from app.db.models import UserStatus
from app.models.proxy import ProxyTable
from app.models.subscription import SubscriptionInboundData, TCPTransportConfig, TLSConfig
from app.subscription import presentation_policy
from app.subscription.base import bracket_ipv6, format_host_port
from app.subscription.links import StandardLinks
from app.subscription.presentation_policy import (
    DEFAULT_POLICY,
    PresentationPolicy,
    category_rank,
    load_presentation_policy,
    order_hosts_for_user,
)
from app.subscription.share import process_host, process_inbounds_and_tags, stable_pick
from app.subscription.wireguard import WireGuardConfiguration
from app.subscription.xray import XrayConfiguration

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


@pytest.fixture(autouse=True)
def _clear_policy_cache():
    presentation_policy.clear_presentation_policy_cache()
    yield
    presentation_policy.clear_presentation_policy_cache()


# ---------------------------------------------------------------- stable picks


def test_stable_pick_single_option_is_identity():
    assert stable_pick(1, "in", "r", ["only"]) == "only"
    assert stable_pick(1, "in", "r", [8443]) == 8443
    assert stable_pick(1, "in", "r", "plain") == "plain"
    assert stable_pick(1, "in", "r", []) == ""


def test_stable_pick_same_inputs_same_choice_and_spread_across_users():
    options = [f"opt{i}" for i in range(6)]
    first = stable_pick(42, "in1", "remark", options)
    for _ in range(20):
        assert stable_pick(42, "in1", "remark", options) == first

    picks = {stable_pick(user_id, "in1", "remark", options) for user_id in range(200)}
    assert len(picks) == len(options)

    # Different hosts give the user independent picks as well.
    per_host = {stable_pick(42, f"in{i}", f"remark{i}", options) for i in range(100)}
    assert len(per_host) > 1


@pytest.mark.asyncio
async def test_process_host_is_deterministic_per_user_and_pairs_sni_with_address():
    addresses = [f"a{i}.example.com" for i in range(4)]
    snis = [f"s{i}.example.com" for i in range(4)]
    req_hosts = [f"h{i}.example.com" for i in range(4)]
    ports = [443, 8443, 2053, 2083]
    short_ids = ["0123", "4567", "89ab", "cdef"]
    inbound = _host(
        address=addresses, port=ports, sni=snis, req_hosts=req_hosts, short_ids=short_ids, remark="{USERNAME}"
    )

    async def render(user_id: int):
        result = await process_host(
            inbound,
            _fmt(),
            ["in1"],
            {"vless": {"id": USER_ID}, "_user_id": user_id},
        )
        assert result is not None
        copy, _ = result
        return (
            copy.address,
            copy.tls_config.sni,
            copy.transport_config.host,
            copy.port,
            copy.tls_config.reality_short_id,
        )

    first = await render(7)
    for _ in range(10):
        assert await render(7) == first

    address, sni, req_host, port, sid = first
    index = addresses.index(address)
    assert snis.index(sni) == index
    assert req_hosts.index(req_host) == index
    assert port in ports
    assert sid in short_ids

    seen = {await render(user_id) for user_id in range(120)}
    assert len({item[0] for item in seen}) == len(addresses)
    assert len({item[3] for item in seen}) == len(ports)
    assert len({item[4] for item in seen}) == len(short_ids)
    # Picks spread across the whole combination space, not a single diagonal.
    assert len(seen) > len(addresses)


@pytest.mark.asyncio
async def test_process_host_keeps_wildcard_salt_replacement():
    inbound = _host(address=["*.cdn.example.com"], sni=["*.cdn.example.com"])
    result = await process_host(inbound, _fmt(), ["in1"], {"vless": {"id": USER_ID}, "_user_id": 1})
    assert result is not None
    copy, _ = result
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


# ---------------------------------------------------------------- presentation policy


def _remarked(*remarks: str) -> list[SubscriptionInboundData]:
    return [_host(remark=remark, tag=f"tag-{index}") for index, remark in enumerate(remarks)]


def test_default_category_order():
    hosts = _remarked(
        "Plain Node",
        "Reality TCP",
        "Reality IPv6 Node",
        "Irancell CF",
        "CF IPv6 Edge",
        "CF ECH Node",
        "Fastly HTTP Node",
        "Fastly Node",
        "VIP Reality Node",
    )
    ordered = order_hosts_for_user(hosts, [], None)
    assert [host.remark for host in ordered] == [
        "VIP Reality Node",
        "Fastly Node",
        "Fastly HTTP Node",
        "CF ECH Node",
        "CF IPv6 Edge",
        "Irancell CF",
        "Reality IPv6 Node",
        "Reality TCP",
        "Plain Node",
    ]


def test_category_rank_uses_inbound_tag_and_is_case_insensitive():
    assert category_rank(_host(remark="node", tag="VLESS-REALITY")) == 7
    assert category_rank(_host(remark="fastly http", tag="x")) == 2
    assert category_rank(_host(remark="FASTLY", tag="x")) == 1
    assert category_rank(_host(remark="misc", tag="x")) == len(DEFAULT_POLICY.categories)


def test_ordering_is_stable_within_category():
    hosts = _remarked("Reality A", "Plain B", "Reality C", "Plain D", "VIP E")
    ordered = order_hosts_for_user(hosts, [1, 2], None)
    assert [host.remark for host in ordered] == ["VIP E", "Reality A", "Reality C", "Plain B", "Plain D"]
    # The input list is not mutated.
    assert [host.remark for host in hosts] == ["Reality A", "Plain B", "Reality C", "Plain D", "VIP E"]


def test_group_filtering_union_and_passthrough():
    policy = PresentationPolicy.from_dict(
        {
            "groups": {
                "7": {"allow": ["economy", "fastly"]},
                "8": {"allow": ["vip"]},
                "9": {"allow": []},
            }
        }
    )
    hosts = _remarked("VIP Reality", "Economy CF", "Fastly HTTP", "Plain")

    economy = [host.remark for host in order_hosts_for_user(hosts, [7], policy)]
    assert economy == ["Fastly HTTP", "Economy CF"]

    vip = [host.remark for host in order_hosts_for_user(hosts, [8], policy)]
    assert vip == ["VIP Reality"]

    both = [host.remark for host in order_hosts_for_user(hosts, [7, 8], policy)]
    assert both == ["VIP Reality", "Fastly HTTP", "Economy CF"]

    # Group ids may arrive as ints or strings.
    assert [host.remark for host in order_hosts_for_user(hosts, ["8"], policy)] == ["VIP Reality"]

    # Unlisted group → everything; a listed group with an empty allow list → everything.
    everything = ["VIP Reality", "Fastly HTTP", "Economy CF", "Plain"]
    assert [host.remark for host in order_hosts_for_user(hosts, [3], policy)] == everything
    assert [host.remark for host in order_hosts_for_user(hosts, [], policy)] == everything
    assert [host.remark for host in order_hosts_for_user(hosts, [7, 9], policy)] == everything


def test_policy_categories_override_and_invalid_regex_is_literal():
    policy = PresentationPolicy.from_dict(
        {
            "categories": [
                {"name": "Bronze", "match": "bronze"},
                {"name": "Gold", "match": ["gold", "[unterminated"]},
            ]
        }
    )
    hosts = _remarked("VIP", "gold [unterminated", "bronze", "gold")
    assert [host.remark for host in order_hosts_for_user(hosts, [], policy)] == [
        "bronze",
        "gold [unterminated",
        "VIP",
        "gold",
    ]


def test_load_policy_missing_file_and_mtime_reload(tmp_path):
    path = tmp_path / "policy.json"
    assert load_presentation_policy(str(path)) is None
    assert load_presentation_policy("") is None

    path.write_text(json.dumps({"groups": {"7": {"allow": ["fastly"]}}}), encoding="utf-8")
    loaded = load_presentation_policy(str(path))
    assert loaded is not None
    assert set(loaded.groups) == {"7"}
    assert loaded.categories == DEFAULT_POLICY.categories
    assert load_presentation_policy(str(path)) is loaded

    path.write_text(json.dumps({"groups": {"8": {"allow": ["vip"]}}}), encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))
    reloaded = load_presentation_policy(str(path))
    assert reloaded is not loaded
    assert set(reloaded.groups) == {"8"}

    path.write_text("{not json", encoding="utf-8")
    os.utime(path, (future + 5, future + 5))
    assert load_presentation_policy(str(path)) is None

    path.unlink()
    assert load_presentation_policy(str(path)) is None


def test_default_setting_path(monkeypatch):
    from config import subscription_env_settings

    assert (
        subscription_env_settings.group_presentation_policy_path == "/var/lib/hexogate/group-presentation-policy.json"
    )
    monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", "")
    assert presentation_policy.policy_path() == ""
    assert load_presentation_policy() is None


def _user(user_id: int, group_ids: list[int]) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        username=f"user{user_id}",
        status=UserStatus.active,
        inbounds=["in-a", "in-b", "in-c"],
        proxy_settings=ProxyTable(),
        group_ids=group_ids,
        data_limit=None,
        expire=None,
    )


@pytest.mark.asyncio
async def test_process_inbounds_applies_policy_file_and_stable_order(tmp_path, monkeypatch):
    policy_file = tmp_path / "group-presentation-policy.json"
    policy_file.write_text(
        json.dumps({"groups": {"7": {"allow": ["economy"]}, "8": {"allow": ["vip", "reality"]}}}), encoding="utf-8"
    )
    from config import subscription_env_settings

    monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", str(policy_file))

    hosts = {
        1: _host(remark="Economy CF", tag="in-a"),
        2: _host(remark="Reality Node", tag="in-b"),
        3: _host(remark="VIP Node", tag="in-c"),
    }
    monkeypatch.setattr("app.subscription.share.host_manager.get_hosts", AsyncMock(return_value=hosts))

    async def remarks_for(user, randomize_order=False):
        conf = StandardLinks()
        rendered = await process_inbounds_and_tags(user, _fmt(), conf, {}, randomize_order=randomize_order)
        return [urlparse(line).fragment.replace("%20", " ") for line in rendered.splitlines() if line]

    assert await remarks_for(_user(1, [7])) == ["Economy CF"]
    assert await remarks_for(_user(1, [8])) == ["VIP Node", "Reality Node"]
    assert await remarks_for(_user(1, [7, 8])) == ["VIP Node", "Reality Node", "Economy CF"]
    assert await remarks_for(_user(1, [])) == ["VIP Node", "Reality Node", "Economy CF"]

    # Random order is per-user stable and never breaks the category priority.
    user = _user(5, [])
    first = await remarks_for(user, randomize_order=True)
    for _ in range(5):
        assert await remarks_for(user, randomize_order=True) == first
    assert first == ["VIP Node", "Reality Node", "Economy CF"]

    # No policy file: ordering still applies, nothing is filtered.
    policy_file.unlink()
    assert await remarks_for(_user(1, [7])) == ["VIP Node", "Reality Node", "Economy CF"]
