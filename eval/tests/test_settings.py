"""Secrets and model config: fail loudly, never leak."""
from __future__ import annotations

import pytest

from eval import settings


def test_judge_family_differs_from_agents():
    cfg = settings.models()
    assert not cfg["judge"]["model"].startswith("openai/")


def _isolate(monkeypatch, tmp_path):
    """No key from the shell or from real .env files on this machine."""
    for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(settings, "ENV_FILE", tmp_path / "eval.env")
    monkeypatch.setattr(settings, "ROOT_ENV_FILE", tmp_path / "root.env")


def test_missing_key_raises_without_value(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        settings.get_secret("OPENROUTER_API_KEY")


def test_key_read_from_env_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    env = tmp_path / "eval.env"
    env.write_text("# comment\nOPENROUTER_API_KEY='sk-test-123'\n")
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


# ---------------------------------------------------------------- Person 2's root .env (OPENAI_API_KEY)

def test_openrouter_key_found_as_openai_key_in_root_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "root.env").write_text("OPENAI_API_KEY=sk-or-v1-abc\nOPENAI_BASE_URL=\n")
    assert settings.get_secret("OPENROUTER_API_KEY") == "sk-or-v1-abc"


def test_direct_openai_key_is_not_used_as_openrouter_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "root.env").write_text("OPENAI_API_KEY=sk-proj-direct\n")
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        settings.get_secret("OPENROUTER_API_KEY")


def test_eval_env_wins_over_root_env(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "eval.env").write_text("OPENROUTER_API_KEY=sk-or-eval\n")
    (tmp_path / "root.env").write_text("OPENROUTER_API_KEY=sk-or-root\nOPENAI_API_KEY=sk-or-other\n")
    assert settings.get_secret("OPENROUTER_API_KEY") == "sk-or-eval"


def test_error_never_contains_a_key(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    (tmp_path / "root.env").write_text("OPENAI_API_KEY=sk-proj-secret-value\n")
    with pytest.raises(RuntimeError) as exc:
        settings.get_secret("OPENROUTER_API_KEY")
    assert "secret-value" not in str(exc.value)


def test_person2_agent_models_are_allowed_and_differ_from_judge():
    """config/app.yaml (Person 2) must only use course-allowed models of the agent family."""
    import yaml

    app = settings.EVAL_DIR.parent / "config" / "app.yaml"
    if not app.exists():
        pytest.skip("Person 2's config/app.yaml is not on this branch yet")
    cfg = settings.models()
    models = {m["model"] for m in (yaml.safe_load(app.read_text()).get("models") or {}).values()}
    assert models <= set(cfg["allowed_models"]), models - set(cfg["allowed_models"])
    assert {m.split("/")[0] for m in models} <= set(cfg["agent_model_families"])
