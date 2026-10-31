"""Secrets and model config: fail loudly, never leak."""
from __future__ import annotations

import pytest

from eval import settings


def test_judge_family_differs_from_agents():
    cfg = settings.models()
    assert not cfg["judge"]["model"].startswith("openai/")


def test_missing_key_raises_without_value(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / ".env")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        settings.get_secret("OPENROUTER_API_KEY")


def test_key_read_from_env_file(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("# comment\nOPENROUTER_API_KEY='sk-test-123'\n")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", env)
    assert settings.get_secret("OPENROUTER_API_KEY") == "sk-test-123"


def test_private_dir_inside_repo_is_refused(monkeypatch):
    monkeypatch.setenv("EVAL_PRIVATE_DIR", str(settings.EVAL_DIR / "heldout"))
    with pytest.raises(RuntimeError, match="outside the repo"):
        settings.private_dir()
