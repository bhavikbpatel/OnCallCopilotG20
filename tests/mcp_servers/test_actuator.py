"""The actuator sidecar: the only process that runs Docker, reached by ops_actions over four fixed endpoints.

A real HTTP server on a free port, the real ActuatorExecutor client in front of it and the real ComposeExecutor
behind it, with Docker itself replaced by a recording stub. So these tests cover the whole path
ops_actions -> HTTP -> allow-list -> `docker compose` argument list, without Docker."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from mcp_servers.actuator.server import MAX_BODY, Actuator, main, make_server
from mcp_servers.ops_actions.executor import ActuatorExecutor, ComposeExecutor, ExecutorError, make_executor

TOKEN = "actuator-secret"


class Docker:
    """Stands in for subprocess.run: records every command and answers like `docker compose` would."""

    def __init__(self):
        self.calls = []
        self.returncode, self.stdout, self.stderr = 0, "container-1\ncontainer-2\n", ""

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        return SimpleNamespace(returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def start(token=TOKEN):
    docker = Docker()
    executor = ComposeExecutor(demo_dir="/shop", runner=docker)
    server = make_server(Actuator(executor, token=token), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return docker, server, "http://127.0.0.1:%d" % server.server_address[1]


@pytest.fixture
def sidecar():
    docker, server, url = start()
    yield docker, url
    server.shutdown()
    server.server_close()


def raw(url, path, method="GET", body=None, token=TOKEN, headers=None):
    """A request without the client, returning (status, json body)."""
    head = dict(headers or {})
    if token:
        head["Authorization"] = "Bearer " + token
    data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url + path, data=data, method=method, headers=head)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


# ------------------------------------------------------------------ the four operations

def test_restart_scale_rollback_and_status_run_fixed_compose_commands(sidecar):
    docker, url = sidecar
    client = ActuatorExecutor(url, token=TOKEN)
    client.restart("cart")
    client.scale("ad", 2)
    client.rollback("checkout", "3.1.0")
    assert client.replicas("cart") == 2                      # two container ids from `ps -q`
    assert client.current_version("cart")
    cmds = [cmd for cmd, _ in docker.calls]
    assert cmds[0][:2] == ["docker", "compose"] and cmds[0][-2:] == ["restart", "cart"]
    assert cmds[1][-6:] == ["up", "-d", "--no-deps", "--scale", "ad=2", "ad"]
    assert cmds[2][-4:] == ["up", "-d", "--no-deps", "checkout"]
    assert docker.calls[2][1]["env"]["DEMO_VERSION"] == "3.1.0"
    assert cmds[3][-3:] == ["ps", "-q", "cart"]


def test_docker_is_never_run_through_a_shell(sidecar):
    docker, url = sidecar
    ActuatorExecutor(url, token=TOKEN).restart("cart")
    for cmd, kwargs in docker.calls:
        assert isinstance(cmd, list) and all(isinstance(part, str) for part in cmd)
        assert kwargs["shell"] is False


def test_a_docker_failure_comes_back_as_an_executor_error_with_the_reason(sidecar):
    docker, url = sidecar
    docker.returncode, docker.stderr = 1, "no such service: cart"
    with pytest.raises(ExecutorError, match="no such service"):
        ActuatorExecutor(url, token=TOKEN).restart("cart")


def test_without_a_shop_directory_it_says_so_and_runs_nothing():
    server = make_server(Actuator(ComposeExecutor(demo_dir="", runner=Docker()), token=TOKEN), "127.0.0.1", 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        with pytest.raises(ExecutorError, match="OTEL_DEMO_DIR"):
            ActuatorExecutor("http://127.0.0.1:%d" % server.server_address[1], token=TOKEN).restart("cart")
    finally:
        server.shutdown()
        server.server_close()


# ------------------------------------------------------------------ it does not trust its caller

@pytest.mark.parametrize("path,body,why", [
    ("/restart", {"service": "kafka"}, "may not be changed"),                    # protected services
    ("/restart", {"service": "flagd"}, "may not be changed"),
    ("/restart", {"service": "no-such-service"}, "unknown service"),
    ("/scale", {"service": "ad", "replicas": 4}, "replicas"),                    # outside 1 to 3
    ("/scale", {"service": "ad", "replicas": 0}, "replicas"),
    ("/scale", {"service": "ad", "replicas": True}, "boolean"),
    ("/rollback", {"service": "cart", "to_version": "9.9.9"}, "known versions"),
    ("/restart", {"service": "cart", "extra": "--remove-orphans"}, "exactly"),   # no extra fields
    ("/restart", {}, "exactly"),
    ("/restart", ["cart"], "exactly"),
    ("/scale", {"service": "ad"}, "exactly"),
])
def test_bad_requests_are_refused_before_docker_runs(sidecar, path, body, why):
    docker, url = sidecar
    status, payload = raw(url, path, "POST", body)
    assert status == 400 and payload["ok"] is False and why in payload["error"]["message"]
    assert docker.calls == []


def test_the_client_shows_the_actuators_reason_not_just_the_status(sidecar):
    _, url = sidecar
    with pytest.raises(ExecutorError, match="may not be changed"):
        ActuatorExecutor(url, token=TOKEN).restart("kafka")


def test_status_needs_exactly_one_allowed_service(sidecar):
    docker, url = sidecar
    assert raw(url, "/status")[0] == 400
    assert raw(url, "/status?service=cart&service=ad")[0] == 400
    assert raw(url, "/status?service=kafka")[0] == 400
    assert docker.calls == []


def test_not_json_and_oversize_bodies_are_refused(sidecar):
    docker, url = sidecar
    assert raw(url, "/restart", "POST", b"not json", headers={"Content-Type": "application/json"})[0] == 400
    assert raw(url, "/restart", "POST", b"x" * (MAX_BODY + 1))[0] == 413
    assert docker.calls == []


def test_only_the_four_endpoints_and_health_exist(sidecar):
    docker, url = sidecar
    assert raw(url, "/shell", "POST", {"cmd": "ls"})[0] == 404
    assert raw(url, "/restart", "GET")[0] == 405
    assert raw(url, "/status", "POST", {"service": "cart"})[0] == 405
    assert raw(url, "/restart", "DELETE")[0] == 405
    assert raw(url, "/health")[1] == {"ok": True, "service": "actuator"}
    assert docker.calls == []


# ------------------------------------------------------------------ the token

def test_a_missing_or_wrong_token_is_refused_and_health_needs_none(sidecar):
    docker, url = sidecar
    assert raw(url, "/restart", "POST", {"service": "cart"}, token="")[0] == 401
    assert raw(url, "/restart", "POST", {"service": "cart"}, token="wrong")[0] == 401
    assert raw(url, "/status?service=cart", token="")[0] == 401
    assert raw(url, "/health", token="")[0] == 200
    assert docker.calls == []
    with pytest.raises(ExecutorError, match="token"):
        ActuatorExecutor(url, token="").restart("cart")
    ActuatorExecutor(url, token=TOKEN).restart("cart")
    assert len(docker.calls) == 1


def test_the_client_reads_the_token_from_the_environment(monkeypatch):
    monkeypatch.setenv("ACTUATOR_URL", "http://actuator:8105")
    monkeypatch.setenv("ACTUATOR_TOKEN", TOKEN)
    client = make_executor()
    assert isinstance(client, ActuatorExecutor) and client.token == TOKEN


def test_it_will_not_listen_beyond_this_machine_without_a_token():
    with pytest.raises(ValueError, match="ACTUATOR_TOKEN"):
        make_server(Actuator(ComposeExecutor(demo_dir="/shop", runner=Docker()), token=""), "0.0.0.0", 0)
    server = make_server(Actuator(ComposeExecutor(demo_dir="/shop", runner=Docker()), token=TOKEN), "0.0.0.0", 0)
    server.server_close()


def test_main_exits_instead_of_starting_an_open_actuator(monkeypatch, capsys):
    monkeypatch.setenv("ACTUATOR_HOST", "0.0.0.0")
    monkeypatch.delenv("ACTUATOR_TOKEN", raising=False)
    assert main() == 2
    assert "ACTUATOR_TOKEN" in capsys.readouterr().err
