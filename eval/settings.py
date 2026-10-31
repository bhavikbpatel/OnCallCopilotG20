"""Settings for the evaluation harness: model config and secrets.

Secrets come from environment variables, or from ``eval/.env`` (git-ignored).
The API key is never printed, logged or written to results.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

EVAL_DIR = Path(__file__).resolve().parent
ENV_FILE = EVAL_DIR / ".env"
MODELS_FILE = EVAL_DIR / "config" / "models.yaml"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def get_secret(name: str) -> str:
    """Environment variable first, then eval/.env. Raises if missing or empty."""
    value = os.environ.get(name) or _read_env_file(ENV_FILE).get(name, "")
    if not value:
        raise RuntimeError(
            f"{name} is not set. Put it in {ENV_FILE} (copy .env.example) "
            f"or export it in your shell. Never commit it."
        )
    return value


@lru_cache(maxsize=1)
def models() -> dict:
    cfg = yaml.safe_load(MODELS_FILE.read_text())
    judge_family = cfg["judge"]["model"].split("/")[0]
    fallback_family = cfg["judge"]["fallback_model"].split("/")[0]
    agent_families = set(cfg["agent_model_families"])
    if {judge_family, fallback_family} & agent_families:
        raise ValueError(
            f"judge models must come from a different family than the agents {sorted(agent_families)}"
        )
    return cfg


def private_dir() -> Path:
    """Folder holding held-out cases and red-team attacks (outside the repo)."""
    path = Path(get_secret("EVAL_PRIVATE_DIR")).expanduser()
    repo_root = EVAL_DIR.parent
    if repo_root == path or repo_root in path.parents:
        raise RuntimeError("EVAL_PRIVATE_DIR must be outside the repo, so held-out cases are never committed")
    return path
