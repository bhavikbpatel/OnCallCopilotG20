"""Demo 1 (System Design section 9): an approval is for ONE exact action.

Run from the repo root:   py scripts/demo_approval.py
"""
from _demo_common import PrintExecutor, flag_store, say, show, step

from mcp_servers.common.decision import auto_decision, propose, resolve
from mcp_servers.ops_actions.server import OpsActions

ex = PrintExecutor()
ops = OpsActions(store=flag_store(), executor=ex)

step("1. The agent proposes: restart the cart service")
cart = {"tool": "restart_service", "service": "cart"}
show("dry_run(restart cart)", ops.dry_run(cart))
proposal = propose(cart, expected_effect="cart recovers", rollback_plan="none needed")
say("  engineer is shown action: %s" % proposal["action"])
say("  payload hash of that action:  %s..." % proposal["payload_hash"][:20])

step("2. The engineer approves; a token is minted for THAT hash")
outcome = resolve(proposal, auto_decision(proposal, "approve", "on-call-engineer"))
token = outcome["token"]
say("  status: %s   (the model never sees this token)" % outcome["status"])

step("3. Someone edits the action to restart CHECKOUT and replays the old token")
checkout = {"tool": "restart_service", "service": "checkout"}
show("dry_run(restart checkout)", ops.dry_run(checkout))
r = ops.restart_service("checkout", token)
show("restart checkout + old token", r)
say("  Docker ran: %s" % (ex.ran or "nothing"))

step("4. A write with no dry run first")
other = propose({"tool": "scale_service", "service": "ad", "replicas": 2})
other_token = resolve(other, auto_decision(other))["token"]
show("scale ad (never dry-run)", ops.scale_service("ad", 2, other_token))

step("5. The approved action itself runs, once")
show("restart cart + its own token", ops.restart_service("cart", token))
say("  (the dry run is used up, so the engineer's agent dry-runs again, then replays the same token)")
show("dry_run(restart cart) again", ops.dry_run(cart))
show("same token used a second time", ops.restart_service("cart", token))

step("6. No secret configured: nothing can be approved")
import os
saved = os.environ.pop("APPROVAL_SECRET")
bad = resolve(proposal, auto_decision(proposal))
say("  status: %s  reason: %s" % (bad["status"], (bad["reason"] or "")[:60]))
os.environ["APPROVAL_SECRET"] = saved

say("\nWhat actually ran on 'Docker': %s" % ex.ran)
