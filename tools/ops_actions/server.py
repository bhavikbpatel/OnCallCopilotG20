"""ops-actions MCP server: the only place that can change the shop.

Tools: dry_run (read) and set_flag / restart_service / scale_service / rollback (write).

A write runs only if ALL of these hold (rule 3: a person approves each exact change):
  1. the action is on the allow-list with typed arguments (allowlist.validate_action);
  2. a dry_run of the same action ran first;
  3. approval_token is a valid, unexpired token for payload_hash(tool, args);
  4. that token has not been used before (single use).
Otherwise it returns {"ok": false, "error": {...}} and changes nothing.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from eval.injector import FlagStoreError, HttpFlagStore
from eval.injector.flagstore import apply as apply_flags
from eval.injector.flagstore import get_variant
from eval.schemas import payload_hash

from tools.approval import check_token
from tools.common import err, marks, ok, run_server
from tools.ops_actions.allowlist import split_action, validate_action
from tools.ops_actions.executor import ComposeExecutor, ExecutorError
from tools.token_store import UsedTokens

SERVER = "ops-actions"


def _flat(tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
    return dict({"tool": tool}, **args)


class OpsActions:
    def __init__(self, store=None, executor=None, secret: Optional[str] = None,
                 journal_path: Optional[str] = None, clock=time.time, used_tokens: Optional[UsedTokens] = None):
        self.store = store or HttpFlagStore()
        self.executor = executor or ComposeExecutor()
        self._secret = secret
        self._journal_path = journal_path
        self._clock = clock
        self._dry_runs = set()        # payload hashes that have been dry-run
        self._used = used_tokens or UsedTokens()   # spent tokens: durable and atomic (tools/token_store.py)
        self.journal = {}             # action_id -> record
        self._n = 0

    # ------------------------------------------------------------- dry run

    def dry_run(self, action: Dict[str, Any]) -> dict:
        """READ. Say what the action would do, without doing it. Mandatory before a write."""
        problem = validate_action(action)
        if problem:
            return err(problem[0], problem[1])
        tool, args = split_action(action)
        try:
            preview = self._preview(tool, args)
        except FlagStoreError as exc:
            return err("flag_store_error", str(exc))
        except ExecutorError as exc:
            return err("executor_error", str(exc))
        if preview.get("error"):
            return err(*preview["error"])
        digest = payload_hash(tool, args)
        self._dry_runs.add(digest)
        data = {"payload_hash": digest, "action": _flat(tool, args)}
        data.update({k: v for k, v in preview.items() if k != "error"})
        return ok(data)

    def _preview(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "set_flag":
            flags = self.store.read()
            if args["flag"] not in flags:
                return {"error": ("unknown_flag", "flag %r does not exist in this shop" % args["flag"])}
            before = get_variant(flags[args["flag"]])
            after = args["variant"]
            undo = None if before == after else _flat("set_flag", {"flag": args["flag"], "variant": before})
            return {
                "would_do": "set flag %s from %s to %s" % (args["flag"], before, after),
                "current_state": {"flag": args["flag"], "variant": before},
                "expected_effect": ("no change: already %s" % after) if before == after else
                                   "flag %s becomes %s through flagd; the fault it controls changes within about a minute" % (args["flag"], after),
                "rollback_plan": "no-op" if undo is None else "set %s back to %s" % (args["flag"], before),
                "rollback_action": undo,
            }
        if tool == "restart_service":
            return {
                "would_do": "restart the %s container" % args["service"],
                "current_state": {"service": args["service"]},
                "expected_effect": "%s is unavailable for a few seconds, then starts clean" % args["service"],
                "rollback_plan": "none needed: a restart changes no configuration",
                "rollback_action": None,
            }
        if tool == "scale_service":
            current = self.executor.replicas(args["service"])
            undo = None if current < 1 or current == args["replicas"] else _flat(
                "scale_service", {"service": args["service"], "replicas": current})
            return {
                "would_do": "scale %s from %d to %d replicas" % (args["service"], current, args["replicas"]),
                "current_state": {"service": args["service"], "replicas": current},
                "expected_effect": "load on %s is spread over %d instances" % (args["service"], args["replicas"]),
                "rollback_plan": "no-op" if undo is None else "scale %s back to %d" % (args["service"], current),
                "rollback_action": undo,
            }
        # rollback(service, to_version)
        current = self.executor.current_version(args["service"])
        undo = None if current == args["to_version"] else _flat(
            "rollback", {"service": args["service"], "to_version": current})
        return {
            "would_do": "redeploy %s at version %s" % (args["service"], args["to_version"]),
            "current_state": {"service": args["service"], "version": current},
            "expected_effect": "%s restarts on version %s" % (args["service"], args["to_version"]),
            "rollback_plan": "no-op" if undo is None else "redeploy %s at %s" % (args["service"], current),
            "rollback_action": undo,
        }

    # ------------------------------------------------------------- writes

    def set_flag(self, flag: str, variant: str, approval_token: str = "") -> dict:
        return self._execute({"tool": "set_flag", "flag": flag, "variant": variant}, approval_token)

    def restart_service(self, service: str, approval_token: str = "") -> dict:
        return self._execute({"tool": "restart_service", "service": service}, approval_token)

    def scale_service(self, service: str, replicas: int, approval_token: str = "") -> dict:
        return self._execute({"tool": "scale_service", "service": service, "replicas": replicas}, approval_token)

    def rollback(self, service: str, to_version: str, approval_token: str = "") -> dict:
        return self._execute({"tool": "rollback", "service": service, "to_version": to_version}, approval_token)

    def _execute(self, action: Dict[str, Any], token: str) -> dict:
        problem = validate_action(action)
        if problem:
            return err(problem[0], problem[1])
        tool, args = split_action(action)
        digest = payload_hash(tool, args)
        code = check_token(digest, token, self._secret, self._clock())
        if code:
            return err(code, "the approval token does not authorise exactly this action")
        if digest not in self._dry_runs:
            return err("dry_run_required", "dry_run this exact action before executing it")
        if not self._used.claim(token):      # atomic: of two racing calls only one gets past here
            return err("approval_already_used", "this approval token was already used")
        try:
            result = self._do(tool, args)
        except FlagStoreError as exc:
            self._used.release(token)        # nothing changed, so the approval is still usable
            return err("flag_store_error", str(exc))
        except ExecutorError as exc:
            self._used.release(token)
            return err("executor_error", str(exc))
        self._dry_runs.discard(digest)
        action_id = self._record(tool, args, digest, result)
        data = {"action_id": action_id, "payload_hash": digest}
        data.update(result)
        return ok(data, action_id=action_id)

    def _do(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if tool == "set_flag":
            before = get_variant(self.store.read()[args["flag"]])
            after = apply_flags(self.store, {args["flag"]: args["variant"]})[args["flag"]]
            return {"flag": args["flag"], "before": before, "after": after}
        if tool == "restart_service":
            self.executor.restart(args["service"])
            return {"service": args["service"], "restarted": True}
        if tool == "scale_service":
            before = self.executor.replicas(args["service"])
            self.executor.scale(args["service"], args["replicas"])
            return {"service": args["service"], "before": before, "after": args["replicas"]}
        before = self.executor.current_version(args["service"])
        self.executor.rollback(args["service"], args["to_version"])
        return {"service": args["service"], "before": before, "after": args["to_version"]}

    def _record(self, tool: str, args: Dict[str, Any], digest: str, result: Dict[str, Any]) -> str:
        self._n += 1
        action_id = "A-%04d" % self._n
        record = {"action_id": action_id, "tool": tool, "args": args, "payload_hash": digest,
                  "result": result, "at": self._clock()}
        self.journal[action_id] = record
        if self._journal_path:
            with open(self._journal_path, "a") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
        return action_id


def build_server(actions: Optional[OpsActions] = None):
    """Build the FastMCP server. Run with:  python -m tools.ops_actions.server"""
    from fastmcp import FastMCP

    ops = actions or OpsActions()
    mcp = FastMCP("ops-actions")

    @mcp.tool(**marks(SERVER, "dry_run"))
    def dry_run(tool: str, flag: str = "", variant: str = "", service: str = "",
                replicas: int = 0, to_version: str = "") -> dict:
        """READ. Preview one action without doing it. tool is set_flag, restart_service,
        scale_service or rollback; pass only that tool's arguments. Returns payload_hash,
        would_do, current_state, expected_effect, rollback_plan and rollback_action."""
        action = {"tool": tool, "flag": flag, "variant": variant, "service": service,
                  "replicas": replicas or None, "to_version": to_version}
        return ops.dry_run({k: v for k, v in action.items() if v not in ("", None)})

    @mcp.tool(**marks(SERVER, "set_flag"))
    def set_flag(flag: str, variant: str, approval_token: str) -> dict:
        """WRITE (needs an approved dry_run). Switch a shop feature flag to a variant ('off' or a valid one)."""
        return ops.set_flag(flag, variant, approval_token)

    @mcp.tool(**marks(SERVER, "restart_service"))
    def restart_service(service: str, approval_token: str) -> dict:
        """WRITE (needs an approved dry_run). Restart one allow-listed service."""
        return ops.restart_service(service, approval_token)

    @mcp.tool(**marks(SERVER, "scale_service"))
    def scale_service(service: str, replicas: int, approval_token: str) -> dict:
        """WRITE (needs an approved dry_run). Scale one allow-listed service to 1-3 replicas."""
        return ops.scale_service(service, replicas, approval_token)

    @mcp.tool(**marks(SERVER, "rollback"))
    def rollback(service: str, to_version: str, approval_token: str) -> dict:
        """WRITE (needs an approved dry_run). Redeploy a service at a known earlier version."""
        return ops.rollback(service, to_version, approval_token)

    return mcp


if __name__ == "__main__":
    run_server(build_server(), "ops_actions")
