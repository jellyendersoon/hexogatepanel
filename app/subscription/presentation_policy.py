"""Group-based subscription presentation policy.

Hosts rendered into a customer's subscription are ordered by "method category"
(VIP first, then Fastly, Fastly HTTP, CF ECH, CF IPv6, CF Irancell, Reality IPv6,
Reality and finally everything else) and, optionally, filtered to a curated list
per user group.

The policy is an optional JSON file whose path comes from the
``GROUP_PRESENTATION_POLICY_PATH`` setting (default
``/var/lib/hexogate/group-presentation-policy.json``). A missing or unreadable
file means "no policy": hosts are still ordered using the built-in default
categories, nothing is filtered.

Schema::

    {
      "categories": [                      # optional; replaces DEFAULT_CATEGORIES
        {"name": "VIP", "match": "vip"},
        {"name": "Fastly HTTP", "match": ["fastly", "http"]},
        {"name": "CF IPv6", "match": ["ipv6", "cf|cloudflare"]}
      ],
      "groups": {                          # optional; keyed by group id as string
        "7": {"allow": ["economy", "fastly"]},
        "8": {"allow": ["vip", "reality"]}
      }
    }

``categories`` is an ordered list: the position is the display priority (first
is shown first). ``match`` is a case-insensitive regular expression, or a list
of regular expressions that must *all* match. Every pattern is searched against
the host remark and the inbound tag. When a host matches several categories the
most specific one (most patterns) wins, ties go to the earlier category, so
"Fastly HTTP" (two terms) beats "Fastly" (one term) for a remark containing
both words. A pattern that is not a valid regular expression is matched as a
literal substring. Hosts that match no category are placed last, keeping their
relative order.

``groups`` maps a group id (string) to an object with an ``allow`` list of
patterns (same syntax). A user in a listed group receives only the hosts whose
remark or inbound tag matches at least one allowed pattern. A user in several
listed groups receives the union of their lists. A group whose ``allow`` list
is empty or missing allows every host. Users in no listed group receive all
hosts. Nothing here hard-codes group ids; the owner's fleet uses "7" for the
Economy policy group and "8" for VIP, but that is configuration.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from config import subscription_env_settings

# Ordered by display priority. Multi-term matches are more specific than
# single-term ones, so "Fastly HTTP" wins over "Fastly" for a remark containing
# both words even though "Fastly" is listed first.
DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {"name": "VIP", "match": ["vip"]},
    {"name": "Fastly", "match": ["fastly"]},
    {"name": "Fastly HTTP", "match": ["fastly", "http"]},
    {"name": "CF ECH", "match": ["ech"]},
    {"name": "CF IPv6", "match": ["ipv6", r"cf|cloudflare"]},
    {"name": "CF Irancell", "match": ["irancell"]},
    {"name": "Reality IPv6", "match": ["reality", "ipv6"]},
    {"name": "Reality", "match": ["reality"]},
]


def _compile_pattern(raw: Any) -> re.Pattern | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return re.compile(text, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(text), re.IGNORECASE)


def _compile_patterns(raw: Any) -> list[re.Pattern]:
    if raw is None:
        return []
    if isinstance(raw, str | bytes):
        raw = [raw]
    if not isinstance(raw, list | tuple):
        return []
    compiled = [_compile_pattern(item) for item in raw]
    return [pattern for pattern in compiled if pattern is not None]


@dataclass(frozen=True)
class Category:
    name: str
    patterns: tuple[re.Pattern, ...]

    def matches(self, haystacks: Sequence[str]) -> bool:
        if not self.patterns:
            return False
        return all(any(pattern.search(text) for text in haystacks) for pattern in self.patterns)

    @property
    def specificity(self) -> int:
        return len(self.patterns)


@dataclass(frozen=True)
class GroupRule:
    allow: tuple[re.Pattern, ...] = ()

    @property
    def allows_everything(self) -> bool:
        return not self.allow

    def allows(self, haystacks: Sequence[str]) -> bool:
        if self.allows_everything:
            return True
        return any(pattern.search(text) for pattern in self.allow for text in haystacks)


@dataclass(frozen=True)
class PresentationPolicy:
    categories: tuple[Category, ...] = ()
    groups: dict[str, GroupRule] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> PresentationPolicy:
        data = data if isinstance(data, dict) else {}
        raw_categories = data.get("categories")
        if not isinstance(raw_categories, list) or not raw_categories:
            raw_categories = DEFAULT_CATEGORIES
        categories = tuple(
            Category(
                name=str(item.get("name") or f"category-{index}"), patterns=tuple(_compile_patterns(item.get("match")))
            )
            for index, item in enumerate(raw_categories)
            if isinstance(item, dict)
        )
        groups: dict[str, GroupRule] = {}
        raw_groups = data.get("groups")
        if isinstance(raw_groups, dict):
            for group_id, rule in raw_groups.items():
                if isinstance(rule, dict):
                    allow = rule.get("allow")
                elif isinstance(rule, list):
                    allow = rule
                else:
                    allow = None
                groups[str(group_id)] = GroupRule(allow=tuple(_compile_patterns(allow)))
        return cls(categories=categories, groups=groups)


DEFAULT_POLICY = PresentationPolicy.from_dict(None)

_cache_lock = threading.Lock()
_cache: dict[str, tuple[float | None, PresentationPolicy | None]] = {}


def policy_path() -> str:
    return getattr(subscription_env_settings, "group_presentation_policy_path", "") or ""


def load_presentation_policy(path: str | None = None) -> PresentationPolicy | None:
    """Load the policy file, reloading when its mtime changes.

    Returns ``None`` when the path is empty, the file is missing or cannot be
    parsed; callers then fall back to default ordering without filtering.
    """

    path = path if path is not None else policy_path()
    if not path:
        return None

    try:
        mtime: float | None = os.stat(path).st_mtime
    except OSError:
        mtime = None

    with _cache_lock:
        cached = _cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]

    policy: PresentationPolicy | None = None
    if mtime is not None:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            policy = PresentationPolicy.from_dict(data)
        except OSError, ValueError, TypeError, AttributeError:
            policy = None

    with _cache_lock:
        _cache[path] = (mtime, policy)
    return policy


def clear_presentation_policy_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _host_haystacks(host: Any) -> tuple[str, str]:
    if isinstance(host, dict):
        remark = host.get("remark")
        tag = host.get("inbound_tag")
    else:
        remark = getattr(host, "remark", None)
        tag = getattr(host, "inbound_tag", None)
    return str(remark or ""), str(tag or "")


def category_rank(host: Any, policy: PresentationPolicy | None = None) -> int:
    """Index of the best matching category; ``len(categories)`` when none match."""

    categories = (policy or DEFAULT_POLICY).categories
    haystacks = _host_haystacks(host)
    best_rank = len(categories)
    best_specificity = 0
    for rank, category in enumerate(categories):
        if category.specificity <= best_specificity:
            continue
        if category.matches(haystacks):
            best_rank = rank
            best_specificity = category.specificity
    return best_rank


def _allowed_rules(group_ids: Iterable[Any] | None, policy: PresentationPolicy | None) -> list[GroupRule] | None:
    if policy is None or not policy.groups:
        return None
    rules = [policy.groups[str(group_id)] for group_id in (group_ids or ()) if str(group_id) in policy.groups]
    return rules or None


def order_hosts_for_user(
    hosts: Iterable[Any], group_ids: Iterable[Any] | None, policy: PresentationPolicy | None
) -> list:
    """Filter hosts to the user's curated list and order them by category priority.

    Pure: depends only on its arguments. ``policy`` may be ``None`` (default
    categories, no filtering). The sort is stable, so hosts in the same category
    keep their incoming order.
    """

    hosts = list(hosts)
    rules = _allowed_rules(group_ids, policy)
    if rules is not None and not any(rule.allows_everything for rule in rules):
        hosts = [host for host in hosts if any(rule.allows(_host_haystacks(host)) for rule in rules)]

    effective = policy or DEFAULT_POLICY
    return sorted(hosts, key=lambda host: category_rank(host, effective))
