"""copilot/tools.py against the telemetry server's answers (mcp_servers/README.md): no LLM, no server."""
import asyncio

import pytest

from copilot import tools
from copilot.tools import McpTelemetry


@pytest.mark.parametrize("values,want", [
    ([[1, "0.31"]], 0.31),                 # Prometheus strings
    ([[1, 0.12]], 0.12),                   # numbers
    ([[1, "0.1"], [2, "0.2"]], 0.2),       # the last reading counts
    ([[1, None]], None),                   # the server sends null for NaN / Inf (0/0, no traffic)
    ([[1, "NaN"]], None),                  # raw NaN string, if ever passed through
    ([[1, "+Inf"]], None),
    ([[1, "n/a"]], None),                  # unreadable: no data, never 0
    ([], None),
    ([5.0], 5.0),                          # a bare value instead of [ts, value]
])
def test_last_value(values, want):
    assert tools._last_value({"values": values}) == want


class FakeToolbox:
    """Answers query_promql the way the telemetry server does: values [[ts, value | null], ...]."""

    def __init__(self, by_query):
        self.by_query = by_query

    async def call(self, qualified, args):
        assert qualified == "telemetry.query_promql"
        kind = "latency" if "histogram_quantile" in args["query"] else "errors"
        return {"series": self.by_query[kind]}


def test_service_health_survives_null_readings():
    """Live-shop integration run, 1 Nov: one null reading crashed service_health with float(None)."""
    tb = FakeToolbox({
        "errors": [{"labels": {"service_name": "payment"}, "values": [[1, "0.31"]]},
                   {"labels": {"service_name": "quote"}, "values": [[1, None]]},       # no traffic
                   {"labels": {"service_name": "flagd"}, "values": [[1, "0.35"]]}],
        "latency": [{"labels": {"service_name": "payment"}, "values": [[1, "120"]]},
                    {"labels": {"service_name": "quote"}, "values": [[1, None]]}],
    })
    health = {h.service: h for h in asyncio.run(McpTelemetry(tb).service_health())}
    assert set(health) == {"payment", "quote"}                  # flagd is never a suspect
    assert health["payment"].error_rate == 0.31 and health["payment"].latency_p95_ms == 120.0
    assert health["quote"].error_rate is None and health["quote"].latency_p95_ms is None   # unknown, never 0
