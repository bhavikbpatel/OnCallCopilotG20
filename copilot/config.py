"""App settings, loaded from config/app.yaml (one versioned file; its hash is recorded per run)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from copilot.state import BudgetLimits

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / "config" / "app.yaml"


class ModelConfig(BaseModel):
    model: str
    temperature: float = 0.1


class AppSettings(BaseModel):
    models: dict[str, ModelConfig] = Field(default_factory=lambda: {
        "commander": ModelConfig(model="openai/gpt-6-luna", temperature=0.2),
        "triage": ModelConfig(model="openai/gpt-5-mini"),
        "diagnosis": ModelConfig(model="openai/gpt-5-mini"),
        "runbook": ModelConfig(model="openai/gpt-5-mini"),
        "remediation": ModelConfig(model="openai/gpt-5-mini"),
        "comms": ModelConfig(model="openai/gpt-5-mini"),
    })
    llm_base_url: str = "https://openrouter.ai/api/v1"   # OpenRouter; env OPENAI_BASE_URL overrides
    prompts_dir: Path = ROOT / "config" / "prompts"
    mcp_servers_path: Path = ROOT / "config" / "mcp_servers.yaml"
    permissions_path: Path = ROOT / "config" / "permissions.yaml"
    embedding_model: str | None = None   # e.g. "openai/text-embedding-3-small"; None → incident search by metadata only
    embedding_dims: int | None = 1536
    limits: BudgetLimits = Field(default_factory=BudgetLimits)
    max_replans: int = 2
    max_parallel_diagnosis: int = 3
    verify_window_s: int = 300          # seconds after the fix by which recovery must show (eval within_s)
    verify_window_by_signal: dict[str, int] = Field(default_factory=dict)   # slower signals, e.g. latency 600
    verify_poll_s: int = 30
    verify_initial_delay_s: int = 40    # Prometheus lags 12–40 s behind real time
    verify_settle_s: int = 90           # errors before this point after the fix are ignored (eval settle_s)
    follow_up_signals: list[str] = Field(default_factory=list)   # signals that can need a 2nd step (memory)
    verify_min_requests: int = 5        # fewer requests since the fix → too little traffic to judge
    review_enabled: bool = True         # False on held-out runs: nothing is written to long-term memory
    memory_read_only: bool = False      # True after the playbook is frozen (E3)
    config_hash: str = ""


def load_settings(path: Path | str = DEFAULT_CONFIG) -> AppSettings:
    path = Path(path)
    if not path.exists():
        return AppSettings()
    raw = path.read_bytes()
    data = yaml.safe_load(raw) or {}
    return AppSettings(**data, config_hash=hashlib.sha256(raw).hexdigest()[:12])
