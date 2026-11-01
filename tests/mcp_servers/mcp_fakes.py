"""Fakes shared by the mcp_servers tests."""
from __future__ import annotations

from eval.injector import InMemoryFlagStore
from eval.injector.flagstore import get_variant
from mcp_servers.ops_actions.executor import ExecutorError


def flags():
    return {
        "paymentFailure": {"state": "ENABLED", "variants": {"100%": 1, "50%": 0.5, "off": 0}, "defaultVariant": "50%"},
        "cartFailure": {"state": "ENABLED", "variants": {"100%": 1, "off": 0}, "defaultVariant": "off"},
        "emitRawPii": {"state": "ENABLED", "variants": {"on": True, "off": False}, "defaultVariant": "off"},
    }


class FakeExecutor:
    def __init__(self):
        self.restarts, self.scales, self.rollbacks = [], [], []
        self.counts = {"ad": 1, "cart": 1}
        self.version = "3.1.0"
        self.fail = False

    def restart(self, service):
        if self.fail:
            raise ExecutorError("docker down")
        self.restarts.append(service)

    def scale(self, service, replicas):
        self.scales.append((service, replicas))
        self.counts[service] = replicas

    def replicas(self, service):
        return self.counts.get(service, 1)

    def rollback(self, service, to_version):
        self.rollbacks.append((service, to_version))

    def current_version(self, service):
        return self.version
