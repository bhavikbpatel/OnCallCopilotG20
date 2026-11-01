"""ops_actions: allow-list, dry run, approval binding, single use, expiry, no side effects on refusal."""
from __future__ import annotations

from eval.injector import InMemoryFlagStore
from eval.injector.flagstore import get_variant
from eval.schemas import payload_hash

from mcp_servers.common.approval import make_token
from mcp_servers.ops_actions.executor import ExecutorError
from mcp_servers.ops_actions.server import OpsActions

SECRET = "test-secret"


from mcp_fakes import FakeExecutor, flags  # noqa: E402


def make(clock=None):
    store, ex = InMemoryFlagStore(flags()), FakeExecutor()
    kwargs = {"clock": clock} if clock else {}
    return OpsActions(store=store, executor=ex, secret=SECRET, **kwargs), store, ex


def approve(action):
    tool = action["tool"]
    args = {k: v for k, v in action.items() if k != "tool"}
    return make_token(payload_hash(tool, args), secret=SECRET)


def code(result):
    return result["error"]["code"]


FIX = {"tool": "set_flag", "flag": "paymentFailure", "variant": "off"}


def test_dry_run_returns_hash_and_a_rollback_action():
    ops, store, _ = make()
    r = ops.dry_run(FIX)
    assert r["ok"]
    assert r["result"]["payload_hash"] == payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})
    assert r["result"]["current_state"] == {"flag": "paymentFailure", "variant": "50%"}
    assert r["result"]["rollback_action"] == {"tool": "set_flag", "flag": "paymentFailure", "variant": "50%"}
    assert store.writes == []                       # a dry run changes nothing


def test_dry_run_of_a_noop_has_no_rollback():
    ops, _, _ = make()
    r = ops.dry_run({"tool": "set_flag", "flag": "cartFailure", "variant": "off"})
    assert r["ok"] and r["result"]["rollback_action"] is None


def test_allow_list_refusals():
    ops, _, _ = make()
    assert code(ops.dry_run({"tool": "bash", "cmd": "ls"})) == "unknown_tool"
    assert code(ops.dry_run({"tool": "set_flag", "flag": "emitRawPii", "variant": "on"})) == "flag_not_allowed"
    assert code(ops.dry_run({"tool": "set_flag", "flag": "paymentFailure", "variant": "60%"})) == "bad_argument"
    assert code(ops.dry_run({"tool": "set_flag", "flag": "paymentFailure", "value": "off"})) == "bad_argument"
    assert code(ops.dry_run({"tool": "restart_service", "service": "flagd"})) == "service_not_allowed"
    assert code(ops.dry_run({"tool": "restart_service", "service": "kafka"})) == "service_not_allowed"
    assert code(ops.dry_run({"tool": "restart_service", "service": "all"})) == "bad_argument"
    assert code(ops.dry_run({"tool": "scale_service", "service": "ad", "replicas": 100})) == "bad_argument"
    assert code(ops.dry_run({"tool": "scale_service", "service": "ad", "replicas": True})) == "bad_argument"
    assert code(ops.dry_run({"tool": "rollback", "service": "payment", "to_version": "0.0.1"})) == "version_not_allowed"
    assert code(ops.dry_run({"tool": "restart_service", "service": "payment", "extra": 1})) == "bad_argument"


def test_dry_run_unknown_flag_in_shop():
    ops, store, _ = make()
    del store._flags["cartFailure"]
    assert code(ops.dry_run({"tool": "set_flag", "flag": "cartFailure", "variant": "off"})) == "unknown_flag"


def test_write_without_dry_run_is_refused():
    ops, store, _ = make()
    r = ops.set_flag("paymentFailure", "off", approve(FIX))
    assert code(r) == "dry_run_required" and store.writes == []


def test_write_without_or_with_forged_token_is_refused():
    ops, store, _ = make()
    ops.dry_run(FIX)
    assert code(ops.set_flag("paymentFailure", "off", "")) == "approval_required"
    forged = payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"})   # the bare hash is not a token
    assert code(ops.set_flag("paymentFailure", "off", forged)) == "approval_hash_mismatch"
    assert code(ops.set_flag("paymentFailure", "off", "tok:9999999999:" + "0" * 64)) == "approval_hash_mismatch"
    assert store.writes == []


def test_happy_path_changes_the_flag_and_returns_action_id():
    ops, store, _ = make()
    ops.dry_run(FIX)
    r = ops.set_flag("paymentFailure", "off", approve(FIX))
    assert r["ok"] and r["action_id"] == "A-0001"
    assert r["result"]["before"] == "50%" and r["result"]["after"] == "off"
    assert get_variant(store.read()["paymentFailure"]) == "off"


def test_token_is_single_use():
    ops, store, _ = make()
    ops.dry_run(FIX)
    token = approve(FIX)
    assert ops.set_flag("paymentFailure", "off", token)["ok"]
    ops.dry_run(FIX)
    assert code(ops.set_flag("paymentFailure", "off", token)) == "approval_already_used"


def test_an_edit_invalidates_the_earlier_approval():
    ops, _, ex = make()
    cart = {"tool": "restart_service", "service": "cart"}
    ops.dry_run(cart)
    ops.dry_run({"tool": "restart_service", "service": "checkout"})
    token = approve(cart)                                          # engineer approved cart ...
    r = ops.restart_service("checkout", token)                     # ... then the command was edited
    assert code(r) == "approval_hash_mismatch" and ex.restarts == []


def test_expired_token_is_refused():
    now = [1000.0]
    ops, store, _ = make(clock=lambda: now[0])
    ops.dry_run(FIX)
    token = make_token(payload_hash("set_flag", {"flag": "paymentFailure", "variant": "off"}),
                       ttl_s=600, secret=SECRET, now=1000.0)
    now[0] = 1000.0 + 601
    assert code(ops.set_flag("paymentFailure", "off", token)) == "approval_expired"
    assert store.writes == []


def test_restart_scale_and_rollback_use_the_executor():
    ops, _, ex = make()
    for action, call in [
        ({"tool": "restart_service", "service": "email"}, lambda t: ops.restart_service("email", t)),
        ({"tool": "scale_service", "service": "ad", "replicas": 2}, lambda t: ops.scale_service("ad", 2, t)),
        ({"tool": "rollback", "service": "payment", "to_version": "3.1.0"}, lambda t: ops.rollback("payment", "3.1.0", t)),
    ]:
        assert ops.dry_run(action)["ok"]
        assert call(approve(action))["ok"]
    assert ex.restarts == ["email"] and ex.scales == [("ad", 2)] and ex.rollbacks == [("payment", "3.1.0")]


def test_scale_dry_run_offers_a_rollback_to_the_current_replicas():
    ops, _, _ = make()
    r = ops.dry_run({"tool": "scale_service", "service": "ad", "replicas": 3})
    assert r["result"]["rollback_action"] == {"tool": "scale_service", "service": "ad", "replicas": 1}


def test_rollback_action_from_dry_run_undoes_a_fix():
    ops, store, _ = make()
    fix = ops.dry_run(FIX)
    ops.set_flag("paymentFailure", "off", approve(FIX))
    undo = fix["result"]["rollback_action"]
    assert ops.dry_run(undo)["ok"]
    assert ops.set_flag("paymentFailure", undo["variant"], approve(undo))["ok"]
    assert get_variant(store.read()["paymentFailure"]) == "50%"


def test_executor_failure_is_an_error_result_and_does_not_burn_the_token():
    ops, _, ex = make()
    action = {"tool": "restart_service", "service": "email"}
    ops.dry_run(action)
    token = approve(action)
    ex.fail = True
    assert code(ops.restart_service("email", token)) == "executor_error"
    ex.fail = False
    assert ops.restart_service("email", token)["ok"]
