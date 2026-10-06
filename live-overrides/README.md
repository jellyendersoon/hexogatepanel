# Live panel overrides: snapshot of 2026-10-06 (reference only, do not merge)

This folder is a copy of what production runs on top of stock `pasarguard/panel@sha256:019bd437…` (**v5.4.0**).
Paths mirror `/opt/pasarguard/` on the panel host. Nothing here is live config: it is a reference for checking that the fork matches production.

**Redactions (this repo is public):**
- Inbound tags in `group-presentation-policy.json` and the test/tool files are replaced by `TAG-<sha256[:8]>`.
  The mapping is consistent, so the same real tag always gives the same placeholder.
- Host ids, remarks and remark regexes are real (customers already see the remarks).
- `var-lib-pasarguard/hexo-time-guard.json` uses a placeholder admin name.
- No `.env`, `retry.env`, credentials, database or domains are included.

## What is mounted where

| File | Container | Mounted at |
|---|---|---|
| `overrides/hexo-time-guard/sitecustomize.py` | `pasarguard-pasarguard-1` (main panel) | `/code/.venv/lib/python3.14/site-packages/sitecustomize.py:ro` |
| `var-lib-pasarguard/hexo-time-guard.json` | main panel (read via the `/var/lib/pasarguard` volume) | `/var/lib/pasarguard/hexo-time-guard.json` |
| `overrides/group8-sub-renderer/sub_renderer.py` | `pasarguard-sub-renderer-canary` | `/code/sub_renderer.py` (entrypoint `python /code/sub_renderer.py`) |
| `overrides/group8-sub-renderer/subscription-share.py` | sub renderer | `/code/app/subscription/share.py` |
| `overrides/group8-sub-renderer/operation-subscription.py` | sub renderer | `/code/app/operation/subscription.py` |
| `overrides/group8-sub-renderer/presentation_policy.py` | sub renderer | `/code/app/subscription/presentation_policy.py` |
| `overrides/group8-sub-renderer/group-presentation-policy.json` | sub renderer | `/code/group-presentation-policy.json` (env `PASARGUARD_GROUP_PRESENTATION_POLICY_PATH`) |
| `overrides/subscription-xray.py` | sub renderer | `/code/app/subscription/xray.py` |
| `overrides/group8-sub-renderer/test_*.py`, `*_shadow_contract.py` | not mounted (offline tests and tools) | n/a |
| `docker-compose.yml` | main panel, caddy, mysql, phpmyadmin. **Live, always run with `-f docker-compose.yml`** | n/a |
| `docker-compose.override.yml` | **NOT live.** Plain `docker compose up` would load it and mount subscription-xray.py into the main panel | n/a |
| `docker-compose.sub-renderer.canary.yml` | sub renderer (compose project of its own) | n/a |
| `reserve-start/releases/2026.10.02.1/*` | `hexogate-panel-reserve-start` (image tag `:2026.10.02.2`, built from this release dir) | `reserve_start/` is baked into `/opt/reserve-start` |

**reserve-start notes:**
- The release dir also has `native-runtime/` (`app/`, `config.py`, `role.py`), which the Dockerfile copies into `/code`. It is omitted here because it is byte-identical to upstream tag v5.4.0.
- The sidecar runs as uid 65532, read-only, with `cap_drop ALL`. It listens on `/var/lib/pasarguard/reserve-start/reserve-start.sock`.
- Caddy routes `POST /api/user/*/reserve-start` there.
- It authenticates to the panel socket with `RESERVE_SERVICE_API_KEY` (or service user/password) from env.
- Its state is in `/state` (sqlite intents plus per-user locks).
