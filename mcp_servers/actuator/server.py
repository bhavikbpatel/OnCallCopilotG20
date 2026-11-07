"""actuator: the one small process that runs Docker for the copilot (system design, section 6).

`ops_actions` never touches Docker. It calls four fixed HTTP endpoints here, and this process alone
runs `docker compose`:

    POST /restart   {"service": s}
    POST /scale     {"service": s, "replicas": n}
    POST /rollback  {"service": s, "to_version": v}
    GET  /status?service=s   ->  {"ok": true, "replicas": n, "version": v}
    GET  /health             ->  {"ok": true}            (no Docker, no token)

Why it checks again: this is the process that can change the shop, so it does not trust its caller.
Every request goes through the same allow-list as `ops_actions` (service, replica range, known
versions), takes no extra fields, and runs as a fixed argument list (never a shell). A token is
required when it listens on anything but this machine.

Run it on the machine that has the shop's compose project:

    py -m mcp_servers.actuator.server

Settings: OTEL_DEMO_DIR (the shop checkout, as for ops_actions), CAPSTONE_COMPOSE_OVERRIDE,
ACTUATOR_HOST (default 127.0.0.1), ACTUATOR_PORT (default 8105), ACTUATOR_TOKEN (sent by
ops_actions as `Authorization: Bearer <token>`).
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from mcp_servers.ops_actions.allowlist import validate_action
from mcp_servers.ops_actions.executor import ComposeExecutor, ExecutorError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8105
MAX_BODY = 4096                      # a request is a few short fields; anything bigger is refused
LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})

# endpoint -> (ops tool the request is checked as, the exact fields it takes)
POST_ROUTES: Dict[str, Tuple[str, Tuple[str, ...]]] = {
    "/restart": ("restart_service", ("service",)),
    "/scale": ("scale_service", ("service", "replicas")),
    "/rollback": ("rollback", ("service", "to_version")),
}

Reply = Tuple[int, Dict[str, Any]]


def _fail(status: int, code: str, message: str) -> Reply:
    return status, {"ok": False, "error": {"code": code, "message": message}}


class Actuator:
    """The request logic, separate from the HTTP plumbing so it can be tested directly."""

    def __init__(self, executor: Any = None, token: Optional[str] = None):
        self.executor = executor or ComposeExecutor()
        self.token = token or ""
        self._lock = threading.Lock()          # one Docker command at a time

    def authorized(self, header: Optional[str]) -> bool:
        if not self.token:
            return True
        prefix = "Bearer "
        if not header or not header.startswith(prefix):
            return False
        return hmac.compare_digest(header[len(prefix):].encode(), self.token.encode())

    def handle(self, method: str, target: str, body: Optional[bytes], auth: Optional[str]) -> Reply:
        parsed = urlparse(target)
        path = parsed.path
        if path == "/health":
            return (200, {"ok": True, "service": "actuator"}) if method == "GET" else _fail(405, "method_not_allowed", method)
        if path not in POST_ROUTES and path != "/status":
            return _fail(404, "not_found", "unknown path")
        if not self.authorized(auth):
            return _fail(401, "unauthorized", "missing or wrong actuator token")
        if path == "/status":
            return self._status(parsed.query) if method == "GET" else _fail(405, "method_not_allowed", method)
        if method != "POST":
            return _fail(405, "method_not_allowed", method)
        return self._act(path, body)

    def _status(self, query: str) -> Reply:
        values = parse_qs(query).get("service", [])
        if len(values) != 1:
            return _fail(400, "bad_argument", "give exactly one ?service=")
        problem = validate_action({"tool": "restart_service", "service": values[0]})
        if problem:
            return _fail(400, *problem)
        try:
            with self._lock:
                return 200, {"ok": True, "replicas": self.executor.replicas(values[0]),
                             "version": self.executor.current_version(values[0])}
        except ExecutorError as exc:
            return _fail(502, "executor_error", str(exc))

    def _act(self, path: str, raw: Optional[bytes]) -> Reply:
        tool, fields = POST_ROUTES[path]
        try:
            args = json.loads((raw or b"").decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return _fail(400, "bad_argument", "the body must be JSON")
        if not isinstance(args, dict) or set(args) != set(fields):
            return _fail(400, "bad_argument", "the body must be an object with exactly: %s" % ", ".join(fields))
        problem = validate_action(dict({"tool": tool}, **args))
        if problem:
            return _fail(400, *problem)
        try:
            with self._lock:
                if path == "/restart":
                    self.executor.restart(args["service"])
                elif path == "/scale":
                    self.executor.scale(args["service"], args["replicas"])
                else:
                    self.executor.rollback(args["service"], args["to_version"])
        except ExecutorError as exc:
            return _fail(502, "executor_error", str(exc))
        return 200, {"ok": True}


def make_server(actuator: Actuator, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    if host not in LOOPBACK and not actuator.token:
        raise ValueError("ACTUATOR_TOKEN is required when listening on %r (anything but this machine)" % host)

    class Handler(BaseHTTPRequestHandler):
        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                status, payload = _fail(413, "too_large", "the body is limited to %d bytes" % MAX_BODY)
            else:
                body = self.rfile.read(length) if length else None
                status, payload = actuator.handle(self.command, self.path, body, self.headers.get("Authorization"))
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _serve

        def log_message(self, fmt: str, *args: Any) -> None:       # method, path and status only; never the token
            sys.stderr.write("actuator %s %s\n" % (self.address_string(), fmt % args))

    return ThreadingHTTPServer((host, port), Handler)


def main() -> int:
    host = os.environ.get("ACTUATOR_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ACTUATOR_PORT", str(DEFAULT_PORT)))
    try:
        server = make_server(Actuator(token=os.environ.get("ACTUATOR_TOKEN")), host, port)
    except ValueError as exc:
        sys.stderr.write("actuator: %s\n" % exc)
        return 2
    if not os.environ.get("OTEL_DEMO_DIR"):
        sys.stderr.write("actuator: OTEL_DEMO_DIR is not set, so restart/scale/rollback will fail until it is\n")
    sys.stderr.write("actuator listening on http://%s:%d\n" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
