#!/usr/bin/env sh
# Headless browser (docker variant; not verified) click-through of the admin UI (Playwright) against a fresh, throwaway dev-mode gateway.
# No ports published; the gateway sits on an internal network with no internet. Screenshots: ./ui-check/out/
set -e
cd "$(dirname "$0")"
NET=gateway-uicheck-net
GW=gateway-uicheck
docker build -q -f Containerfile --target prod -t localhost/gateway-uicheck:latest . >/dev/null
docker build -q -f ui-check/Containerfile -t localhost/gateway-uicheck-runner:latest ui-check >/dev/null
cleanup() { docker rm -f "$GW" >/dev/null 2>&1 || true; docker network rm "$NET" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
cleanup
docker network create --internal "$NET" >/dev/null
docker run -d --name "$GW" --network "$NET" --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /data:rw,mode=1777 --tmpfs /run/gateway:rw,mode=1777 \
  -e ENV=development -e API_HOSTS="$GW" -e ADMIN_HOSTS="$GW" -e DATABASE_PATH=/data/gateway.sqlite3 \
  -e DEV_AUTO_UNLOCK_PASSPHRASE=dev-only-passphrase-not-for-production -e DEV_ADMIN_PASSWORD=asdf -e GATEWAY_DEV_PORT=8000 \
  localhost/gateway-uicheck:latest >/dev/null
rm -rf ui-check/out && mkdir -p ui-check/out
docker run --rm --network "$NET" --ipc=host --user "$(id -u):$(id -g)" \
  -v "$PWD/ui-check/check.py":/check/check.py:ro,z -v "$PWD/ui-check/out":/out:z \
  -e BASE="http://$GW:8000" -e OUT=/out -e HOME=/tmp \
  localhost/gateway-uicheck-runner:latest sh -c "for i in \$(seq 1 30); do python -c \"import urllib.request; urllib.request.urlopen('http://$GW:8000/health')\" 2>/dev/null && break; sleep 1; done; python -I /check/check.py"
