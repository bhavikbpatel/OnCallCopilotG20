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


def test_judge_models_are_allowed_by_course_key():
    cfg = settings.models()
    allowed = set(cfg["allowed_models"])
    assert cfg["judge"]["model"] in allowed and cfg["judge"]["fallback_model"] in allowed


def test_disallowed_judge_model_is_refused(monkeypatch, tmp_path):
    import yaml
    cfg = yaml.safe_load(settings.MODELS_FILE.read_text())
    cfg["judge"]["model"] = "google/gemini-ultra-not-on-list"
    f = tmp_path / "models.yaml"
    f.write_text(yaml.safe_dump(cfg))
    monkeypatch.setattr(settings, "MODELS_FILE", f)
    settings.models.cache_clear()
    try:
        with pytest.raises(ValueError, match="not in allowed_models"):
            settings.models()
    finally:
        settings.models.cache_clear()
