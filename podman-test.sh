#!/usr/bin/env sh
# Run the test suite in the dev image with 4 xdist workers. Extra args go to pytest, e.g. ./podman-test.sh -k unlock
# Live tests: ./podman-test.sh -m live (needs LIVE_* variables exported).
set -e
cd "$(dirname "$0")"
podman build -q -f Containerfile.dev -t localhost/gateway-dev:latest . >/dev/null
exec podman run --rm --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD":/srv:ro,z \
  -e LIVE_TELEGRAM_TOKEN -e LIVE_TELEGRAM_CHAT_ID -e LIVE_TODOIST_TOKEN \
  localhost/gateway-dev:latest python -m pytest -n 4 -q "$@"
