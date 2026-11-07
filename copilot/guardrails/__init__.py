"""Input and output guardrails: plain, unit-tested code. Layer 1 of several.

Text from the shop's own data path (logs, traces, runbooks, alert labels) is data, never
instructions. These filters neutralise instruction-like lines and redact secrets. They are
deliberately simple and will miss clever attacks; permissions, the allow-list and the
approval gate sit behind them, so no single filter has to be perfect (system design 10).
GuardConfig switches each layer off for the E2 "defences off" run.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

import yaml

from mcp_servers.common import redact

REMOVED = "[REMOVED: instruction-like text in untrusted data]"
MAX_STRING = 2000


@dataclass
class GuardConfig:
    injection_filter: bool = True    # neutralise instruction-like lines in tool output
    redaction: bool = True           # remove secrets / PII from tool output and alerts
    permissions: bool = True         # per-agent tool permissions in the hooks
    pinning: bool = True             # refuse tools whose server or description is not pinned (config/mcp_allowlist.yaml)
    # Optional model layer (Prompt Guard 2 on tool results, Llama Guard 4 on engineer text): any callable
    # text -> risk score 0..1. Not part of the on/off config; the app sets it. None = regex layer only.
    classifier: Optional[Callable[[str], float]] = None
    classifier_threshold: float = 0.5

    @classmethod
    def all_off(cls) -> "GuardConfig":
        return cls(False, False, False, False)

    @classmethod
    def load(cls, path: Optional[str] = None, env: Optional[Mapping[str, str]] = None) -> "GuardConfig":
        """Defences from config, for experiment E2 (no code change needed to flip them).

        1. `defences:` in config/app.yaml (or the file named by COPILOT_CONFIG / `path`), e.g.
               defences: {injection_filter: true, redaction: true, permissions: true}
        2. COPILOT_DEFENCES overrides it: "off", "on", or "redaction=off,permissions=on".
        Missing file, missing key or an unreadable value means the defence stays ON (fail closed).
        """
        env = os.environ if env is None else env
        values: Dict[str, bool] = {}
        file = path or env.get("COPILOT_CONFIG") or os.path.join("config", "app.yaml")
        try:
            with open(file) as fh:
                section = (yaml.safe_load(fh) or {}).get("defences") or {}
            if isinstance(section, dict):
                for name in _DEFENCES:
                    if name in section:
                        values[name] = _on(section[name])
        except (OSError, yaml.YAMLError, AttributeError):
            pass
        raw = str(env.get("COPILOT_DEFENCES", "")).strip().lower()
        if raw in ("off", "on"):
            values = {name: raw == "on" for name in _DEFENCES}
        elif raw:
            for part in raw.split(","):
                name, _, value = part.partition("=")
                if name.strip() in _DEFENCES:
                    values[name.strip()] = _on(value)
        return cls(**{name: values.get(name, True) for name in _DEFENCES})

    def classify(self, text: str) -> bool:
        """True when the model layer rates this text as an injection. A broken classifier counts as a hit
        (fail closed): the text is withheld rather than trusted."""
        if self.classifier is None or not self.injection_filter or not text.strip():
            return False
        try:
            return float(self.classifier(text)) >= self.classifier_threshold
        except Exception:
            return True


_DEFENCES = ("injection_filter", "redaction", "permissions", "pinning")


def _on(value: Any) -> bool:
    """YAML turns an unquoted `off` into False, so accept real booleans as well as words."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("off", "false", "0", "no"):
        return False
    return True      # "on", "true", anything unreadable: the defence stays on


INJECTION_PATTERNS: List[Tuple[str, "re.Pattern"]] = [
    ("role_prefix", re.compile(r"(?:\bSYSTEM\s*:|\[\s*SYSTEM\s*\]|<\s*/?\s*system\s*>|\bASSISTANT\s*:|\bAI\s+REVIEWER\b)")),
    ("ignore_instructions", re.compile(r"(?i)\b(?:ignore|disregard|forget)\b[^.\n]{0,40}\b(?:instructions|rules|guidelines|prompt)\b")),
    ("mass_action", re.compile(r"(?i)\b(?:restart|stop|kill|delete|shut\s*down|scale|roll\s*back)\b[^.\n]{0,20}\b(?:every|all|each)\b[^.\n]{0,20}\b(?:services?|containers?|servers?|pods?|replicas?)\b")),
    ("fake_approval", re.compile(r"(?i)(?<!not )(?<!never )(?<!no )(?<!n't )\bpre-?approved\b|\bapproved\b[^.\n]{0,25}\b(?:by|from)\b[^.\n]{0,20}\b(?:on-?call|engineer|admin|manager)\b")),
    ("shell_command", re.compile(r"(?i)\b(?:docker\s+(?:exec|run|rm|kill)|kubectl\s+(?:delete|exec)|rm\s+-rf)\b|\bcurl\s+\S+\s*\|\s*(?:sh|bash)\b")),
    ("new_instructions", re.compile(r"(?i)\b(?:new|updated)\s+instructions?\s*:|\byou\s+(?:must|should|are\s+required\s+to)\s+(?:now\s+)?(?:restart|run|execute|call|use|scale|delete)\b")),
    ("hide_from_human", re.compile(r"(?i)\bdo\s+not\s+(?:tell|inform|alert|notify)\b[^.\n]{0,20}\b(?:engineer|user|human|operator)\b")),
]


def find_injection(text: str) -> List[str]:
    return [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]


def neutralise(text: str) -> Tuple[str, List[str]]:
    """Replace each line that looks like an instruction. Returns (text, matched pattern names)."""
    flags: List[str] = []
    lines = []
    for line in text.split("\n"):
        hit = find_injection(line)
        if hit:
            flags.extend(hit)
            lines.append(REMOVED)
        else:
            lines.append(line)
    return "\n".join(lines), flags


def sanitize_obj(obj: Any, cfg: Optional[GuardConfig] = None) -> Tuple[Any, List[str]]:
    """Walk a nested result; redact secrets and neutralise instruction-like lines in every string."""
    cfg = cfg or GuardConfig()
    flags: List[str] = []

    def walk(value: Any) -> Any:
        if isinstance(value, str):
            if cfg.injection_filter:
                value, found = neutralise(value)
                flags.extend(found)
                if cfg.classify(value):
                    value = REMOVED
                    flags.append("classifier")
            if cfg.redaction:
                value = redact(value)
            return value
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(obj), flags


def input_guard(payload: Any, cfg: Optional[GuardConfig] = None) -> Dict[str, Any]:
    """Alert payloads and engineer free text: cap string length, redact, neutralise.

    The alert is never dropped (an incident must still be handled); suspicious text is
    neutralised and reported in flags.
    """
    cfg = cfg or GuardConfig()

    def cap(value: Any) -> Any:
        if isinstance(value, str):
            return value[:MAX_STRING]
        if isinstance(value, dict):
            return {k: cap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cap(v) for v in value]
        return value

    clean, flags = sanitize_obj(cap(payload), cfg)
    return {"payload": clean, "flags": sorted(set(flags))}


def check_citations(claims: Iterable[Dict[str, Any]], known_ids: Iterable[str]) -> List[str]:
    """Every claim in a status note must cite an evidence_id the tools actually returned (design section 10).
    claims: [{"text", "evidence_id"}]. Returns flags: uncited_claim:<n>, unknown_evidence:<id>."""
    known = set(known_ids)
    flags: List[str] = []
    for n, claim in enumerate(claims):
        cited = claim.get("evidence_id")
        ids = cited if isinstance(cited, list) else [cited] if cited else []
        if not ids:
            flags.append("uncited_claim:%d" % n)
        for evidence_id in ids:
            if evidence_id not in known:
                flags.append("unknown_evidence:%s" % evidence_id)
    return flags


def output_guard(text: str, cfg: Optional[GuardConfig] = None, action: Optional[Dict[str, Any]] = None,
                 claims: Optional[Iterable[Dict[str, Any]]] = None,
                 known_evidence: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """Check what the agent says: no secrets, any proposed action on the allow-list, and (when the
    claims are given) every claim cites an evidence_id the tools returned."""
    cfg = cfg or GuardConfig()
    flags: List[str] = []
    if cfg.redaction:
        cleaned = redact(text)
        if cleaned != text:
            flags.append("secret_leak")
        text = cleaned
    blocked = False
    if claims is not None:
        bad = check_citations(claims, known_evidence or [])
        flags.extend(bad)
        blocked = blocked or bool(bad)
    if action is not None:
        from mcp_servers.ops_actions.allowlist import validate_action
        problem = validate_action(action)
        if problem:
            flags.append("action_not_allowlisted:%s" % problem[0])
            blocked = True
    return {"text": text, "flags": flags, "blocked": blocked}
