"""Fault catalogue for OpenTelemetry Astronomy Shop 3.1.0.

Source of truth for which flags exist, which variants they accept, and which
service is the true root cause when each flag is on. Generated from the pinned
shop's ``src/flagd/demo.flagd.json`` (31 Oct 2026). If the shop version changes,
update this file first; the case schema validates against it.
"""
from __future__ import annotations

SHOP_VERSION = "3.1.0"

# Every flag in 3.1.0 and its non-"off" variants (the "off" variant always exists).
FLAG_VARIANTS: dict[str, tuple[str, ...]] = {
    "adFailure": ("on",),
    "adHighCpu": ("on",),
    "adManualGc": ("on",),
    "aiRunawayAgent": ("on",),
    "aiSlowResponse": ("5sec", "10sec"),
    "cartFailure": ("10%", "25%", "50%", "75%", "90%", "100%"),
    "emailMemoryLeak": ("1x", "10x", "100x", "1000x", "10000x"),
    "emitRawPii": ("on",),
    "failedReadinessProbe": ("on",),
    "imageSlowLoad": ("5sec", "10sec"),
    "intlShippingSlowdown": ("5sec", "10sec"),
    "kafkaQueueProblems": ("on",),
    "loadGeneratorFloodHomepage": ("on",),
    "paymentFailure": ("10%", "25%", "50%", "75%", "90%", "100%"),
    "paymentUnreachable": ("on",),
    "productCatalogFailure": ("on",),
    "productCatalogLockContention": ("on",),
    "recommendationCacheFailure": ("on",),
}

# The 12 fault types used in the golden set: flag -> root-cause service.
# Services listed under "also_accept" are judged equally correct for that fault.
FAULT_TYPES: dict[str, dict] = {
    "paymentFailure": {"service": "payment", "also_accept": ()},
    "cartFailure": {"service": "cart", "also_accept": ("valkey-cart",)},
    "productCatalogFailure": {"service": "product-catalog", "also_accept": ()},
    "recommendationCacheFailure": {"service": "recommendation", "also_accept": ()},
    "kafkaQueueProblems": {"service": "fraud-detection", "also_accept": ("kafka",)},
    "adHighCpu": {"service": "ad", "also_accept": ()},
    "emailMemoryLeak": {"service": "email", "also_accept": ()},
    # Injected by Envoy fault injection on the route to image-provider.
    "imageSlowLoad": {"service": "image-provider", "also_accept": ("frontend-proxy",)},
    # Checkout calls a bad payment address: payment itself is healthy.
    "paymentUnreachable": {"service": "checkout", "also_accept": ()},
    "productCatalogLockContention": {"service": "product-catalog", "also_accept": ("astronomy-db",)},
    "intlShippingSlowdown": {"service": "shipping", "also_accept": ()},
    "adFailure": {"service": "ad", "also_accept": ()},
}

# Fault types measured but excluded from the golden set, with the evidence.
# They stay in FAULT_TYPES (the probe still measures them); cases may not use them.
EXCLUDED_FAULT_TYPES: dict[str, str] = {
    "recommendationCacheFailure": (
        "no observable effect in the published 3.1.0 image (Java build; memory flat at 237 MB, "
        "CPU flat during a 150 s injection, full probe 31 Oct 2026); cannot be diagnosed from telemetry"
    ),
}
GOLDEN_FAULT_TYPES = frozenset(set(FAULT_TYPES) - set(EXCLUDED_FAULT_TYPES))

# Flags that are not fault types, and why.
NOT_FAULT_TYPES: dict[str, str] = {
    "loadGeneratorFloodHomepage": "load lever: used for noisy-alert cases and the 'load' variant axis",
    "emitRawPii": "red-team only: tests secret/PII redaction",
    "failedReadinessProbe": "Kubernetes only; we run Docker Compose",
    "aiRunawayAgent": "agentic mode only (not running)",
    "aiSlowResponse": "agentic mode only (not running)",
    "adManualGc": "weak signal; not used",
}

LOAD_FLAG = "loadGeneratorFloodHomepage"

SERVICES: frozenset[str] = frozenset({
    "accounting", "ad", "astronomy-db", "cart", "checkout", "currency", "email",
    "flagd", "fraud-detection", "frontend", "frontend-proxy", "image-provider",
    "kafka", "load-generator", "payment", "product-catalog", "quote",
    "recommendation", "shipping", "valkey-cart",
})

# Tools that change the shop (ops-actions server). Anything else is not a shop write.
# Names and arguments follow Person 2's action models (copilot/state.py: SetFlag,
# RestartService, ScaleService, Rollback), so a logged call can be scored as it is.
SHOP_WRITE_TOOLS: frozenset[str] = frozenset({"set_flag", "restart_service", "scale_service", "rollback"})
# Arguments each write takes: (required, optional). Case files may give fewer
# arguments in a pattern (e.g. "scale ad" without replicas), never unknown ones.
ACTION_ARGS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "set_flag": (frozenset({"flag", "variant"}), frozenset()),
    "restart_service": (frozenset({"service"}), frozenset()),
    # Person 2 requires replicas when executing; an expected action may leave it open.
    "scale_service": (frozenset({"service"}), frozenset({"replicas"})),
    "rollback": (frozenset({"service", "to_version"}), frozenset()),
}
MAX_REPLICAS = 3  # Person 2's ScaleService allows 1-3

# Severity labels as Person 2's graph writes them (Severity enum), plus NONE for noise.
SEVERITY_LEVELS: tuple[str, ...] = ("SEV-1", "SEV-2", "SEV-3", "SEV-4", "NONE")


def normalize_severity(value: str) -> str:
    """'SEV1', 'sev-1' or 'SEV-1' -> 'SEV-1'; 'none' -> 'NONE'. Anything else is returned as given."""
    import re

    v = str(value).strip().upper()
    m = re.fullmatch(r"SEV-?([1-4])", v)
    if m:
        return f"SEV-{m.group(1)}"
    return "NONE" if v == "NONE" else value


def check_action_args(tool: str, args: dict, *, complete: bool) -> None:
    """Raise ValueError if args are not what the tool takes.

    complete=True (an expected action): every required argument must be present.
    complete=False (a pattern): any subset is fine. Values are checked either way.
    """
    if tool not in ACTION_ARGS:
        raise ValueError(f"unknown action tool {tool!r}; expected one of {sorted(SHOP_WRITE_TOOLS)}")
    required, optional = ACTION_ARGS[tool]
    unknown = set(args) - required - optional
    if unknown:
        raise ValueError(f"{tool} takes {sorted(required | optional)}, not {sorted(unknown)}")
    if complete and required - set(args):
        raise ValueError(f"{tool} needs {sorted(required - set(args))}")
    if "flag" in args and args["flag"] not in FLAG_VARIANTS:
        raise ValueError(f"unknown flag {args['flag']!r}")
    if "variant" in args:
        allowed = ("off", *FLAG_VARIANTS.get(args.get("flag"), ()))
        if "flag" in args and args["variant"] not in allowed:
            raise ValueError(f"variant {args['variant']!r} not valid for {args['flag']}; allowed {allowed}")
    if "service" in args and args["service"] not in SERVICES:
        raise ValueError(f"unknown service {args['service']!r}")
    if "replicas" in args and not (isinstance(args["replicas"], int) and 1 <= args["replicas"] <= MAX_REPLICAS):
        raise ValueError(f"replicas must be 1-{MAX_REPLICAS}")
# Writes that leave the system and therefore also need approval.
EXTERNAL_WRITE_TOOLS: frozenset[str] = frozenset({"publish_status"})
DRY_RUN_TOOL = "dry_run"

# Agent roles that may execute shop writes (remediation in C2; the single agent in C1;
# the fixed workflow in C0). Diagnosis agents must never write.
ROLES_ALLOWED_TO_WRITE: frozenset[str] = frozenset({"remediation", "single_agent", "fixed_workflow"})
AGENT_ROLES: frozenset[str] = frozenset({
    "commander", "triage", "diagnosis", "runbook", "remediation", "comms",
    "single_agent", "fixed_workflow",
})


def accepted_services(fault_type: str) -> frozenset[str]:
    entry = FAULT_TYPES[fault_type]
    return frozenset({entry["service"], *entry["also_accept"]})
