"""What ops_actions is allowed to touch. Everything here is code, not model judgement.

Argument names and value rules follow eval.schemas.catalogue (Person 2's action models):
set_flag(flag, variant), restart_service(service), scale_service(service, replicas 1-3),
rollback(service, to_version).
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

from eval.schemas.catalogue import (FAULT_TYPES, SERVICES, SHOP_VERSION, SHOP_WRITE_TOOLS,
                                    check_action_args)

# Only the shop's fault flags can be switched (not emitRawPii, the load flag, etc.).
SETTABLE_FLAGS = frozenset(FAULT_TYPES)

# Never restarted, scaled or rolled back by the agent: the flag service, the load
# generator, and the stateful services whose restart can lose data.
PROTECTED_SERVICES = frozenset({"flagd", "load-generator", "kafka", "astronomy-db", "valkey-cart"})
ACTIONABLE_SERVICES = frozenset(SERVICES) - PROTECTED_SERVICES

CONTROL_KEYS = frozenset({"approval_token", "dry_run", "payload_hash"})


def known_versions() -> frozenset:
    """Versions rollback may target. Set KNOWN_VERSIONS=3.1.0,3.0.0 to widen."""
    raw = os.environ.get("KNOWN_VERSIONS", "")
    versions = {v.strip() for v in raw.split(",") if v.strip()}
    return frozenset(versions or {SHOP_VERSION})


def split_action(action: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """{"tool": "set_flag", "flag": ..} -> ("set_flag", {"flag": ..}); control keys dropped."""
    tool = action.get("tool")
    args = {k: v for k, v in action.items() if k != "tool" and k not in CONTROL_KEYS}
    return tool, args


def validate_action(action: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """None if the action is allowed, else (error_code, message)."""
    if not isinstance(action, dict):
        return "bad_argument", "action must be an object"
    tool, args = split_action(action)
    if tool not in SHOP_WRITE_TOOLS:
        return "unknown_tool", "%r is not an ops action; expected one of %s" % (tool, sorted(SHOP_WRITE_TOOLS))
    try:
        check_action_args(tool, args, complete=True)
        if any(isinstance(v, bool) for v in args.values()):
            raise ValueError("boolean values are not valid arguments")
    except (ValueError, TypeError) as exc:
        return "bad_argument", str(exc)
    if tool == "set_flag" and args["flag"] not in SETTABLE_FLAGS:
        return "flag_not_allowed", "flag %r may not be changed by the copilot" % args["flag"]
    if "service" in args and args["service"] not in ACTIONABLE_SERVICES:
        return "service_not_allowed", "service %r may not be changed by the copilot" % args["service"]
    if tool == "rollback" and args["to_version"] not in known_versions():
        return "version_not_allowed", "to_version %r is not in the known versions %s" % (
            args["to_version"], sorted(known_versions()))
    return None
