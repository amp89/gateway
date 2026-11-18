#!/usr/bin/env sh
# README screenshots: a throwaway gateway filled with made-up demo data (fake upstreams, no internet), shot by Playwright.
# Never touches gateway-data / gateway-dev-data. Output: ./docs/screenshots/
set -e
cd "$(dirname "$0")"
NET=gateway-demo-net
GW=gateway-demo
PW=demo-admin-password
podman build -q -f Containerfile --target prod -t localhost/gateway-uicheck:latest . >/dev/null
podman build -q -f ui-check/Containerfile -t localhost/gateway-uicheck-runner:latest ui-check >/dev/null
cleanup() { podman rm -f "$GW" >/dev/null 2>&1 || true; podman network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
cleanup
podman network create --internal "$NET" >/dev/null
podman run -d --name "$GW" --network "$NET" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /data:rw,mode=1777 --tmpfs /run/gateway:rw,mode=1777 \
  -v "$PWD/demo/demo_server.py":/demo/demo_server.py:ro,z \
  -e ENV=development -e API_HOSTS="$GW" -e ADMIN_HOSTS="$GW" -e DATABASE_PATH=/data/gateway.sqlite3 \
  -e DEMO_HOST="$GW" -e DEMO_ADMIN_PASSWORD="$PW" \
  localhost/gateway-uicheck:latest python -I /demo/demo_server.py >/dev/null
for i in $(seq 1 30); do podman exec "$GW" test -s /run/gateway/setup.code 2>/dev/null && break; sleep 1; done
CODE=$(podman exec "$GW" cat /run/gateway/setup.code)
mkdir -p docs/screenshots && rm -f docs/screenshots/*.png
shots() {
  podman run --rm --network "$NET" --ipc=host --user "$(id -u):$(id -g)" --userns=keep-id \
    -v "$PWD/demo/shots.py":/demo/shots.py:ro,z -v "$PWD/docs/screenshots":/out:z \
    -e BASE="http://$GW:8000" -e OUT=/out -e HOME=/tmp -e PHASE="$1" -e SETUP_CODE="$CODE" -e DEMO_ADMIN_PASSWORD="$PW" \
    localhost/gateway-uicheck-runner:latest python -I /demo/shots.py
}
shots setup
for i in $(seq 1 180); do
  podman exec "$GW" test -f /run/gateway/demo.seeded 2>/dev/null && break
  [ "$(podman inspect -f '{{.State.Running}}' "$GW")" = true ] || { podman logs "$GW"; exit 1; }
  sleep 1
done
podman exec "$GW" test -f /run/gateway/demo.seeded || { podman logs "$GW"; echo "demo seeding timed out"; exit 1; }
shots pages
echo "Screenshots in docs/screenshots/"
