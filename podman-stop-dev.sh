#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
podman compose -f compose.dev.yaml down
