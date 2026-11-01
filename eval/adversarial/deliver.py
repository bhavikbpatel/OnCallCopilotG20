"""Deliver HTTP-vector attacks: hostile text inside real requests to the shop.

The requests go only to the shop (frontend-proxy, default http://localhost:8080; env SHOP_URL),
so the text lands in the logs and traces the agent reads during an incident. Responses of any
status count as delivered (a 404 for a hostile path is still logged); only network failures
do not. Standard library only.

Headers cannot carry line breaks or non-Latin-1 text, so such payloads must use the query,
path or body vectors (checked when the attack loads).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from ..schemas.attack import HTTP_VECTORS, Attack

DEFAULT_SHOP_URL = "http://localhost:8080"


class HttpAttackSender:
    def __init__(self, base_url: Optional[str] = None, timeout_s: float = 5.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.base_url = (base_url or os.environ.get("SHOP_URL") or DEFAULT_SHOP_URL).rstrip("/")
        self.timeout_s = timeout_s
        self._sleep = sleep
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.stats = {"sent": 0, "responses": 0, "network_errors": 0}

    def build_request(self, attack: Attack) -> urllib.request.Request:
        if attack.vector not in HTTP_VECTORS:
            raise ValueError(f"{attack.attack_id}: vector {attack.vector} is not delivered over HTTP")
        d, text = attack.delivery, attack.wire_text()
        path = d.path
        headers = {"Accept": "application/json, text/html"}
        data = None
        if attack.vector == "http_path":
            path = path.replace("{payload}", urllib.parse.quote(text, safe=""))
        url = self.base_url + path
        if attack.vector == "http_query":
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode({d.param: text})
        elif attack.vector == "http_header":
            headers[d.header] = text
        elif attack.vector == "http_body":
            data = json.dumps({d.field: text}).encode()
            headers["Content-Type"] = "application/json"
        return urllib.request.Request(url, data=data, method=d.method, headers=headers)

    def send_burst(self, attack: Attack) -> int:
        """Send ``delivery.repeat`` requests; returns how many got any HTTP response."""
        got = 0
        for _ in range(attack.delivery.repeat):
            req = self.build_request(attack)
            self.stats["sent"] += 1
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    resp.read(1024)
                got += 1
            except urllib.error.HTTPError:
                got += 1                       # the shop answered (4xx/5xx): it saw and logged the request
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
                self.stats["network_errors"] += 1
        self.stats["responses"] += got
        return got

    def start(self, attack: Attack) -> None:
        """Send a burst now, then one every ``delivery.every_s`` until stop()."""
        if self._thread is not None:
            raise RuntimeError("sender already running")
        self._stop.clear()
        self.send_burst(attack)

        def loop():
            while not self._stop.wait(attack.delivery.every_s):
                self.send_burst(attack)

        self._thread = threading.Thread(target=loop, daemon=True, name=f"attack-{attack.attack_id}")
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        return dict(self.stats, delivered=self.stats["responses"] > 0)
