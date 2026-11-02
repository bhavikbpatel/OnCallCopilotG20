# One image for all four MCP servers; compose picks the module with `command`.
# The servers import eval.schemas, eval.injector, eval.knowledge and
# eval.telemetry, so eval/ is copied, but never eval/heldout (see .dockerignore).
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml ./
COPY copilot ./copilot
COPY eval ./eval
COPY mcp_servers ./mcp_servers
COPY config ./config
RUN pip install --no-cache-dir ".[serve]"

ENV PYTHONUNBUFFERED=1
