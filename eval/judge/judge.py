"""LLM judge for what code cannot decide: status-note quality and evidence support.

Code decides everything else (diagnosis, actions, recovery, severity, secrets, timeline). The judge:

- uses a model from a different family than the agents (``eval/config/models.yaml``, checked by
  ``eval.settings.models()``), through OpenRouter, at temperature 0;
- rates one run at a time against the anchored rubric in ``rubric.yaml`` (no pairwise comparison,
  so there is no position to be biased by; the rubric tells it not to reward length);
- sees the agent's texts inside an ``<agent_output>`` block marked as data, never instructions;
- replies in a fixed JSON shape, validated with Pydantic. An invalid reply is retried once, then the
  fallback model is tried; if both fail, the result says so and carries no scores (never a guess).

The API key comes from ``eval.settings.get_secret`` and is never printed, logged or stored.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .. import settings
from ..schemas.result import ResultLine

RUBRIC_FILE = Path(__file__).resolve().parent / "rubric.yaml"
DIMENSIONS = ("status_note_quality", "evidence_support")
ATTEMPTS_PER_MODEL = 2


@lru_cache(maxsize=1)
def rubric() -> dict:
    cfg = yaml.safe_load(RUBRIC_FILE.read_text())
    if set(cfg["dimensions"]) != set(DIMENSIONS):
        raise ValueError(f"rubric dimensions must be {DIMENSIONS}")
    for name, dim in cfg["dimensions"].items():
        if sorted(int(k) for k in dim["anchors"]) != [1, 2, 3, 4, 5]:
            raise ValueError(f"rubric dimension {name} needs anchors for 1 to 5")
        if not all(isinstance(v, str) and v.strip() for v in (dim["question"], *dim["anchors"].values())):
            raise ValueError(f"rubric dimension {name}: question and anchors must be plain text")
    # A rule with an unquoted "key: value" inside it would load as a mapping, not text.
    if not all(isinstance(r, str) and r.strip() for r in cfg["rules"]):
        raise ValueError("every rubric rule must be plain text (quote rules that contain a colon)")
    return cfg


# ---------- what the judge rates ----------

class CitedEvidence(BaseModel):
    evidence_id: str
    found: bool                      # False: the note cites an ID the run never recorded
    summary: str = ""
    supports: Optional[bool] = None  # about its own hypothesis (for_hypothesis), not the note's claim
    hypothesis_id: Optional[str] = None
    for_hypothesis: Optional[str] = None   # e.g. "payment/paymentUnreachable (refuted)"


class JudgeItem(BaseModel):
    """One run, reduced to what the judge needs. Built from a result line by ``build_item``."""

    item_id: str                     # the run_id
    run_id: str
    case_id: str
    config_id: str
    facts: dict[str, Any]            # measured by code; the judge treats these as true
    status_note: str
    cited_evidence: list[CitedEvidence] = Field(default_factory=list)


def _top_hypothesis(result: ResultLine) -> Optional[dict]:
    live = [h for h in result.hypotheses if h.status != "refuted"]
    if not live:
        return None
    h = min(live, key=lambda x: x.rank)
    return {"service": h.service, "fault_type": h.fault_type, "status": h.status}


def _hypothesis_label(h) -> str:
    return f"{h.service}/{h.fault_type or '?'} ({h.status})"


def build_item(result: ResultLine) -> Optional[JudgeItem]:
    """``None`` when the run wrote no status note (nothing for the judge to rate)."""
    ex = result.explanation
    if ex is None or not (ex.status_note or "").strip():
        return None
    by_id = {e.evidence_id: e for e in ex.evidence}
    # The adapter keeps Person 2's hypothesis id on each ranked hypothesis (extra field).
    hyps = {getattr(h, "hypothesis_id", None): h for h in result.hypotheses}
    cited = []
    for eid in dict.fromkeys(ex.status_note_evidence_ids):        # keep order, drop repeats
        e = by_id.get(eid)
        h = hyps.get(e.hypothesis_id) if e and e.hypothesis_id else None
        cited.append(CitedEvidence(evidence_id=eid, found=e is not None,
                                   summary=e.summary if e else "", supports=e.supports if e else None,
                                   hypothesis_id=e.hypothesis_id if e else None,
                                   for_hypothesis=_hypothesis_label(h) if h else None))
    rec = result.recovery
    facts = {
        "alerting_service": rec.service,
        "alert_signal": rec.signal,
        "verdict": result.verdict,
        "final_status": result.final_status,
        "independent_recovery_check": ("recovered" if rec.recovered is True else
                                       "not recovered" if rec.recovered is False else "could not be judged"),
        "agent_top_hypothesis": _top_hypothesis(result),
        # Every hypothesis, ranked, with its final status: a note must not present a refuted one as the cause.
        "agent_hypotheses": [_hypothesis_label(h) for h in sorted(result.hypotheses, key=lambda x: x.rank)],
        "actions_executed": [{"tool": c.tool, "args": c.args, "purpose": getattr(c, "purpose", None)}
                             for c in result.tool_calls
                             if c.kind == "write" and c.ok and not c.blocked and c.tool != "dry_run"],
    }
    return JudgeItem(item_id=result.run_id, run_id=result.run_id, case_id=result.case_id,
                     config_id=result.config_id, facts=facts, status_note=ex.status_note.strip(),
                     cited_evidence=cited)


# ---------- prompt ----------

def _anchors_text(cfg: dict) -> str:
    lines = []
    for name in DIMENSIONS:
        dim = cfg["dimensions"][name]
        lines.append(f"## {name}\n{dim['question']}")
        for score in (5, 4, 3, 2, 1):
            lines.append(f"- {score}: {dim['anchors'][score]}")
    return "\n".join(lines)


def build_messages(item: JudgeItem, cfg: Optional[dict] = None) -> list[dict[str, str]]:
    cfg = cfg or rubric()
    agent_output = json.dumps({"status_note": item.status_note,
                               "cited_evidence": [c.model_dump() for c in item.cited_evidence]},
                              indent=2, ensure_ascii=False)
    # A closing tag inside the agent's text must not end the data block early.
    agent_output = agent_output.replace("</agent_output>", "<\\/agent_output>")
    user = "\n\n".join([
        "Rate the status note below using this rubric.",
        _anchors_text(cfg),
        "# Rules\n" + "\n".join(f"- {r}" for r in cfg["rules"]),
        "# FACTS (measured by code)\n" + json.dumps(item.facts, indent=2, default=str),
        "<agent_output>\n" + agent_output + "\n</agent_output>",
        'Reply with JSON only: {"status_note_quality": <1-5>, "evidence_support": <1-5>, '
        '"reason": "<one sentence>"}',
    ])
    return [{"role": "system", "content": cfg["system_prompt"]}, {"role": "user", "content": user}]


def prompt_hash(cfg: Optional[dict] = None) -> str:
    """Identifies the exact rubric text, so results from a changed rubric are never mixed."""
    cfg = cfg or rubric()
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()[:12]


# ---------- reply ----------

class JudgeScores(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status_note_quality: int = Field(ge=1, le=5)
    evidence_support: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1, max_length=400)


RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "judge_scores", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["status_note_quality", "evidence_support", "reason"],
        "properties": {
            "status_note_quality": {"type": "integer", "minimum": 1, "maximum": 5},
            "evidence_support": {"type": "integer", "minimum": 1, "maximum": 5},
            "reason": {"type": "string"},
        }}},
}

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_reply(text: str) -> JudgeScores:
    """Raises ValueError for anything but the exact JSON shape (code fences are tolerated)."""
    body = _FENCE.sub("", (text or "").strip())
    try:
        return JudgeScores.model_validate(json.loads(body))
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"judge reply is not valid: {type(e).__name__}") from None


class JudgeResult(BaseModel):
    item_id: str
    run_id: str
    case_id: str
    rubric_version: str
    prompt_hash: str
    model: Optional[str] = None       # the model whose reply was used; None if every attempt failed
    used_fallback: bool = False
    attempts: int = 0
    status_note_quality: Optional[int] = None
    evidence_support: Optional[int] = None
    reason: Optional[str] = None
    error: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0


# ---------- OpenRouter ----------

class JudgeAPIError(RuntimeError):
    pass


Transport = Callable[[urllib.request.Request, float], dict]


def _urlopen_json(req: urllib.request.Request, timeout_s: float) -> dict:
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        return json.loads(resp.read().decode())


class OpenRouterClient:
    """Minimal chat-completions client (stdlib only). Errors never include the key."""

    def __init__(self, base_url: str, api_key: str, *, timeout_s: float = 60.0,
                 transport: Optional[Transport] = None):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self.timeout_s = timeout_s
        self._transport = transport or _urlopen_json

    def __repr__(self) -> str:                      # never show the key
        return f"OpenRouterClient(url={self.url!r})"

    def chat(self, model: str, messages: list[dict], *, temperature: float, max_tokens: int,
             response_format: Optional[dict] = None) -> tuple[str, dict]:
        body: dict[str, Any] = {"model": model, "messages": messages, "temperature": temperature,
                                "max_tokens": max_tokens}
        if response_format:
            body["response_format"] = response_format
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), method="POST", headers={
            "Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
            "X-Title": "OnCallCopilotG20 eval judge"})
        try:
            data = self._transport(req, self.timeout_s)
        except urllib.error.HTTPError as e:
            raise JudgeAPIError(f"OpenRouter HTTP {e.code} for {model}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise JudgeAPIError(f"OpenRouter unreachable for {model}: {type(e).__name__}") from None
        if "error" in data:
            code = (data.get("error") or {}).get("code")
            raise JudgeAPIError(f"OpenRouter error {code} for {model}")
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise JudgeAPIError(f"OpenRouter reply for {model} has no message") from None
        return text, data.get("usage") or {}


class Judge:
    """Scores JudgeItems. ``client`` defaults to OpenRouter with the course key."""

    def __init__(self, client: Optional[OpenRouterClient] = None, models_cfg: Optional[dict] = None):
        cfg = models_cfg or settings.models()      # settings.models() also checks the allowed list
        self.model = cfg["judge"]["model"]
        self.fallback_model = cfg["judge"]["fallback_model"]
        agents = set(cfg.get("agent_model_families") or [])
        if not agents or {m.split("/")[0] for m in (self.model, self.fallback_model)} & agents:
            raise ValueError(f"judge models must come from a different family than the agents {sorted(agents)}")
        self.temperature = float(cfg["judge"].get("temperature", 0))
        self.max_tokens = int(cfg["judge"].get("max_tokens", 800))
        self.client = client or OpenRouterClient(cfg["provider"]["base_url"],
                                                 settings.get_secret(cfg["provider"]["api_key_env"]))
        self.rubric = rubric()
        self.prompt_hash = prompt_hash(self.rubric)

    def score(self, item: JudgeItem) -> JudgeResult:
        res = JudgeResult(item_id=item.item_id, run_id=item.run_id, case_id=item.case_id,
                          rubric_version=self.rubric["version"], prompt_hash=self.prompt_hash)
        messages = build_messages(item, self.rubric)
        errors: list[str] = []
        for model in (self.model, self.fallback_model):
            for attempt in range(ATTEMPTS_PER_MODEL):
                res.attempts += 1
                # First try with OpenRouter's strict JSON schema; the retry relies on the prompt alone,
                # for models or providers that refuse response_format. The reply is validated either way.
                fmt = RESPONSE_FORMAT if attempt == 0 else None
                try:
                    text, usage = self.client.chat(model, messages, temperature=self.temperature,
                                                   max_tokens=self.max_tokens, response_format=fmt)
                    res.tokens_in += int(usage.get("prompt_tokens") or 0)
                    res.tokens_out += int(usage.get("completion_tokens") or 0)
                    scores = parse_reply(text)
                except (JudgeAPIError, ValueError) as e:
                    errors.append(str(e))
                    continue
                return res.model_copy(update={**scores.model_dump(), "model": model,
                                              "used_fallback": model != self.model, "error": None})
        return res.model_copy(update={"error": "; ".join(errors[-2:])})
