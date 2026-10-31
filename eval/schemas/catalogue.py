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
SHOP_WRITE_TOOLS: frozenset[str] = frozenset({"set_flag", "restart", "scale", "rollback"})
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
