#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

import presentation_policy as policy


FORMATS = (
    "v2box-links-base64",
    "streisand-xray-json",
    "happ-links",
    "v2rayng-links",
    "clash-verge-meta",
    "clash-meta",
    "sfa-sing-box",
    "karing-sing-box",
    "outline-json",
    "raw-links",
    "raw-links-base64",
    "wireguard-zip",
)


@dataclass
class Host:
    id: int
    remark: str
    inbound_tag: str

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


@dataclass
class HostManagerValue:
    """Production host-manager values intentionally do not carry the DB row id."""

    remark: str
    inbound_tag: str

    def model_copy(self, update=None):
        return replace(self, **(update or {}))


@dataclass
class User:
    group_ids: list[int]
    inbounds: list[str]


META_A = Host(17240, "👤 Account", "META-A")
META_B = Host(17163, "📊 Data", "META-B")
DE_REALITY = Host(17769, "old reality", "TAG-0952895f")
DE_REALITY_2 = Host(17770, "🇩🇪 Reality · Germany 2", "TAG-0952895f")
DE_CDN = Host(17465, "🇩🇪 CDN · Germany", "TAG-64b522d7")
DE_FASTLY = Host(17787, "🇩🇪 CDN · Fastly · Germany", "TAG-ca600c74")
DE_HTTP = Host(17320, "🇩🇪 HTTP · Germany", "TAG-07459dac")
DE_OLD_VIP = Host(17460, "🇩🇪 VIP · Germany", "TAG-2ce4e984")
DE_GROUP8_ONLY = Host(17341, "👑 TUN 🇩🇪 Germany", "TAG-19818a7a")
DE_MLKEM = Host(17783, "old mlkem", "TAG-80fb763b")
NL = Host(17766, "🇳🇱 Reality · Netherlands", "NL-REALITY")
HOSTS = [META_A, META_B, DE_REALITY, DE_REALITY_2, DE_CDN, DE_FASTLY, DE_HTTP, DE_OLD_VIP, DE_GROUP8_ONLY, DE_MLKEM, NL]
ALL_TAGS = sorted({host.inbound_tag for host in HOSTS})


def host_manager_items(hosts=HOSTS):
    return [
        (host.id, HostManagerValue(host.remark, host.inbound_tag))
        for host in hosts
    ]


class PresentationPolicyTests(unittest.TestCase):
    def setUp(self):
        os.environ[policy.POLICY_ENV] = str(
            Path(__file__).with_name("group-presentation-policy.json")
        )
        policy._runtime_policy.cache_clear()
        policy._log_runtime_drift_once.cache_clear()

    def tearDown(self):
        policy._runtime_policy.cache_clear()
        policy._log_runtime_drift_once.cache_clear()

    def test_strict_file_validation(self):
        parsed = policy.validate_policy_file(os.environ[policy.POLICY_ENV])
        self.assertEqual([8], sorted(parsed["groups"]))

    def test_all_twelve_formats_share_exact_pure_vip_projection(self):
        user = User([8], ALL_TAGS)
        expected_ids = [17240, 17163, 17769, 17783, 17320, 17766]
        for client_format in FORMATS:
            with self.subTest(client_format=client_format):
                output = policy.apply_group_presentation_policy(user, HOSTS)
                self.assertEqual(expected_ids, [host.id for host in output])
                self.assertEqual("🇩🇪 Reality · Germany · پرسرعت", output[2].remark)
                self.assertEqual("👑 【🇮🇷 → 🇩🇪】 VIP · Germany", output[3].remark)

    def test_metadata_rows_and_non_scope_order_are_preserved(self):
        output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), HOSTS)
        self.assertIs(META_A, output[0])
        self.assertIs(META_B, output[1])
        self.assertIs(NL, output[-1])

    def test_randomize_order_cannot_break_selected_reality_then_vip_order(self):
        user = User([8], ALL_TAGS)
        observed_non_scope_orders = set()
        for seed in range(20):
            shuffled = list(HOSTS)
            random.Random(seed).shuffle(shuffled)
            output = policy.apply_group_presentation_policy(user, shuffled)
            ids = [host.id for host in output]
            reality_index = ids.index(17769)
            self.assertEqual(17783, ids[reality_index + 1])
            self.assertEqual(17320, ids[reality_index + 2])
            observed_non_scope_orders.add(tuple(host_id for host_id in ids if host_id not in (17769, 17783)))
        self.assertGreater(len(observed_non_scope_orders), 1)

    def test_real_host_manager_values_without_id_activate_for_pure_group8(self):
        output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), host_manager_items())
        self.assertEqual([17240, 17163, 17769, 17783, 17320, 17766], [host_id for host_id, _ in output])
        self.assertEqual("🇩🇪 Reality · Germany · پرسرعت", output[2][1].remark)
        self.assertEqual("👑 【🇮🇷 → 🇩🇪】 VIP · Germany", output[3][1].remark)
        self.assertFalse(hasattr(output[2][1], "id"))

    def test_real_host_manager_pair_randomization_keeps_selected_order(self):
        observed_non_scope_orders = set()
        for seed in range(20):
            shuffled = host_manager_items()
            random.Random(seed).shuffle(shuffled)
            output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), shuffled)
            ids = [host_id for host_id, _ in output]
            reality_index = ids.index(17769)
            self.assertEqual(17783, ids[reality_index + 1])
            self.assertEqual(17320, ids[reality_index + 2])
            observed_non_scope_orders.add(tuple(host_id for host_id in ids if host_id not in (17769, 17783)))
        self.assertGreater(len(observed_non_scope_orders), 1)

    def test_real_host_manager_pairs_preserve_mapped_group1_contribution(self):
        output = policy.apply_group_presentation_policy(User([1, 8], ALL_TAGS), host_manager_items())
        ids = [host_id for host_id, _ in output]
        for preserved in (17770, 17465, 17787, 17320):
            self.assertIn(preserved, ids)
        self.assertEqual(ids.index(17783), ids.index(17769) + 1)
        self.assertNotIn(17460, ids)
        self.assertNotIn(17341, ids)

    def test_non_target_group_one_and_seven_are_byte_equivalent_inputs(self):
        for group_id in (1, 7):
            original = list(HOSTS)
            output = policy.apply_group_presentation_policy(User([group_id], ALL_TAGS), original)
            self.assertIs(original, output)

    def test_multi_group_preserves_non_target_group_ten_contribution(self):
        output = policy.apply_group_presentation_policy(User([5, 8, 10], ALL_TAGS), HOSTS)
        ids = [host.id for host in output]
        self.assertEqual([17240, 17163, 17769, 17783, 17320, 17460, 17766], ids)
        self.assertNotIn(17341, ids)

    def test_multi_group_one_preserves_its_broad_de_union(self):
        output = policy.apply_group_presentation_policy(User([1, 3, 4, 5, 8, 9, 10], ALL_TAGS), HOSTS)
        ids = [host.id for host in output]
        for preserved in (17770, 17465, 17787, 17320, 17460):
            self.assertIn(preserved, ids)
        self.assertNotIn(17341, ids)

    def test_fastly_is_visible_to_group1_plus_vip_but_hidden_from_pure_vip(self):
        broad = policy.apply_group_presentation_policy(User([1, 8], ALL_TAGS), HOSTS)
        pure_vip = policy.apply_group_presentation_policy(User([8], ALL_TAGS), HOSTS)
        self.assertIn(17787, [host.id for host in broad])
        self.assertNotIn(17787, [host.id for host in pure_vip])

    def test_unknown_multi_group_fails_open_with_loud_log(self):
        original = list(HOSTS)
        with self.assertLogs(policy.LOGGER, level="ERROR"):
            output = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
        self.assertIs(original, output)

    def test_repeated_identical_runtime_drift_is_rate_limited(self):
        original = list(HOSTS)
        with mock.patch.object(policy.LOGGER, "error") as error:
            first = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
            second = policy.apply_group_presentation_policy(User([8, 999], ALL_TAGS), original)
        self.assertIs(original, first)
        self.assertIs(original, second)
        self.assertEqual(1, error.call_count)

    def test_missing_selected_host_fails_open_with_loud_log(self):
        original = [host for host in HOSTS if host.id != 17783]
        with self.assertLogs(policy.LOGGER, level="ERROR"):
            output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), original)
        self.assertIs(original, output)

    def test_inactive_scope_still_hides_legacy_group8_rows(self):
        original = list(HOSTS)
        inbounds = [tag for tag in ALL_TAGS if tag != DE_MLKEM.inbound_tag]
        with mock.patch.object(policy.LOGGER, "error") as error:
            output = policy.apply_group_presentation_policy(User([8], inbounds), original)
        self.assertIsNot(original, output)
        self.assertEqual(
            [host.id for host in original if host.id not in {17460, 17341}],
            [host.id for host in output],
        )
        self.assertEqual(0, error.call_count)

    def test_invalid_policy_blocks_predeploy_but_runtime_fails_open(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory) / "invalid.json"
            invalid.write_text(json.dumps({"version": 99, "groups": {}}))
            with self.assertRaises(policy.PresentationPolicyError):
                policy.validate_policy_file(invalid)
            os.environ[policy.POLICY_ENV] = str(invalid)
            policy._runtime_policy.cache_clear()
            original = list(HOSTS)
            with self.assertLogs(policy.LOGGER, level="ERROR"):
                output = policy.apply_group_presentation_policy(User([8], ALL_TAGS), original)
            self.assertIs(original, output)

    def test_missing_runtime_policy_fails_open(self):
        os.environ.pop(policy.POLICY_ENV)
        policy._runtime_policy.cache_clear()
        original = list(HOSTS)
        self.assertIs(original, policy.apply_group_presentation_policy(User([8], ALL_TAGS), original))


if __name__ == "__main__":
    unittest.main(verbosity=2)
