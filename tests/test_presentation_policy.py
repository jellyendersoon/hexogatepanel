"""Group presentation policy: validation, scoped projection, visibility, ordering and fail-open behaviour.

All ids, remarks and inbound tags here are made up. The real policy is runtime data and never
enters the source tree.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, replace
from pathlib import Path
from unittest import mock

import pytest

from app.subscription import presentation_policy as policy
from config import SubscriptionEnvSettings, subscription_env_settings


@dataclass
class Host:
    id: int
    remark: str
    inbound_tag: str

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


@dataclass
class HostManagerValue:
    """Host-manager values do not carry the DB row id; the id travels in the (id, value) pair."""

    remark: str
    inbound_tag: str

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


@dataclass
class User:
    group_ids: list[int]
    inbounds: list[str]


META_A = Host(101, "👤 Account", "META-A")
META_B = Host(102, "📊 Data", "META-B")
DE_REALITY = Host(103, "old reality", "TAG-REALITY")
DE_REALITY_2 = Host(104, "🇩🇪 Reality · Germany 2", "TAG-REALITY")
DE_CDN = Host(105, "🇩🇪 CDN · Germany", "TAG-CDN")
DE_FASTLY = Host(106, "🇩🇪 CDN · Fastly · Germany", "TAG-FASTLY")
DE_HTTP = Host(107, "🇩🇪 HTTP · Germany", "TAG-HTTP")
DE_OLD_VIP = Host(108, "🇩🇪 VIP · Germany", "TAG-OLD-VIP")
DE_GROUP8_ONLY = Host(109, "👑 TUN 🇩🇪 Germany", "TAG-TUN")
DE_MLKEM = Host(110, "old mlkem", "TAG-MLKEM")
NL = Host(111, "🇳🇱 Reality · Netherlands", "TAG-NL-REALITY")
TR_ECONOMY = Host(112, "🇹🇷 Turkey ▸ Reality", "TAG-TR-REALITY")
GB_PAID = Host(113, "🇬🇧 UK ▸ Reality", "TAG-GB-REALITY")
HOSTS = [META_A, META_B, DE_REALITY, DE_REALITY_2, DE_CDN, DE_FASTLY, DE_HTTP, DE_OLD_VIP, DE_GROUP8_ONLY, DE_MLKEM, NL]
ALL_TAGS = sorted({host.inbound_tag for host in HOSTS + [TR_ECONOMY, GB_PAID]})

SELECTED_REALITY_REMARK = "🇩🇪 Reality · Germany · fast"
SELECTED_VIP_REMARK = "👑 【🇮🇷 → 🇩🇪】 VIP · Germany"
PURE_VIP_IDS = [101, 102, 103, 110, 107, 111]

POLICY = {
    "version": 1,
    "host_group_visibility": [
        {"any_group_ids": [7, 8], "host_ids": [TR_ECONOMY.id]},
        {"any_group_ids": [1], "host_ids": [GB_PAID.id]},
    ],
    "groups": {
        "8": {
            "unknown_non_target_group": "fail_open",
            "hide_unless_non_target_authorized_host_ids": [DE_OLD_VIP.id, DE_GROUP8_ONLY.id],
            "non_target_group_inbound_tags": {
                "1": ["TAG-REALITY", "TAG-CDN", "TAG-FASTLY", "TAG-HTTP"],
                "3": ["TAG-GROUP3"],
                "4": ["TAG-GROUP4"],
                "5": ["TAG-GROUP5"],
                "7": ["TAG-TR-REALITY"],
                "9": ["TAG-GROUP9"],
                "10": ["TAG-OLD-VIP"],
            },
            "scopes": [
                {
                    "name": "germany",
                    "match": {"remark_regex": "🇩🇪"},
                    "activate_when_inbounds": ["TAG-MLKEM"],
                    "selected": [
                        {"host_id": DE_REALITY.id, "remark": SELECTED_REALITY_REMARK},
                        {"host_id": DE_MLKEM.id, "remark": SELECTED_VIP_REMARK},
                        {"host_id": DE_HTTP.id},
                    ],
                }
            ],
        }
    },
}


def host_manager_items(hosts=HOSTS):
    return [(host.id, HostManagerValue(host.remark, host.inbound_tag)) for host in hosts]


def _use_policy(monkeypatch, tmp_path: Path, data) -> Path:
    path = tmp_path / "group-presentation-policy.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", str(path))
    policy.clear_presentation_policy_cache()
    return path


@pytest.fixture
def policy_file(monkeypatch, tmp_path):
    path = _use_policy(monkeypatch, tmp_path, POLICY)
    yield path
    policy.clear_presentation_policy_cache()


# ---------------------------------------------------------------- validation


def test_strict_file_validation(policy_file):
    parsed = policy.validate_policy_file(policy_file)
    assert sorted(parsed["groups"]) == [8]
    group = parsed["groups"][8]
    assert group["hide_unless_non_target_authorized_host_ids"] == frozenset({108, 109})
    assert group["non_target_group_inbound_tags"][10] == frozenset({"TAG-OLD-VIP"})
    assert group["scopes"][0]["activate_when_inbounds"] == frozenset({"TAG-MLKEM"})
    assert group["scopes"][0]["selected"][2] == {"host_id": 107, "remark": None}
    assert parsed["host_group_visibility"] == {112: frozenset({7, 8}), 113: frozenset({1})}


def _policy_with(**group_overrides):
    data = json.loads(json.dumps(POLICY))
    data["groups"]["8"].update(group_overrides)
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 2, "groups": {}}, "version must be 1"),
        ({"version": 1}, "groups must be an object"),
        ({"version": 1, "groups": {}, "host_group_visibility": {}}, "host_group_visibility must be a list"),
        ({"version": 1, "groups": {}, "host_group_visibility": [{"host_ids": [1]}]}, "non-empty host_ids"),
        (
            {"version": 1, "groups": {}, "host_group_visibility": [{"host_ids": [1, 1], "any_group_ids": [8]}]},
            "visibility host IDs contain duplicates",
        ),
        ({"version": 1, "groups": {"0": {}}}, "group id must be positive"),
        ({"version": 1, "groups": {"8": []}}, "group 8 must be an object"),
        (_policy_with(unknown_non_target_group="fail_closed"), "must be fail_open"),
        (_policy_with(hide_unless_non_target_authorized_host_ids=[108, 108]), "contains duplicates"),
        (_policy_with(non_target_group_inbound_tags={"8": ["x"]}), "target group cannot be a non-target mapping"),
        (_policy_with(non_target_group_inbound_tags={"1": ["x", "x"]}), "tags contain duplicates"),
        (_policy_with(non_target_group_inbound_tags={"1": [""]}), "must be non-empty strings"),
        (_policy_with(scopes=[]), "scopes must be a non-empty list"),
        (_policy_with(scopes=[{"name": "", "match": {"remark_regex": "x"}}]), "scope names must be unique"),
        (_policy_with(scopes=[{"name": "a", "match": {}}]), "requires match.remark_regex"),
        (_policy_with(scopes=[{"name": "a", "match": {"remark_regex": "["}}]), "invalid remark regex"),
        (
            _policy_with(scopes=[{"name": "a", "match": {"remark_regex": "x"}, "activate_when_inbounds": ["t", "t"]}]),
            "must be unique strings",
        ),
        (_policy_with(scopes=[{"name": "a", "match": {"remark_regex": "x"}, "selected": []}]), "non-empty list"),
        (
            _policy_with(scopes=[{"name": "a", "match": {"remark_regex": "x"}, "selected": [{"host_id": -1}]}]),
            "selected host id must be positive",
        ),
        (
            _policy_with(
                scopes=[
                    {"name": "a", "match": {"remark_regex": "x"}, "selected": [{"host_id": 1}]},
                    {"name": "b", "match": {"remark_regex": "y"}, "selected": [{"host_id": 1}]},
                ]
            ),
            "selected host 1 is duplicated",
        ),
        (
            _policy_with(
                scopes=[{"name": "a", "match": {"remark_regex": "x"}, "selected": [{"host_id": 1, "remark": ""}]}]
            ),
            "selected remark must be a non-empty string",
        ),
    ],
)
def test_validate_policy_rejects_bad_shapes(data, message):
    with pytest.raises(policy.PresentationPolicyError, match=message):
        policy.validate_policy(data)


# ---------------------------------------------------------------- scoped projection


def test_pure_vip_projection_selects_renames_and_drops_unselected_rows(policy_file):
    output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), HOSTS)
    assert [host.id for host in output] == PURE_VIP_IDS
    assert output[2].remark == SELECTED_REALITY_REMARK
    assert output[3].remark == SELECTED_VIP_REMARK
    assert output[4] is DE_HTTP  # selected without a remark override: original object
    # Inputs are never mutated.
    assert DE_REALITY.remark == "old reality"


def test_metadata_rows_and_non_scope_order_are_preserved(policy_file):
    output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), HOSTS)
    assert output[0] is META_A
    assert output[1] is META_B
    assert output[-1] is NL


def test_randomize_order_cannot_break_selected_order(policy_file):
    user = User([8], ALL_TAGS)
    observed_non_scope_orders = set()
    for seed in range(20):
        shuffled = list(HOSTS)
        random.Random(seed).shuffle(shuffled)
        ids = [host.id for host in policy.apply_group_presentation_policy(user, shuffled)]
        reality_index = ids.index(DE_REALITY.id)
        assert ids[reality_index + 1] == DE_MLKEM.id
        assert ids[reality_index + 2] == DE_HTTP.id
        observed_non_scope_orders.add(tuple(i for i in ids if i not in (DE_REALITY.id, DE_MLKEM.id, DE_HTTP.id)))
    assert len(observed_non_scope_orders) > 1


def test_host_manager_pairs_without_id_attribute_activate(policy_file):
    output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), host_manager_items())
    assert [host_id for host_id, _ in output] == PURE_VIP_IDS
    assert output[2][1].remark == SELECTED_REALITY_REMARK
    assert output[3][1].remark == SELECTED_VIP_REMARK
    assert not hasattr(output[2][1], "id")


def test_host_manager_pair_randomization_keeps_selected_order(policy_file):
    for seed in range(20):
        shuffled = host_manager_items()
        random.Random(seed).shuffle(shuffled)
        ids = [host_id for host_id, _ in policy.apply_group_presentation_policy(User([8], ALL_TAGS), shuffled)]
        reality_index = ids.index(DE_REALITY.id)
        assert ids[reality_index + 1] == DE_MLKEM.id
        assert ids[reality_index + 2] == DE_HTTP.id


def test_pairs_preserve_mapped_group1_contribution(policy_file):
    output = policy.apply_group_presentation_policy(User([1, 8], ALL_TAGS), host_manager_items())
    ids = [host_id for host_id, _ in output]
    for preserved in (DE_REALITY_2.id, DE_CDN.id, DE_FASTLY.id, DE_HTTP.id):
        assert preserved in ids
    assert ids.index(DE_MLKEM.id) == ids.index(DE_REALITY.id) + 1
    assert DE_OLD_VIP.id not in ids
    assert DE_GROUP8_ONLY.id not in ids


def test_non_target_groups_get_the_original_list_object(policy_file):
    for group_id in (1, 3):
        original = list(HOSTS)
        assert policy.apply_group_presentation_policy(User([group_id], ALL_TAGS), original) is original
    original = list(HOSTS)
    assert policy.apply_group_presentation_policy(User([], ALL_TAGS), original) is original


def test_multi_group_preserves_non_target_group_ten_contribution(policy_file):
    ids = [host.id for host in policy.apply_group_presentation_policy(User([5, 8, 10], ALL_TAGS), HOSTS)]
    assert ids == [101, 102, 103, 110, 107, 108, 111]
    assert DE_GROUP8_ONLY.id not in ids


def test_multi_group_one_preserves_its_broad_de_union(policy_file):
    ids = [host.id for host in policy.apply_group_presentation_policy(User([1, 3, 4, 5, 8, 9, 10], ALL_TAGS), HOSTS)]
    for preserved in (DE_REALITY_2.id, DE_CDN.id, DE_FASTLY.id, DE_HTTP.id, DE_OLD_VIP.id):
        assert preserved in ids
    assert DE_GROUP8_ONLY.id not in ids


def test_fastly_is_visible_to_group1_plus_vip_but_hidden_from_pure_vip(policy_file):
    broad = policy.apply_group_presentation_policy(User([1, 8], ALL_TAGS), HOSTS)
    pure_vip = policy.apply_group_presentation_policy(User([8], ALL_TAGS), HOSTS)
    assert DE_FASTLY.id in [host.id for host in broad]
    assert DE_FASTLY.id not in [host.id for host in pure_vip]


def test_inactive_scope_still_hides_legacy_group8_rows(policy_file):
    original = list(HOSTS)
    inbounds = [tag for tag in ALL_TAGS if tag != DE_MLKEM.inbound_tag]
    with mock.patch.object(policy.LOGGER, "error") as error:
        output = policy.apply_group_presentation_policy(User([8], inbounds), original)
    assert output is not original
    assert [host.id for host in output] == [host.id for host in original if host.id not in {108, 109}]
    assert error.call_count == 0


# ---------------------------------------------------------------- host visibility


def test_visibility_rule_applies_to_every_group_and_survives_scope_drift(policy_file):
    hosts = [*HOSTS, TR_ECONOMY, GB_PAID]

    def ids_for(group_ids):
        return [host.id for host in policy.apply_group_presentation_policy(User(group_ids, ALL_TAGS), hosts)]

    assert ids_for([1]) == [host.id for host in HOSTS] + [GB_PAID.id]
    assert ids_for([7]) == [host.id for host in HOSTS] + [TR_ECONOMY.id]
    for group_ids in ([], [3, 5]):
        assert ids_for(group_ids) == [host.id for host in HOSTS]
    assert ids_for([8]) == [*PURE_VIP_IDS, TR_ECONOMY.id]
    assert ids_for([1, 8]) == [101, 102, 103, 110, 107, 104, 105, 106, 111, 112, 113]

    # A scope runtime error falls back to the visibility-filtered list, never to the raw one.
    with mock.patch.object(policy.LOGGER, "error") as error:
        assert ids_for([8, 999]) == [host.id for host in HOSTS] + [TR_ECONOMY.id]
    assert error.call_count == 1


# ---------------------------------------------------------------- fail open


def test_unknown_multi_group_fails_open_with_loud_log(policy_file):
    original = list(HOSTS)
    with mock.patch.object(policy.LOGGER, "error") as error:
        output = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
    assert output is original
    assert error.call_count == 1
    assert "unmapped non-target groups [999]" in error.call_args.args[1]


def test_repeated_identical_runtime_drift_is_rate_limited(policy_file):
    original = list(HOSTS)
    with mock.patch.object(policy.LOGGER, "error") as error:
        first = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
        second = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
    assert first is original
    assert second is original
    assert error.call_count == 1


def test_missing_selected_host_fails_open_with_loud_log(policy_file):
    original = [host for host in HOSTS if host.id != DE_MLKEM.id]
    with mock.patch.object(policy.LOGGER, "error") as error:
        output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), original)
    assert output is original
    assert error.call_count == 1


def test_selected_tag_outside_user_inbounds_fails_open(policy_file):
    original = list(HOSTS)
    inbounds = [tag for tag in ALL_TAGS if tag != DE_HTTP.inbound_tag]
    with mock.patch.object(policy.LOGGER, "error") as error:
        output = policy.apply_group_presentation_policy(User([8], inbounds), original)
    assert output is original
    assert error.call_count == 1


def test_invalid_policy_blocks_predeploy_but_runtime_fails_open(monkeypatch, tmp_path):
    invalid = _use_policy(monkeypatch, tmp_path, {"version": 99, "groups": {}})
    with pytest.raises(policy.PresentationPolicyError):
        policy.validate_policy_file(invalid)
    original = list(HOSTS)
    with mock.patch.object(policy.LOGGER, "exception") as exception:
        output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), original)
    assert output is original
    assert exception.call_count == 1
    policy.clear_presentation_policy_cache()


def test_unset_or_absent_policy_path_fails_open_quietly(monkeypatch, tmp_path):
    original = list(HOSTS)
    for path in ("", str(tmp_path / "missing.json")):
        monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", path)
        policy.clear_presentation_policy_cache()
        with mock.patch.object(policy.LOGGER, "error") as error, mock.patch.object(policy.LOGGER, "exception") as exc:
            assert policy.apply_group_presentation_policy(User([8], ALL_TAGS), original) is original
        assert error.call_count == 0
        assert exc.call_count == 0
    policy.clear_presentation_policy_cache()


def test_policy_is_read_once_per_process(policy_file, monkeypatch):
    assert policy._runtime_policy() is not None
    monkeypatch.setattr(subscription_env_settings, "group_presentation_policy_path", "")
    assert policy._runtime_policy() is not None  # cached until clear_presentation_policy_cache()
    policy.clear_presentation_policy_cache()
    assert policy._runtime_policy() is None


# ---------------------------------------------------------------- settings


def test_policy_path_setting_accepts_both_env_names(monkeypatch):
    for name in ("GROUP_PRESENTATION_POLICY_PATH", "PASARGUARD_GROUP_PRESENTATION_POLICY_PATH"):
        monkeypatch.delenv(name, raising=False)
    assert SubscriptionEnvSettings(_env_file=None).group_presentation_policy_path == (
        "/var/lib/hexogate/group-presentation-policy.json"
    )
    monkeypatch.setenv("PASARGUARD_GROUP_PRESENTATION_POLICY_PATH", "/code/policy.json")
    assert SubscriptionEnvSettings(_env_file=None).group_presentation_policy_path == "/code/policy.json"
    monkeypatch.delenv("PASARGUARD_GROUP_PRESENTATION_POLICY_PATH")
    monkeypatch.setenv("GROUP_PRESENTATION_POLICY_PATH", "/data/policy.json")
    assert SubscriptionEnvSettings(_env_file=None).group_presentation_policy_path == "/data/policy.json"
    assert policy.POLICY_ENV == "PASARGUARD_GROUP_PRESENTATION_POLICY_PATH"


# ---------------------------------------------------------------- ordering


@pytest.mark.parametrize(
    ("remark", "rank"),
    [
        ("🇩🇪 Germany ▸ VIP 👑", 1),
        ("🇩🇪 Germany ▸ Fastly", 2),
        ("🇩🇪 Germany ▸ Fastly HTTP", 3),
        ("🇩🇪 Germany ▸ Reality", 4),
        ("🇩🇪 Germany ▸ HTTP TLS", 4),
        ("🇩🇪 Germany ▸ HTTP ML-KEM · A", 4),
        ("🇩🇪 Germany ▸ Reality IPv6 · A", 5),
        ("🇩🇪 Germany ▸ Cloudflare IPv6 · A", 6),
        ("🇩🇪 Germany ▸ Cloudflare ECH · A", 7),
        ("🇩🇪 Germany ▸ Cloudflare · B", 8),
        ("🇩🇪 Germany ▸ mKCP", 9),
        ("🇩🇪 Germany ▸ IPv6only", 9),  # word boundary: "IPv6only" is not the IPv6 method
    ],
)
def test_method_rank(remark, rank):
    assert policy.method_rank(remark) == rank


def test_fastly_rows_follow_measured_speed_order():
    # 2026-10-06: inside the Fastly classes the measured origin speed decides, not the country order.
    rows = _rows(
        "🇬🇧 UK New ▸ Fastly",
        "🇩🇪 Germany ▸ Fastly",
        "🇫🇮 Finland ▸ Fastly HTTP",
        "🇩🇪 Germany 2 ▸ Fastly",
        "🇸🇪 Sweden 2 ▸ Fastly HTTP",
        "🇩🇪 Germany ▸ Reality",
    )
    ordered = [host.remark for host in policy.order_hosts_for_user(User([8], []), rows)]
    assert ordered == [
        "🇩🇪 Germany 2 ▸ Fastly",
        "🇩🇪 Germany ▸ Fastly",
        "🇬🇧 UK New ▸ Fastly",
        "🇫🇮 Finland ▸ Fastly HTTP",
        "🇸🇪 Sweden 2 ▸ Fastly HTTP",
        "🇩🇪 Germany ▸ Reality",
    ]
    # Paid / mixed-group users keep the plain country order.
    assert [host.remark for host in policy.order_hosts_for_user(User([1], []), rows)] == [
        "🇩🇪 Germany ▸ Fastly",
        "🇩🇪 Germany 2 ▸ Fastly",
        "🇩🇪 Germany ▸ Reality",
        "🇬🇧 UK New ▸ Fastly",
        "🇫🇮 Finland ▸ Fastly HTTP",
        "🇸🇪 Sweden 2 ▸ Fastly HTTP",
    ]


def test_duplicate_info_rows_keep_only_the_last_copy():
    # 2026-10-06 owner: multi-group users saw the same account row twice; the last copy wins.
    rows = _rows("👤 Account", "🇩🇪 Germany ▸ Reality", "👤 Account", "📊 Data", "📊 Data")
    for groups in ([8], [1], [1, 8], []):
        ordered = policy.order_hosts_for_user(User(groups, []), rows)
        assert [host.remark for host in ordered] == ["👤 Account", "📊 Data", "🇩🇪 Germany ▸ Reality"]
        assert [host.id for host in ordered if host.remark == "👤 Account"] == [202]
        assert [host.id for host in ordered if host.remark == "📊 Data"] == [204]


def _rows(*remarks: str) -> list[Host]:
    return [Host(200 + index, remark, f"TAG-{index}") for index, remark in enumerate(remarks)]


COUNTRY_INPUT = (
    "🇳🇱 Netherlands ▸ Reality",
    "🇩🇪 Germany ▸ Fastly",
    "👤 Account",
    "🇩🇪 Germany ▸ Reality",
    "🇹🇷 Turkey ▸ VIP 👑",
    "📊 Data",
    "🇩🇪 Germany ▸ VIP 👑",
    "no flag at all",
)
COUNTRY_ORDER = [
    "👤 Account",
    "📊 Data",
    "no flag at all",
    "🇩🇪 Germany ▸ Fastly",
    "🇩🇪 Germany ▸ Reality",
    "🇩🇪 Germany ▸ VIP 👑",
    "🇳🇱 Netherlands ▸ Reality",
    "🇹🇷 Turkey ▸ VIP 👑",
]
METHOD_ORDER = [
    "👤 Account",
    "📊 Data",
    "no flag at all",
    "🇩🇪 Germany ▸ VIP 👑",
    "🇹🇷 Turkey ▸ VIP 👑",
    "🇩🇪 Germany ▸ Fastly",
    "🇩🇪 Germany ▸ Reality",
    "🇳🇱 Netherlands ▸ Reality",
]


def test_sort_hosts_by_location_keeps_info_rows_first_and_is_stable():
    hosts = _rows(*COUNTRY_INPUT)
    assert [host.remark for host in policy.sort_hosts_by_location(hosts)] == COUNTRY_ORDER
    assert [host.remark for host in hosts] == list(COUNTRY_INPUT)
    pairs = [(host.id, host) for host in hosts]
    assert [host.remark for _, host in policy.sort_hosts_by_location(pairs)] == COUNTRY_ORDER


@pytest.mark.parametrize("group_ids", [[7], [8], [7, 8], ["7"], [8, 7]])
def test_economy_and_vip_only_users_get_method_then_country_order(group_ids):
    ordered = policy.order_hosts_for_user(User(group_ids, []), _rows(*COUNTRY_INPUT))
    assert [host.remark for host in ordered] == METHOD_ORDER


@pytest.mark.parametrize("group_ids", [[], None, [1], [1, 8], [7, 1], [3], [8, 999]])
def test_everyone_else_keeps_stock_country_order(group_ids):
    ordered = policy.order_hosts_for_user(User(group_ids, []), _rows(*COUNTRY_INPUT))
    assert [host.remark for host in ordered] == COUNTRY_ORDER


def test_ordering_errors_fall_back_to_country_order():
    user = User(["not-a-number"], [])
    with mock.patch.object(policy.LOGGER, "exception") as exception:
        ordered = policy.order_hosts_for_user(user, _rows(*COUNTRY_INPUT))
    assert [host.remark for host in ordered] == COUNTRY_ORDER
    assert exception.call_count == 1
