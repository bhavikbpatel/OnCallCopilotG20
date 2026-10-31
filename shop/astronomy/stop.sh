#!/usr/bin/env bash
# Stop the Astronomy Shop. Quit Docker Desktop before ejecting BMac.
set -euo pipefail
DEMO_DIR="${OTEL_DEMO_DIR:-/Volumes/BMac/IITB/Capstone/opentelemetry-demo}"
cd "$DEMO_DIR" && make stop
