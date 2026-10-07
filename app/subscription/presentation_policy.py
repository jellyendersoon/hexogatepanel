"""Data-driven subscription host presentation policy.

This module changes only rendered host visibility/order. It does not alter
group membership, inbound credentials, cores, nodes, listeners, or accounting.
Runtime errors are availability-safe: the original host list is returned.
The validator is intentionally strict so deployment tooling can fail closed.

Active scopes are projected before the conditional-hide pass, and only IDs
explicitly selected by an active scope are exempt from that pass. This lets a
scope select a normally hidden row without exposing it while the activation
tag is absent.

The policy file is runtime data (real host ids, remarks and inbound tags) and is
never part of the source tree. Its path comes from ``GROUP_PRESENTATION_POLICY_PATH``
(or the production name ``PASARGUARD_GROUP_PRESENTATION_POLICY_PATH``), see
``config.SubscriptionEnvSettings``. It is read once per process; restart the
renderer to pick up an edited file.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from config import subscription_env_settings

LOGGER = logging.getLogger("hexogate.subscription.presentation_policy")
# Production env name; the fork also accepts GROUP_PRESENTATION_POLICY_PATH (see config.py).
POLICY_ENV = "PASARGUARD_GROUP_PRESENTATION_POLICY_PATH"

# Keep the order shared by v2rayNG and the other subscription formats stable.
# A country may have several routes; sorting remains stable within that country.
LOCATION_FLAGS = (
    "🇩🇪",
    "🇳🇱",
    "🇹🇷",
    "🇪🇸",
    "🇬🇧",
    "🇷🇺",
    "🇫🇷",
    "🇫🇮",
    "🇸🇪",
    "🇺🇸",
    "🇨🇦",
)

# Economy (7) and VIP (8) lists are ordered by method first (see method_rank),
# country order inside each class. Users with any other group keep the plain
# country order.
CLASS_ORDER_GROUP_IDS = frozenset({7, 8})


class PresentationPolicyError(ValueError):
    pass


def _positive_id(value: Any, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise PresentationPolicyError(f"{label} must be an integer") from exc
    if number < 1:
        raise PresentationPolicyError(f"{label} must be positive")
    return number


def validate_policy(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("version") != 1:
        raise PresentationPolicyError("policy version must be 1")
    groups = data.get("groups")
    if not isinstance(groups, dict):
        raise PresentationPolicyError("groups must be an object")

    raw_visibility = data.get("host_group_visibility", [])
    if not isinstance(raw_visibility, list):
        raise PresentationPolicyError("host_group_visibility must be a list")
    visibility: dict[int, frozenset[int]] = {}
    for rule in raw_visibility:
        if not isinstance(rule, dict):
            raise PresentationPolicyError("host visibility rule must be an object")
        host_ids = rule.get("host_ids")
        group_ids = rule.get("any_group_ids")
        if not isinstance(host_ids, list) or not host_ids or not isinstance(group_ids, list) or not group_ids:
            raise PresentationPolicyError("host visibility rules need non-empty host_ids and any_group_ids")
        allowed = [_positive_id(value, "visibility group id") for value in group_ids]
        if len(allowed) != len(set(allowed)):
            raise PresentationPolicyError("visibility group IDs contain duplicates")
        for value in host_ids:
            host_id = _positive_id(value, "visibility host id")
            if host_id in visibility:
                raise PresentationPolicyError("visibility host IDs contain duplicates")
            visibility[host_id] = frozenset(allowed)
    normalized: dict[str, Any] = {"version": 1, "groups": {}, "host_group_visibility": visibility}
    for raw_group_id, raw_group in groups.items():
        group_id = _positive_id(raw_group_id, "group id")
        if not isinstance(raw_group, dict):
            raise PresentationPolicyError(f"group {group_id} must be an object")
        unknown_mode = raw_group.get("unknown_non_target_group", "fail_open")
        if unknown_mode != "fail_open":
            raise PresentationPolicyError("runtime unknown-group mode must be fail_open")

        raw_hidden_ids = raw_group.get("hide_unless_non_target_authorized_host_ids", [])
        if not isinstance(raw_hidden_ids, list):
            raise PresentationPolicyError("hide_unless_non_target_authorized_host_ids must be a list")
        hidden_ids = [_positive_id(value, "conditionally hidden host id") for value in raw_hidden_ids]
        if len(hidden_ids) != len(set(hidden_ids)):
            raise PresentationPolicyError("hide_unless_non_target_authorized_host_ids contains duplicates")

        raw_mappings = raw_group.get("non_target_group_inbound_tags", {})
        if not isinstance(raw_mappings, dict):
            raise PresentationPolicyError("non_target_group_inbound_tags must be an object")
        mappings: dict[int, frozenset[str]] = {}
        for raw_other_id, raw_tags in raw_mappings.items():
            other_id = _positive_id(raw_other_id, "non-target group id")
            if other_id == group_id:
                raise PresentationPolicyError("target group cannot be a non-target mapping")
            if not isinstance(raw_tags, list) or any(not isinstance(tag, str) or not tag for tag in raw_tags):
                raise PresentationPolicyError(f"group {other_id} tags must be non-empty strings")
            if len(raw_tags) != len(set(raw_tags)):
                raise PresentationPolicyError(f"group {other_id} tags contain duplicates")
            mappings[other_id] = frozenset(raw_tags)

        raw_scopes = raw_group.get("scopes")
        if not isinstance(raw_scopes, list) or not raw_scopes:
            raise PresentationPolicyError(f"group {group_id} scopes must be a non-empty list")
        scopes = []
        scope_names: set[str] = set()
        selected_ids: set[int] = set()
        for raw_scope in raw_scopes:
            if not isinstance(raw_scope, dict):
                raise PresentationPolicyError("scope must be an object")
            name = raw_scope.get("name")
            if not isinstance(name, str) or not name or name in scope_names:
                raise PresentationPolicyError("scope names must be unique non-empty strings")
            scope_names.add(name)
            match = raw_scope.get("match")
            pattern = match.get("remark_regex") if isinstance(match, dict) else None
            if not isinstance(pattern, str) or not pattern:
                raise PresentationPolicyError(f"scope {name} requires match.remark_regex")
            try:
                compiled = re.compile(pattern)
            except re.error as exc:
                raise PresentationPolicyError(f"scope {name} has invalid remark regex") from exc

            raw_activation = raw_scope.get("activate_when_inbounds", [])
            if (
                not isinstance(raw_activation, list)
                or any(not isinstance(tag, str) or not tag for tag in raw_activation)
                or len(raw_activation) != len(set(raw_activation))
            ):
                raise PresentationPolicyError(f"scope {name} activate_when_inbounds must be unique strings")

            raw_selected = raw_scope.get("selected")
            if not isinstance(raw_selected, list) or not raw_selected:
                raise PresentationPolicyError(f"scope {name} selected must be a non-empty list")
            selected = []
            for raw_item in raw_selected:
                if not isinstance(raw_item, dict):
                    raise PresentationPolicyError("selected item must be an object")
                host_id = _positive_id(raw_item.get("host_id"), "selected host id")
                if host_id in selected_ids:
                    raise PresentationPolicyError(f"selected host {host_id} is duplicated")
                selected_ids.add(host_id)
                remark = raw_item.get("remark")
                if remark is not None and (not isinstance(remark, str) or not remark):
                    raise PresentationPolicyError("selected remark must be a non-empty string")
                selected.append({"host_id": host_id, "remark": remark})
            scopes.append(
                {
                    "name": name,
                    "remark_regex": compiled,
                    "activate_when_inbounds": frozenset(raw_activation),
                    "selected": selected,
                }
            )
        normalized["groups"][group_id] = {
            "unknown_non_target_group": unknown_mode,
            "hide_unless_non_target_authorized_host_ids": frozenset(hidden_ids),
            "non_target_group_inbound_tags": mappings,
            "scopes": scopes,
        }
    return normalized


def validate_policy_file(path: str | os.PathLike[str]) -> dict[str, Any]:
    policy_path = Path(path)
    return validate_policy(json.loads(policy_path.read_text(encoding="utf-8")))


def policy_path() -> str:
    """Configured policy file path ('' disables the policy)."""
    return (subscription_env_settings.group_presentation_policy_path or "").strip()


@lru_cache(maxsize=1)
def _runtime_policy() -> dict[str, Any] | None:
    path = policy_path()
    if not path:
        return None
    if not Path(path).is_file():
        LOGGER.info("group presentation policy file %s is absent; rendering original hosts", path)
        return None
    try:
        return validate_policy_file(path)
    except Exception:
        LOGGER.exception("group presentation policy unavailable or invalid; rendering original hosts")
        return None


@lru_cache(maxsize=256)
def _log_runtime_drift_once(message: str) -> None:
    # Only ever called from the except block in apply_group_presentation_policy,
    # so the traceback of the drift is attached; the cache rate-limits identical drift.
    LOGGER.error(
        "group presentation policy runtime drift; rendering original hosts: %s",
        message,
        exc_info=True,  # noqa: LOG014
    )


def clear_presentation_policy_cache() -> None:
    """Forget the loaded policy and the drift-log de-duplication (tests, reloads)."""
    _runtime_policy.cache_clear()
    _log_runtime_drift_once.cache_clear()


def _split_host_entry(entry: Any) -> tuple[int | None, Any, bool]:
    """Return ``(host_id, host_data, is_pair)`` for fixtures or manager items."""
    if isinstance(entry, tuple) and len(entry) == 2:
        host_id, host = entry
        try:
            return int(host_id), host, True
        except (TypeError, ValueError) as exc:
            raise PresentationPolicyError("host-manager item id must be an integer") from exc
    host_id = getattr(entry, "id", None)
    return (int(host_id) if host_id is not None else None), entry, False


def _host_id(entry: Any) -> int | None:
    return _split_host_entry(entry)[0]


def _host_data(entry: Any) -> Any:
    return _split_host_entry(entry)[1]


def sort_hosts_by_location(hosts: list[Any]) -> list[Any]:
    """Group rendered hosts by destination, keeping metadata and route order."""

    def location_rank(entry: Any) -> int:
        remark = str(getattr(_host_data(entry), "remark", "") or "")
        matches = ((remark.find(flag), rank) for rank, flag in enumerate(LOCATION_FLAGS) if flag in remark)
        first = min(matches, default=None)
        return first[1] + 1 if first else 0

    return sorted(hosts, key=location_rank)


# 2026-10-06 owner: Fastly blocks are ordered by measured origin speed (Germany Fastly is served by DE-02).
FASTLY_SPEED_ORDER = [
    "Germany 2 \u25b8",
    "Germany \u25b8",
    "Finland \u25b8",
    "Sweden 2 \u25b8",
    "NL \u25b8",
    "Netherlands 1 \u25b8",
    "USA \u25b8",
    "Spain \u25b8",
    "Canada \u25b8",
    "France 2 \u25b8",
    "Sweden \u25b8",
    "Turkey \u25b8",
    "UK New \u25b8",
    "UK \u25b8",
]


def _class_rank(entry: Any) -> int:
    remark = str(getattr(_host_data(entry), "remark", "") or "")
    if not any(flag in remark for flag in LOCATION_FLAGS):
        return 0  # account/info rows stay on top
    return method_rank(remark)


def _class_key(entry: Any) -> tuple[int, int]:
    """Method rank, then (for the two Fastly classes only) the measured-speed location order."""
    remark = str(getattr(_host_data(entry), "remark", "") or "")
    rank = _class_rank(entry)
    if rank in (3, 4):
        hits = [index for index, name in enumerate(FASTLY_SPEED_ORDER) if name in remark]
        return rank, (min(hits) if hits else len(FASTLY_SPEED_ORDER))
    return rank, 0


def method_rank(remark: str) -> int:
    """2026-10-07 owner order: VIP, Reality (country order, so DE and NL lead), Fastly TLS, Fastly HTTP,
    HTTP TLS, HTTP (HTTP 2091 / HTTP ML-KEM), Cloudflare, then everything else."""
    if "VIP" in remark:
        return 1
    if "Reality" in remark:
        return 2
    if "Fastly" in remark:
        return 4 if "HTTP" in remark else 3
    if "Cloudflare" in remark:
        return 7
    if "HTTP TLS" in remark:
        return 5
    if "HTTP" in remark or "ML-KEM" in remark:
        return 6
    return 8


def _dedupe_info_rows(hosts: list[Any]) -> list[Any]:
    """2026-10-06 owner ("there are two profiles configs"): multi-group users got the same account/info row
    twice (an old row plus the new Fastly-backed info rows). Keep only the LAST row per identical info remark."""
    keep, seen = [], set()
    for entry in reversed(hosts):
        remark = str(getattr(_host_data(entry), "remark", "") or "")
        if remark and not any(flag in remark for flag in LOCATION_FLAGS):
            if remark in seen:
                continue
            seen.add(remark)
        keep.append(entry)
    return list(reversed(keep))


def order_hosts_for_user(user: Any, hosts: list[Any]) -> list[Any]:
    """Country order for everyone; method-then-country (Fastly by speed) for Economy/VIP-only users."""
    try:
        hosts = _dedupe_info_rows(hosts)
    except Exception:  # presentation only
        LOGGER.exception("info-row dedupe failed; keeping all rows")
    by_location = sort_hosts_by_location(hosts)
    try:
        group_ids = {int(group_id) for group_id in (getattr(user, "group_ids", None) or [])}
        if not group_ids or not group_ids <= CLASS_ORDER_GROUP_IDS:
            return by_location
        return sorted(by_location, key=_class_key)
    except Exception:  # presentation only: never break a subscription
        LOGGER.exception("class ordering failed; keeping country order")
        return by_location


def _clone_with_remark(entry: Any, remark: str | None) -> Any:
    host_id, host, is_pair = _split_host_entry(entry)
    if not remark or getattr(host, "remark", None) == remark:
        return entry
    if hasattr(host, "model_copy"):
        cloned = host.model_copy(update={"remark": remark})
    else:
        cloned = copy.copy(host)
        cloned.remark = remark
    return (host_id, cloned) if is_pair else cloned


def _apply_scopes(hosts: list[Any], scopes: list[dict[str, Any]], non_target_tags: frozenset[str]) -> list[Any]:
    """Project active scopes as one policy-ordered block.

    Each scope still preserves authorized non-target-group rows that match its
    protocol.  Coordinating the projection is important: inserting scopes one
    at a time leaves their relative order controlled by stale database
    positions instead of by the policy's declared scope order.
    """
    by_id = {host_id: entry for entry in hosts if (host_id := _host_id(entry)) is not None}
    all_selected_ids = {item["host_id"] for scope in scopes for item in scope["selected"]}
    claimed_indices: set[int] = set()
    scope_matches: list[tuple[dict[str, Any], list[int]]] = []
    for scope in scopes:
        selected = scope["selected"]
        selected_ids = {item["host_id"] for item in selected}
        missing = [item["host_id"] for item in selected if item["host_id"] not in by_id]
        if missing:
            raise PresentationPolicyError(f"scope {scope['name']} missing selected host IDs {missing}")
        matched_indices = [
            index
            for index, entry in enumerate(hosts)
            if _host_id(entry) in selected_ids
            or scope["remark_regex"].search(str(getattr(_host_data(entry), "remark", "") or ""))
        ]
        if not matched_indices:
            raise PresentationPolicyError(f"scope {scope['name']} matched no hosts")
        overlap = claimed_indices.intersection(matched_indices)
        if overlap:
            raise PresentationPolicyError(f"scope {scope['name']} overlaps another active scope")
        claimed_indices.update(matched_indices)
        scope_matches.append((scope, matched_indices))

    ordered = []
    for scope, matched_indices in scope_matches:
        ordered.extend(_clone_with_remark(by_id[item["host_id"]], item.get("remark")) for item in scope["selected"])
        matched = set(matched_indices)
        ordered.extend(
            entry
            for index, entry in enumerate(hosts)
            if index in matched
            and _host_id(entry) not in all_selected_ids
            and getattr(_host_data(entry), "inbound_tag", None) in non_target_tags
        )

    first = min(claimed_indices)
    result = []
    for index, entry in enumerate(hosts):
        if index == first:
            result.extend(ordered)
        if index not in claimed_indices:
            result.append(entry)
    return result


def apply_group_presentation_policy(user: Any, hosts: list[Any]) -> list[Any]:
    """Return a presentation-only host list; fail open on every runtime error."""
    original = hosts
    policy = _runtime_policy()
    if not policy:
        return original
    fallback = original
    try:
        group_ids = {int(group_id) for group_id in (getattr(user, "group_ids", None) or [])}
        visibility = policy["host_group_visibility"]
        visible = [
            entry
            for entry in hosts
            if _host_id(entry) not in visibility or group_ids.intersection(visibility[_host_id(entry)])
        ]
        # Scoped projection errors must not undo an explicit host visibility rule.
        fallback = visible if len(visible) != len(hosts) else original
        target_ids = sorted(group_ids.intersection(policy["groups"]))
        if not target_ids:
            return fallback
        rendered = list(fallback)
        applied = fallback is not original
        for target_id in target_ids:
            group_policy = policy["groups"][target_id]
            other_ids = group_ids - {target_id}
            mappings = group_policy["non_target_group_inbound_tags"]
            unknown = sorted(other_id for other_id in other_ids if other_id not in mappings)
            if unknown:
                raise PresentationPolicyError(f"target group {target_id} has unmapped non-target groups {unknown}")
            non_target_tags = frozenset().union(*(mappings[group_id] for group_id in other_ids))
            hidden_ids = group_policy["hide_unless_non_target_authorized_host_ids"]
            active_scope_selected_ids: set[int] = set()
            user_inbounds = set(getattr(user, "inbounds", None) or [])
            active_scopes = []

            for scope in group_policy["scopes"]:
                if not scope["activate_when_inbounds"].issubset(user_inbounds):
                    continue
                scope_selected_ids = {item["host_id"] for item in scope["selected"]}
                selected_tags = {
                    getattr(_host_data(entry), "inbound_tag", None)
                    for entry in rendered
                    if _host_id(entry) in scope_selected_ids
                }
                if None in selected_tags or not selected_tags.issubset(user_inbounds):
                    raise PresentationPolicyError(
                        f"scope {scope['name']} selected inbound tags are not in the user inbound union"
                    )
                active_scopes.append(scope)
                active_scope_selected_ids.update(scope_selected_ids)

            if active_scopes:
                rendered = _apply_scopes(rendered, active_scopes, non_target_tags)
                applied = True

            if hidden_ids:
                before = len(rendered)
                rendered = [
                    entry
                    for entry in rendered
                    if _host_id(entry) not in hidden_ids
                    or _host_id(entry) in active_scope_selected_ids
                    or getattr(_host_data(entry), "inbound_tag", None) in non_target_tags
                ]
                applied = applied or len(rendered) != before
        return rendered if applied else original
    except Exception as exc:
        _log_runtime_drift_once(str(exc))
        return fallback
