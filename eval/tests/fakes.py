"""Test doubles: a fake flag UI server and a fake clock."""
from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "flagd_3.1.0.json"


def real_flags() -> dict:
    """The real 3.1.0 flag definitions, all at rest."""
    return json.loads(FIXTURE.read_text())["flags"]


class FakeFlagUI:
    """Mimics flagd-ui 3.1.0 behind the proxy prefix /feature.

    - GET  /feature/api/read  -> {"flags": {...}}
    - POST /feature/api/write -> {} immediately; applies {"data": {...}} after
      ``apply_delay_s`` (the real one is an asynchronous GenServer cast).
    - ``drop_writes`` simulates a write that never lands.
    """

    def __init__(self, apply_delay_s: float = 0.0, drop_writes: bool = False):
        self.config = {"$schema": "https://flagd.dev/schema/v0/flags.json", "flags": real_flags()}
        self.apply_delay_s = apply_delay_s
        self.drop_writes = drop_writes
        self.write_bodies: list[dict] = []
        self.lock = threading.Lock()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, payload):
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/feature/api/read", "/feature/api/read-file"):
                    with fake.lock:
                        self._send(200, {"flags": copy.deepcopy(fake.config["flags"])})
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self):
                if self.path not in ("/feature/api/write", "/feature/api/write-to-file"):
                    return self._send(404, {"error": "not found"})
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.write_bodies.append(body)
                if not fake.drop_writes:
                    def land(data=body["data"]):
                        with fake.lock:
                            fake.config = copy.deepcopy(data)
                    if fake.apply_delay_s:
                        threading.Timer(fake.apply_delay_s, land).start()
                    else:
                        land()
                self._send(200, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/feature"
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


class FakeTime:
    """Deterministic clock. The injector's waiter advances time instantly.

    ``stop_at`` ends the run (as if the caller stopped it); ``hooks`` maps a time
    to a callback, used to simulate someone else changing flags mid-run.
    """

    def __init__(self, stop_at: float, hooks: dict | None = None):
        self.t = 0.0
        self.stop_at = stop_at
        self.hooks = dict(hooks or {})

    def clock(self) -> float:
        return self.t

    def waiter(self, stop_event, seconds: float) -> bool:
        if stop_event.is_set():
            return True
        target = self.t + max(seconds, 0.0)
        for at in sorted(self.hooks):
            if self.t < at <= target:
                self.t = at
                self.hooks.pop(at)()
        self.t = target
        return self.t >= self.stop_at
