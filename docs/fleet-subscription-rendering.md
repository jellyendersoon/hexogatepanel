# Fleet subscription rendering

How the fork renders `/sub/*` the way production does: a stable per-user address per host row, a
data-driven group presentation policy, and an optional subscription-only renderer container.

## Stable address pick (`app/subscription/share.py`)

For every host row with several addresses, a user always gets the same one:

```
sorted(addresses)[ int(sha256(f"{user_id}|{key}").hexdigest(), 16) % len(addresses) ]
key = "|" + <row remark as stored, after the policy's remark override, before user formatting>
```

Different rows of the same user spread over the addresses, so blocking one IP only takes out part of
a user's rows. When the chosen address is also listed as an SNI of the row, the SNI is paired with it.
Without a user id the stock random choice is used. The digest is production's and must not change;
every customer would be moved to another address otherwise.

The SNI (when not paired), host header, port and Reality short id are picked from *independent*
digests (`<key>|sni`, `<key>|host`, `<key>|port`, `<key>|short_id`) so they are stable per user too
but can never alter the address choice. Production picks those at random; a stable pick is one of
its possible outcomes.

## Group presentation policy (`app/subscription/presentation_policy.py`)

A JSON file outside the source tree (it holds real host ids, remarks and inbound tags). It only
changes which rows are rendered, their remark and their order; never credentials, inbounds or
accounting. Every runtime error fails open to the unmodified list with one `ERROR` log per distinct
message. The file is read once per process; restart the renderer after editing it. Validate it
before deploying with:

```
uv run python -c "from app.subscription.presentation_policy import validate_policy_file as v; v('/path/policy.json')"
```

### Schema (as enforced by `validate_policy`)

```jsonc
{
  "version": 1,
  "host_group_visibility": [                      // optional; a host id may appear once
    {"any_group_ids": [7, 8], "host_ids": [112]}  // row 112 only for users in group 7 or 8
  ],
  "groups": {                                     // target groups; keys are positive ints
    "8": {
      "unknown_non_target_group": "fail_open",    // optional, only accepted value
      "hide_unless_non_target_authorized_host_ids": [108, 109],   // optional, unique ids
      "non_target_group_inbound_tags": {          // every other group a target user may have
        "1": ["TAG-REALITY", "TAG-HTTP"]          // -> inbound tags that group is entitled to
      },
      "scopes": [                                 // non-empty; names unique
        {
          "name": "germany",
          "match": {"remark_regex": "^🇩🇪"},      // rows claimed by the scope
          "activate_when_inbounds": ["TAG-MLKEM"],// optional; scope is active only if the user has all
          "selected": [                           // non-empty; host ids unique across all scopes
            {"host_id": 103, "remark": "🇩🇪 Germany ▸ Reality ⚡"},
            {"host_id": 107}                      // remark optional (non-empty string)
          ]
        }
      ]
    }
  }
}
```

Evaluation per user (`apply_group_presentation_policy`):

1. `host_group_visibility` drops listed rows unless the user has one of `any_group_ids`.
2. For each target group the user is in: every other group of the user must have a mapping in
   `non_target_group_inbound_tags` (otherwise: runtime drift, fail open); the union of their tags is
   the user's *non-target-authorized* tags.
3. Each active scope is projected as one block at the position of its first claimed row: the
   `selected` rows in policy order (renamed when a remark is given), then the other claimed rows
   whose inbound tag is non-target-authorized. Unselected, unauthorized claimed rows are dropped.
   Scopes may not overlap, and selected rows must exist and be in the user's inbounds.
4. Rows in `hide_unless_non_target_authorized_host_ids` are dropped unless an active scope selected
   them or their tag is non-target-authorized.

### Ordering and the group-subset rule

After the policy, `order_hosts_for_user` sorts the rows:

- Everyone: rows without a country flag (account/info rows) first, then by country in
  `LOCATION_FLAGS` order (DE, NL, TR, ES, GB, RU, FR, FI, SE, US, CA). The sort is stable, so the
  database (or shuffled) order survives inside a country.
- Only when the user's `group_ids` is a **non-empty subset of {7, 8}** (Economy, VIP): additionally
  by method, in `method_rank` order — VIP, Fastly, Fastly HTTP, Cloudflare ECH, Cloudflare IPv6,
  Cloudflare, Reality IPv6, Reality, everything else — with the country order inside each method.
  Paid group 1, mixed-group users and any ordering error keep the plain country order.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `GROUP_PRESENTATION_POLICY_PATH` (alias `PASARGUARD_GROUP_PRESENTATION_POLICY_PATH`) | `/var/lib/hexogate/group-presentation-policy.json` | policy file; absent file = stock rendering |
| `SUB_RENDERER_SOCKET` | `/var/lib/pasarguard/sub-renderer-canary.socket` | renderer listening socket; production routes to this name |
| `SUB_RENDERER_HEARTBEAT` | `/tmp/pasarguard-sub-renderer-refresh-generation` | file updated after every successful refresh |

The policy and the stable pick are active in the main panel as well: the code is the same, only the
policy file decides.

## Subscription-only renderer container

`sub_renderer.py` starts a FastAPI app with just the subscription router on a Unix socket
(mode `0660`, `root:root`, refused otherwise), refreshes the host catalog, settings caches and client
templates every 30 s, and never writes to the database. Run it next to the panel with

```
docker compose -f docker-compose.sub-renderer.yml up -d
```

It shares the panel's `.env` and image, mounts `/var/lib/hexogate` (policy) and
`/var/lib/pasarguard` (socket). Point the reverse proxy at the socket for `/sub/*` with the main
panel socket as fallback, as production's Caddy does.
