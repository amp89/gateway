#!/usr/bin/env sh
# Smoke checks (scripts, pinning, and the running prod stack) from the host, serially. Read-only.
# Uses a local venv (.venv-smoke) with the hash-locked pytest from requirements-dev.lock.
set -e
cd "$(dirname "$0")"
if [ ! -x .venv-smoke/bin/pytest ]; then
  python3 -m venv .venv-smoke
  .venv-smoke/bin/pip install -q --require-hashes --no-deps --only-binary :all: -r requirements-dev.lock
fi
SMOKE_ENGINE=podman exec .venv-smoke/bin/pytest -p no:xdist -p no:cacheprovider -m smoke -q smoke "$@"
