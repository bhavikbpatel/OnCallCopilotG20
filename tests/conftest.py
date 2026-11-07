"""Tests never send traces: a `.env` with LANGSMITH_TRACING=true must not trace test runs (copilot/tracing.py).
load_dotenv does not override variables that are already set, so this wins over `.env`."""
import os

os.environ["LANGSMITH_TRACING"] = "false"
