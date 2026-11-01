"""How restart / scale / rollback reach Docker, without ever using a shell.

Every command is a fixed argument list built from validated, allow-listed values, so
the model cannot inject flags, pipes or a second command. The system design plans a
small actuator sidecar holding the Docker socket; this class is the same four
operations done directly with `docker compose` (development), behind the same interface.
ActuatorExecutor below is the System Design's sidecar client (section 6): ops_actions never holds
the Docker socket; it calls four fixed HTTP endpoints. make_executor() picks one from the environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, List, Optional

from eval.schemas.catalogue import SHOP_VERSION

_DEFAULT_FILES = ["compose.yaml", "compose.full.yaml", "compose.observability.yaml", "compose.extras.yaml"]


class ExecutorError(RuntimeError):
    pass


class ComposeExecutor:
    """Mirrors shop/astronomy/start.sh: same directory, env files and compose files."""

    def __init__(self, demo_dir: Optional[str] = None, compose_files: Optional[List[str]] = None,
                 runner: Callable = subprocess.run, timeout_s: int = 120):
        self.demo_dir = demo_dir or os.environ.get("OTEL_DEMO_DIR")
        files = list(compose_files or _DEFAULT_FILES)
        extra = os.environ.get("CAPSTONE_COMPOSE_OVERRIDE")  # shop/astronomy/compose.capstone.yaml
        if extra and not compose_files:
            files.append(extra)
        self.compose_files = files
        self.version_var = os.environ.get("ROLLBACK_VERSION_VAR", "DEMO_VERSION")
        self._run = runner
        self.timeout_s = timeout_s

    def _compose(self, *args: str, env_extra: Optional[dict] = None) -> str:
        if not self.demo_dir:
            raise ExecutorError("OTEL_DEMO_DIR is not set; cannot reach the shop's compose project")
        cmd = ["docker", "compose", "--env-file", ".env", "--env-file", ".env.override"]
        for name in self.compose_files:
            cmd += ["-f", name]
        cmd += list(args)
        env = dict(os.environ)
        env.update(env_extra or {})
        try:
            done = self._run(cmd, cwd=self.demo_dir, env=env, capture_output=True, text=True,
                             timeout=self.timeout_s, shell=False)
        except FileNotFoundError as exc:
            raise ExecutorError("docker is not installed or not on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ExecutorError("docker compose timed out after %ss" % self.timeout_s) from exc
        if done.returncode != 0:
            raise ExecutorError("docker compose failed: %s" % (done.stderr or done.stdout or "").strip()[:300])
        return done.stdout or ""

    def restart(self, service: str) -> None:
        self._compose("restart", service)

    def scale(self, service: str, replicas: int) -> None:
        self._compose("up", "-d", "--no-deps", "--scale", "%s=%d" % (service, replicas), service)

    def replicas(self, service: str) -> int:
        out = self._compose("ps", "-q", service)
        return len([line for line in out.splitlines() if line.strip()])

    def rollback(self, service: str, to_version: str) -> None:
        # The demo's image tags are built from one version variable; override it for this service only.
        self._compose("up", "-d", "--no-deps", service, env_extra={self.version_var: to_version})

    def current_version(self, service: str) -> str:
        return os.environ.get(self.version_var, SHOP_VERSION)


class ActuatorExecutor:
    """Client for the `actuator` sidecar, which alone holds the Docker socket (system design section 6).

    Four fixed endpoints, JSON in and out, nothing else is ever called:
        POST /restart   {"service": s}
        POST /scale     {"service": s, "replicas": n}
        POST /rollback  {"service": s, "to_version": v}
        GET  /status?service=s  -> {"replicas": n, "version": v}
    A non-2xx answer or an {"ok": false} body is an ExecutorError. The service and version values
    were already checked against the allow-list by ops_actions before they get here.
    """

    def __init__(self, base_url: Optional[str] = None, timeout_s: float = 120.0, opener=None):
        self.base_url = (base_url or os.environ.get("ACTUATOR_URL") or "").rstrip("/")
        self.timeout_s = timeout_s
        self._open = opener or urllib.request.urlopen

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        if not self.base_url:
            raise ExecutorError("ACTUATOR_URL is not set; cannot reach the actuator sidecar")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self._open(req, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            raise ExecutorError("actuator %s %s failed: HTTP %s" % (method, path, exc.code)) from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ExecutorError("actuator %s %s unreachable: %s" % (method, path, exc)) from exc
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise ExecutorError("actuator refused: %s" % str(payload.get("error"))[:300])
        return payload if isinstance(payload, dict) else {}

    def restart(self, service: str) -> None:
        self._call("POST", "/restart", {"service": service})

    def scale(self, service: str, replicas: int) -> None:
        self._call("POST", "/scale", {"service": service, "replicas": int(replicas)})

    def rollback(self, service: str, to_version: str) -> None:
        self._call("POST", "/rollback", {"service": service, "to_version": to_version})

    def replicas(self, service: str) -> int:
        return int(self._call("GET", "/status?service=%s" % urllib.parse.quote(service)).get("replicas", 0))

    def current_version(self, service: str) -> str:
        return str(self._call("GET", "/status?service=%s" % urllib.parse.quote(service)).get("version") or SHOP_VERSION)


def make_executor():
    """ACTUATOR_URL set -> the sidecar client (Docker/Compose); otherwise direct `docker compose` (laptop dev)."""
    return ActuatorExecutor() if os.environ.get("ACTUATOR_URL") else ComposeExecutor()
