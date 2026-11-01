"""Approval tokens: proof that a human approved this exact payload.

token = "tok:<expiry>:<HMAC(secret, payload_hash|expiry)>"

* The gate issues the token when an engineer approves (make_token).
* ops-actions, tickets and the hooks verify it (check_token).
* An agent cannot forge one (it never sees the secret), a token for payload A does not
  verify for payload B (an edit invalidates it), and it expires (default 10 minutes).
* Single use is enforced by the servers that consume tokens, through tools.token_store
  (atomic, and it survives a server restart).

FAILS CLOSED: set APPROVAL_SECRET to the same value in the gate/runner and in every MCP
server. If it is missing nothing is issued and nothing verifies. For local development
only, APPROVAL_DEV_MODE=1 selects a public built-in secret; never use it in a demo or run.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Optional

DEFAULT_TTL_S = 600
_DEV_SECRET = "dev-only-secret-change-me"


class ApprovalConfigError(RuntimeError):
    """APPROVAL_SECRET is not configured, so approvals cannot be issued or checked."""


def _key(secret: Optional[str]) -> bytes:
    value = secret or os.environ.get("APPROVAL_SECRET")
    if value:
        return value.encode()
    if os.environ.get("APPROVAL_DEV_MODE") == "1":
        return _DEV_SECRET.encode()
    raise ApprovalConfigError("APPROVAL_SECRET is not set; set it in the gate and in every MCP server "
                              "(APPROVAL_DEV_MODE=1 allows a public dev secret for local testing only)")


def _mac(payload_hash: str, expiry: int, secret: Optional[str]) -> str:
    return hmac.new(_key(secret), ("%s|%d" % (payload_hash, expiry)).encode(), hashlib.sha256).hexdigest()


def make_token(payload_hash: str, ttl_s: int = DEFAULT_TTL_S, secret: Optional[str] = None,
               now: Optional[float] = None) -> str:
    expiry = int((time.time() if now is None else now) + ttl_s)
    return "tok:%d:%s" % (expiry, _mac(payload_hash, expiry, secret))


def check_token(payload_hash: str, token: object, secret: Optional[str] = None,
                now: Optional[float] = None) -> Optional[str]:
    """None if the token is valid for this payload, else an error code."""
    if not token:
        return "approval_required"
    parts = token.split(":") if isinstance(token, str) else []
    if len(parts) != 3 or parts[0] != "tok" or not parts[1].isdigit():
        return "approval_hash_mismatch"
    expiry = int(parts[1])
    try:
        expected = _mac(payload_hash, expiry, secret)
    except ApprovalConfigError:
        return "approval_not_configured"
    if not hmac.compare_digest(expected, parts[2]):
        return "approval_hash_mismatch"
    if (time.time() if now is None else now) > expiry:
        return "approval_expired"
    return None


def verify_token(payload_hash: str, token: object, secret: Optional[str] = None,
                 now: Optional[float] = None) -> bool:
    return check_token(payload_hash, token, secret, now) is None
