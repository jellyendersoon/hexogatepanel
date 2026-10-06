<p align="center">
  <a href="https://hexogate.com">
    <img src="./dashboard/public/statics/favicon/logo-dark.png" alt="Hexogate" width="140" />
  </a>
</p>

<h1 align="center">Hexogate</h1>

<p align="center">
  Unified, censorship-resistant proxy management panel.<br />
  <a href="https://hexogate.com">hexogate.com</a>
</p>

---

## Overview

Hexogate is a self-hosted control panel for running and managing proxy infrastructure. It ships with a web dashboard, a REST API, a CLI, a Telegram bot, and multi-node support, and it drives [Xray-core](https://github.com/XTLS/Xray-core) and [WireGuard](https://www.wireguard.com/) backends.

### Features

**Web interface and API**
- Built-in web dashboard
- Full REST API backend
- Multi-node support for distributing infrastructure

**Protocols and security**
- VMess, VLESS, Trojan, Shadowsocks, WireGuard and Hysteria2
- TLS and REALITY
- Multiple protocols for a single user

**User management**
- Multiple users on a single inbound
- Multiple inbounds on a single port (fallbacks)
- Traffic and expiry limits, including periodic limits (daily, weekly, and so on)
- HWID / device limits for hardware-bound access

**Subscriptions and sharing**
- Subscription links compatible with V2Ray, Clash and Clash Meta
- Automatic share-link and QR code generation
- System monitoring and traffic statistics

**Tools and customization**
- Customizable Xray configuration
- Integrated Telegram bot
- Command-line interface
- Multi-language dashboard
- Multi-admin with role-based access control

## Installation

### Docker Compose

1. Create the data directory and copy the example configuration:

   ```bash
   sudo mkdir -p /var/lib/hexogate
   git clone https://github.com/jellyendersoon/hexogatepanel.git /opt/hexogate
   cd /opt/hexogate
   cp .env.example .env
   ```

2. Edit `.env` to set at least `UVICORN_HOST`, `UVICORN_PORT` and `SQLALCHEMY_DATABASE_URL`.

3. Start the panel:

   ```bash
   docker compose up -d
   ```

   The default `docker-compose.yml` pulls `ghcr.io/jellyendersoon/hexogatepanel:latest`. To build from source instead, replace the `image:` line with `build: .`.

4. To update or recreate the panel later, use the helper instead of a bare `docker compose up`. It clears leftover containers that still carry the service labels, which otherwise make a recreate fail or start a second panel:

   ```bash
   bash scripts/compose_recreate.sh
   ```

5. Create the owner account. Generate a one-time setup key and use it on the dashboard login page:

   ```bash
   docker compose exec hexogate hexogate-cli generate-temp-key
   ```

### Migrating from PasarGuard

On a server that already runs PasarGuard, stop it and run the migration script from the Hexogate checkout. It moves the data directory to `/var/lib/hexogate` (leaving a symlink behind), rewrites the paths in your `.env`, and prints the remaining manual steps:

```bash
sudo bash scripts/migrate_from_pasarguard.sh --dry-run   # preview
sudo bash scripts/migrate_from_pasarguard.sh             # apply
```

### From source

Requires Python 3.14+, [uv](https://docs.astral.sh/uv/) and [Bun](https://bun.sh).

```bash
git clone https://github.com/jellyendersoon/hexogatepanel.git
cd hexogatepanel
uv sync
cd dashboard && bun install && bun run build && cd ..
cp .env.example .env
uv run main.py
```

## CLI

```bash
# Inside the container
hexogate-cli --help
hexogate-cli generate-temp-key

# From a source checkout
uv run hexogate-cli.py --help
```

See [`cli/README.md`](./cli/README.md) for the full command reference.

## Development

```bash
make help
```

Contribution guidelines are in [CONTRIBUTING.md](./CONTRIBUTING.md).

## License

Hexogate is a fork of an AGPL-3.0 licensed project and is distributed under the same license. See [LICENSE](./LICENSE).
