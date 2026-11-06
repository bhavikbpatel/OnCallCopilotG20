#!/usr/bin/env bash
# Start the Astronomy Shop (full mode) with our capstone override.
# Usage: shop/astronomy/start.sh        (from anywhere)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
DEMO_DIR="${OTEL_DEMO_DIR:-/Volumes/BMac/IITB/Capstone/opentelemetry-demo}"
EXPECTED_TAG="3.1.0"

cd "$DEMO_DIR"
TAG="$(git describe --tags 2>/dev/null || echo unknown)"
if [ "$TAG" != "$EXPECTED_TAG" ]; then
  echo "Demo is at '$TAG', expected '$EXPECTED_TAG'. Run: git checkout $EXPECTED_TAG" >&2
  exit 1
fi

docker compose --env-file .env --env-file .env.override --env-file "$HERE/capstone.env" \
  -f compose.yaml -f compose.full.yaml \
  -f compose.observability.yaml -f compose.extras.yaml \
  -f "$HERE/compose.capstone.yaml" \
  up --force-recreate --remove-orphans --detach

echo "Started. Shop: http://localhost:8080/  Flags: http://localhost:8080/feature"
