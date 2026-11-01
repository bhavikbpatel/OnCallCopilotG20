"""How restart / scale / rollback reach Docker, without ever using a shell.

Every command is a fixed argument list built from validated, allow-listed values, so
the model cannot inject flags, pipes or a second command. The system design plans a
small actuator sidecar holding the Docker socket; this class is the same four
operations done directly with `docker compose` (development), behind the same interface.
A sidecar client can replace it later without touching ops-actions.
"""
from __future__ import annotations

import os
import subprocess
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
