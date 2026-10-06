#!/usr/bin/env bash
# Recreate the Hexogate panel container without colliding with leftovers.
#
# A plain "docker compose up -d" can trip over stray containers that still carry
# the compose service labels from an older run (a previous project name, a
# manual docker run, an interrupted recreate). This script removes those first,
# pulls the configured image, and recreates the service in one go.
#
# Usage: bash scripts/compose_recreate.sh [--no-pull]

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

SERVICE="hexogate"
PULL=1
[ "${1:-}" = "--no-pull" ] && PULL=0

# Containers that claim to be our service but are not managed by this compose project.
project="$(docker compose config --format json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin).get("name",""))' || true)"
mapfile -t strays < <(docker ps -a --filter "label=com.docker.compose.service=${SERVICE}" --format '{{.ID}} {{.Names}} {{.Label "com.docker.compose.project"}}' \
  | awk -v p="$project" '$3 != p {print $1" ("$2", project "$3")"}')
mapfile -t named < <(docker ps -a --filter "name=^/${SERVICE}$" --format '{{.ID}} {{.Label "com.docker.compose.project"}}' \
  | awk -v p="$project" '$2 != p {print $1" (named '"${SERVICE}"', project "$2")"}')

for entry in "${strays[@]}" "${named[@]}"; do
  [ -z "$entry" ] && continue
  id="${entry%% *}"
  echo "Removing stray container $entry"
  docker rm -f "$id" >/dev/null
done

[ "$PULL" = 1 ] && docker compose pull "$SERVICE"
docker compose up -d --force-recreate --remove-orphans "$SERVICE"
docker compose ps "$SERVICE"
