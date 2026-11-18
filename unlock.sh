#!/usr/bin/env sh
# Unlock the running gateway: prompts for the passphrase without echo and posts it from inside the container
# (via a boot-time token only readable inside the container). Works with podman or docker.
set -e
cd "$(dirname "$0")"
NAME="${GATEWAY_CONTAINER:-gateway}"
if command -v podman >/dev/null 2>&1 && podman container exists "$NAME" 2>/dev/null; then E=podman
elif command -v docker >/dev/null 2>&1 && docker inspect "$NAME" >/dev/null 2>&1; then E=docker
else echo "container '$NAME' not running" >&2; exit 1; fi
printf 'Gateway passphrase: '
stty -echo 2>/dev/null || true
trap 'stty echo 2>/dev/null || true' EXIT INT TERM
IFS= read -r PASS
stty echo 2>/dev/null || true
echo
printf '%s\n' "$PASS" | $E exec -i "$NAME" python -m app.unlock_cli
