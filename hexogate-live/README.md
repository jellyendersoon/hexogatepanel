# Live Hexogate panel patches (snapshot 2026-10-06)

These files are the patches the production panel runs today. Each one is bind-mounted over the stock
`pasarguard/panel` **v5.4.0** image. Fold them into the real source tree (5.4.0 to 5.4.1 changes only
one WireGuard line in `app/subscription/xray.py`; there are no migrations), then delete this folder.

| File here | Replaces / adds in the image | Runs in |
|---|---|---|
| `app__subscription__xray.py` | `app/subscription/xray.py` (drops `allowInsecure` from the TLS block; keep the 5.4.1 `remoteDNS` line) | sub renderer (an unused override also mounts it into the main panel) |
| `app__subscription__share.py` | `app/subscription/share.py` (stable per-user address pick + SNI pairing; group policy + method ordering hook) | sub renderer |
| `app__operation__subscription.py` | `app/operation/subscription.py` (`load_groups=True`, `user.group_ids` for the policy) | sub renderer |
| `app__subscription__presentation_policy.py` | new module `app/subscription/presentation_policy.py` | sub renderer |
| `sub_renderer.py` | new entrypoint `/code/sub_renderer.py`: subscription-only FastAPI app on its own UDS | sub renderer |
| `hexo_time_guard_sitecustomize.py` | mounted as `site-packages/sitecustomize.py` (owner rule, see below) | main panel |

## Production layout (what these patches assume)
- **Main panel container:** stock image plus only the time guard. It serves `/var/lib/pasarguard/pasarguard.socket`.
- **Sub renderer container** (`pasarguard-sub-renderer-canary`):
  - Same image, `entrypoint python /code/sub_renderer.py`, with every patch except the time guard.
  - It listens on `/var/lib/pasarguard/sub-renderer-canary.socket` with mode 0660 root:root and refreshes hosts and caches every 30 s.
  - Env: `PASARGUARD_GROUP_PRESENTATION_POLICY_PATH=/code/group-presentation-policy.json`, `SQLALCHEMY_POOL_SIZE=10`, `SQLALCHEMY_MAX_OVERFLOW=20`.
- **Caddy** sends `/sub/*` to the renderer socket first, with the main panel socket as fallback.
- **Keep unchanged in the fork:** the data dir `/var/lib/pasarguard`, the socket names and the env var names. Production depends on them.

## Runtime data that must NOT be committed (this fork is public)
- **Group presentation policy JSON:** real host ids, remarks and inbound tags. Loaded from the path in
  `PASARGUARD_GROUP_PRESENTATION_POLICY_PATH`. Its shape (validated by `validate_policy()`):
  ```
  {"version": int,
   "groups": {"<group id>": {
       "hide_unless_non_target_authorized_host_ids": [int],
       "non_target_group_inbound_tags": {"<group id>": [str]},
       "scopes": [{"name": str, "match": {"remark_regex": str}, "activate_when_inbounds": [...],
                   "selected": [{"host_id": int, "remark": str}]}],
       "unknown_non_target_group": str}},
   "host_group_visibility": [{"any_group_ids": [int], "host_ids": [int]}]}
  ```
- **Time-guard admin list:** `/var/lib/pasarguard/hexo-time-guard.json`, shaped `{"admins": ["<admin username>"]}`. It is re-read when the file changes.
- **Tests:** write them with made-up host ids and tags. Never use real ones.

## Owner rules encoded here
- **Method order (groups 7 Economy / 8 VIP only):** rows sort by method first, then country.
  - Rank: info rows (no flag), VIP, Fastly, Fastly HTTP, Cloudflare ECH, Cloudflare IPv6, Cloudflare (Irancell),
    Reality IPv6, Reality, rest. See `method_rank()`.
  - Only applies when the user's group_ids is a non-empty subset of {7, 8}. Paid group 1, mixed-group users and
    any error fall back to the stock country order.
- **Time guard:** admins on the list may add data, reset usage and edit notes, but never add time to existing users.
  - Blocked: a later expire (10 min slack), an unlimited expire, a new on_hold, a longer on_hold duration, a later on_hold timeout. All return 403 (English + Persian).
  - Wraps `UserOperation._prepare_modified_user`.
  - It is a sitecustomize monkeypatch today; in the fork it should become a proper check inside that method, plus a setting.
- **Stable address pick:** a user keeps the same address per row across refreshes, and different rows spread over the addresses.
