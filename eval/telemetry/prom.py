"""Minimal Prometheus HTTP API client (standard library only).

Used by the probes now and by the independent recovery check later. Prometheus
is published by the shop on http://localhost:9090 (set PROM_URL to override).
"""
from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_PROM_URL = "http://localhost:9090"


class PromError(RuntimeError):
    pass


@dataclass(frozen=True)
class Sample:
    labels: dict = field(default_factory=dict)
    value: float = math.nan

    @property
    def is_nan(self) -> bool:
        return math.isnan(self.value)


def _to_float(raw) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return math.nan


class PromClient:
    def __init__(self, base_url: Optional[str] = None, timeout_s: float = 10.0):
        self.base_url = (base_url or os.environ.get("PROM_URL") or DEFAULT_PROM_URL).rstrip("/")
        self.timeout_s = timeout_s

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        try:
            with urllib.request.urlopen(url, timeout=self.timeout_s) as resp:
                payload = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", "")
            except Exception:
                detail = ""
            raise PromError(f"Prometheus {exc.code} for {path}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise PromError(f"Prometheus not reachable at {self.base_url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PromError(f"Prometheus returned non-JSON for {path}") from exc
        if payload.get("status") != "success":
            raise PromError(f"Prometheus error for {path}: {payload.get('error', payload)}")
        return payload.get("data")

    def query(self, expr: str, at: Optional[float] = None) -> list:
        """Instant query. Returns a list of Sample (one per series; one for scalars)."""
        params = {"query": expr}
        if at is not None:
            params["time"] = f"{at:.3f}"
        data = self._get("/api/v1/query", params)
        kind, result = data.get("resultType"), data.get("result")
        if kind == "vector":
            return [Sample(dict(r.get("metric", {})), _to_float(r["value"][1])) for r in result]
        if kind == "scalar":
            return [Sample({}, _to_float(result[1]))]
        raise PromError(f"unsupported result type {kind!r} for instant query")

    def query_value(self, expr: str, at: Optional[float] = None) -> float:
        """Single number from a query that should return one series; NaN if none."""
        samples = self.query(expr, at)
        if not samples:
            return math.nan
        if len(samples) > 1:
            raise PromError(f"expected one series, got {len(samples)} for: {expr}")
        return samples[0].value

    def label_values(self, label: str) -> list:
        return list(self._get(f"/api/v1/label/{urllib.parse.quote(label)}/values") or [])

    def metric_names(self) -> list:
        return self.label_values("__name__")

    def build_info(self) -> dict:
        return dict(self._get("/api/v1/status/buildinfo") or {})
