#!/usr/bin/env bash
# One-time setup when a codespace is created. Starting the stack stays a manual
# step (docker compose -f compose.lite.yaml pull --ignore-pull-failures, then
# docker compose -f compose.lite.yaml up -d) so a new codespace does not spend
# time and memory before you need it.
set -euo pipefail

git submodule update --init
pip install --user -e ".[dev,metrics]"

# .env is git-ignored. OPENAI_API_KEY comes from a Codespaces secret (an
# environment variable), so only the approval secret is generated here.
if [ ! -f .env ]; then
  cp .env.example .env
  secret=$(python -c "import secrets; print(secrets.token_hex(32))")
  sed -i "s/^APPROVAL_SECRET=$/APPROVAL_SECRET=${secret}/" .env
  echo "Created .env with a new APPROVAL_SECRET."
fi
