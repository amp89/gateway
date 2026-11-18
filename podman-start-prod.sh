#!/usr/bin/env sh
# Build and run the gateway detached on http://127.0.0.1:${GATEWAY_PORT:-38080}, wait until healthy, then offer to unlock.
# Settings live in .env (created from .env.example on first run). Data lives in the gateway-data volume.
set -e
cd "$(dirname "$0")"
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo "Created .env from .env.example. Edit it (API_PREFIX, ADMIN_PREFIX, API_HOSTS, ADMIN_HOSTS), then run this again."
  exit 1
fi
chmod 600 .env
podman compose -f compose.yaml build
podman compose -f compose.yaml down >/dev/null 2>&1
podman compose -f compose.yaml up -d
printf 'Waiting for healthy'
i=0
until podman healthcheck run gateway >/dev/null 2>&1; do
  i=$((i + 1)); [ $i -gt 60 ] && { echo; echo "not healthy after 60s; see: podman logs gateway"; exit 1; }
  printf .; sleep 1
done
echo " ok"
# First run: the gateway wrote a one-time setup code; print a link that fills it in (code after #: not sent).
if CODE=$(podman exec gateway cat /run/gateway/setup.code 2>/dev/null) && [ -n "$CODE" ]; then
  envval() { sed -n "s/^$1=//p" .env | tail -n 1 | tr -d ' '; }
  HOST=$(envval ADMIN_HOSTS); HOST=${HOST%%,*}; PREFIX=$(envval ADMIN_PREFIX); PORT=$(envval GATEWAY_PORT)
  case "$HOST" in
    ""|localhost|127.0.0.1) URL="http://127.0.0.1:${PORT:-38080}" ;;
    *) URL="https://$HOST" ;;
  esac
  echo "Not set up yet. Open this link to set up (fills in the setup code):"
  echo "  $URL/${PREFIX:-admin}/#setup-code=$CODE"
  echo "Setup code: $CODE"
elif [ -t 0 ]; then ./unlock.sh || true; fi
echo "Gateway running (logs: podman logs -f gateway)"
