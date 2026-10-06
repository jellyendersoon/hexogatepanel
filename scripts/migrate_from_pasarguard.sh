#!/usr/bin/env bash
# Migrate an existing PasarGuard installation to Hexogate.
#
# What it does (each step is skipped when there is nothing to do):
#   1. Stops the old service / compose stack.
#   2. Moves /var/lib/pasarguard to /var/lib/hexogate and leaves a symlink
#      behind so anything still pointing at the old path keeps working.
#   3. Rewrites /var/lib/pasarguard paths inside the .env file next to this
#      repository, and the old "pasarguard" socket and log file names.
#   4. Reminds you about settings that are not rewritten automatically.
#
# Usage:
#   sudo bash scripts/migrate_from_pasarguard.sh            # perform the migration
#   sudo bash scripts/migrate_from_pasarguard.sh --dry-run  # only print what would change
#
# Run it from the Hexogate checkout (the directory that contains main.py).

set -euo pipefail

OLD_DIR="/var/lib/pasarguard"
NEW_DIR="/var/lib/hexogate"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="$REPO_DIR/.env"
DRY_RUN=0

for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $arg" >&2; exit 1 ;;
  esac
done

say()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*"; }
run()  { if [ "$DRY_RUN" = 1 ]; then echo "    (dry-run) $*"; else "$@"; fi; }

if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" -ne 0 ]; then
  echo "This script moves files under /var/lib; run it with sudo." >&2
  exit 1
fi

# 1. Stop whatever is currently running the panel.
say "Stopping the old service"
if command -v systemctl >/dev/null 2>&1 && systemctl list-unit-files pasarguard.service >/dev/null 2>&1; then
  run systemctl stop pasarguard.service || true
  run systemctl disable pasarguard.service || true
fi
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'pasarguard'; then
  run docker stop pasarguard || true
fi

# 2. Move the data directory.
if [ -d "$OLD_DIR" ] && [ ! -L "$OLD_DIR" ]; then
  if [ -e "$NEW_DIR" ]; then
    warn "$NEW_DIR already exists; leaving $OLD_DIR in place. Merge them by hand before starting Hexogate."
  else
    say "Moving $OLD_DIR to $NEW_DIR"
    run mv "$OLD_DIR" "$NEW_DIR"
    run ln -s "$NEW_DIR" "$OLD_DIR"
  fi
elif [ -d "$NEW_DIR" ]; then
  say "$NEW_DIR already exists, nothing to move"
else
  say "No $OLD_DIR found, creating an empty $NEW_DIR"
  run mkdir -p "$NEW_DIR"
fi

# 3. Rewrite paths and names in .env.
if [ -f "$ENV_FILE" ]; then
  say "Updating $ENV_FILE"
  if [ "$DRY_RUN" = 1 ]; then
    grep -nE 'pasarguard' "$ENV_FILE" || echo "    nothing to rewrite"
  else
    cp "$ENV_FILE" "$ENV_FILE.pre-hexogate.bak"
    sed -i \
      -e 's#/var/lib/pasarguard#/var/lib/hexogate#g' \
      -e 's#/run/pasarguard\.socket#/run/hexogate.socket#g' \
      -e 's#"pasarguard\.log"#"hexogate.log"#g' \
      "$ENV_FILE"
    echo "    backup written to $ENV_FILE.pre-hexogate.bak"
  fi
else
  warn "No .env found at $ENV_FILE; copy .env.example and fill it in."
fi

# 4. Things that need a human decision.
cat <<EOF

Done. Before you start Hexogate, check these by hand:

  * Database URL. If SQLALCHEMY_DATABASE_URL points at a PostgreSQL or MySQL
    database named "pasarguard", that database is untouched and keeps working.
    Rename it only if you want to; nothing in the panel depends on the name.

  * NATS names. The defaults changed from "pasarguard.*" / "pasarguard_*" to
    "hexogate.*" / "hexogate_*". If you never set NATS_*_SUBJECT or
    NATS_*_KV_BUCKET in .env, every worker picks up the new defaults as long as
    you restart them all together. If you did set them explicitly, they stay
    exactly as you wrote them.

  * Service. Install the new systemd unit with "bash install_service.sh" and
    enable it with "systemctl enable --now hexogate", or run
    "docker compose up -d" if you use Docker. The old "pasarguard" unit was
    stopped and disabled but not deleted.

  * CLI. The commands are now "hexogate-cli" and "hexogate-tui" inside the
    container, or "uv run hexogate-cli.py" from a source checkout.
EOF
