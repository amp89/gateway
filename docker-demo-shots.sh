#!/usr/bin/env sh
# README screenshots (docker variant; not verified): a throwaway gateway filled with made-up demo data
# (fake upstreams, no internet), shot by Playwright. Never touches gateway-data / gateway-dev-data. Output: ./docs/screenshots/
set -e
cd "$(dirname "$0")"
NET=gateway-demo-net
GW=gateway-demo
PW=demo-admin-password
docker build -q -f Containerfile --target prod -t localhost/gateway-uicheck:latest . >/dev/null
docker build -q -f ui-check/Containerfile -t localhost/gateway-uicheck-runner:latest ui-check >/dev/null
cleanup() { docker rm -f "$GW" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
cleanup
docker network create --internal "$NET" >/dev/null
docker run -d --name "$GW" --network "$NET" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /data:rw,mode=1777 --tmpfs /run/gateway:rw,mode=1777 \
  -v "$PWD/demo/demo_server.py":/demo/demo_server.py:ro,z \
  -e ENV=development -e API_HOSTS="$GW" -e ADMIN_HOSTS="$GW" -e DATABASE_PATH=/data/gateway.sqlite3 \
  -e DEMO_HOST="$GW" -e DEMO_ADMIN_PASSWORD="$PW" \
  localhost/gateway-uicheck:latest python -I /demo/demo_server.py >/dev/null
for i in $(seq 1 30); do docker exec "$GW" test -s /run/gateway/setup.code 2>/dev/null && break; sleep 1; done
CODE=$(docker exec "$GW" cat /run/gateway/setup.code)
mkdir -p docs/screenshots && rm -f docs/screenshots/*.png
shots() {
  docker run --rm --network "$NET" --ipc=host --user "$(id -u):$(id -g)" \
    -v "$PWD/demo/shots.py":/demo/shots.py:ro,z -v "$PWD/docs/screenshots":/out:z \
    -e BASE="http://$GW:8000" -e OUT=/out -e HOME=/tmp -e PHASE="$1" -e SETUP_CODE="$CODE" -e DEMO_ADMIN_PASSWORD="$PW" \
    localhost/gateway-uicheck-runner:latest python -I /demo/shots.py
}
shots setup
for i in $(seq 1 180); do
  docker exec "$GW" test -f /run/gateway/demo.seeded 2>/dev/null && break
  [ "$(docker inspect -f '{{.State.Running}}' "$GW")" = true ] || { docker logs "$GW"; exit 1; }
  sleep 1
done
docker exec "$GW" test -f /run/gateway/demo.seeded || { docker logs "$GW"; echo "demo seeding timed out"; exit 1; }
shots pages
echo "Screenshots in docs/screenshots/"
