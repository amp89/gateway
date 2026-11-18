#!/usr/bin/env sh
# Run the gateway with hot reload in the foreground on http://127.0.0.1:${GATEWAY_DEV_PORT:-38081} (Ctrl+C to stop).
# Dev auto-unlocks with a labelled dev-only passphrase. Data lives in the gateway-dev-data volume.
set -e
cd "$(dirname "$0")"
docker compose -f compose.dev.docker.yaml build
docker compose -f compose.dev.docker.yaml down >/dev/null 2>&1
docker compose -f compose.dev.docker.yaml run --rm volume-init
# Ctrl+C: podman-compose 1.0.x leaves containers running, so always bring the stack down on exit.
down() { trap - INT TERM; docker compose -f compose.dev.docker.yaml down >/dev/null 2>&1 || true; }
trap 'down; exit 130' INT TERM
docker compose -f compose.dev.docker.yaml up || true
down
