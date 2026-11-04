"""Shared set-up for the demo scripts: run from the repo root with `py scripts/<name>.py`.
Nothing here touches Docker, the shop or the network: the flag store and the executor are in memory."""
import os
import sys

for _stream in (sys.stdout, sys.stderr):          # Windows consoles/pipes default to cp1252; never crash on a character
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("APPROVAL_SECRET", "demo-secret-not-for-production")
os.environ["APPROVAL_STATE_DB"] = ":memory:"          # spent tokens kept in memory: every run starts fresh

from eval.injector import InMemoryFlagStore          # noqa: E402


def say(text: str = "") -> None:
    print(text, flush=True)


def step(title: str) -> None:
    say("\n=== %s ===" % title)


def show(label: str, result: dict) -> None:
    if result.get("ok"):
        say("  %-34s OK" % label)
    else:
        say("  %-34s REFUSED  (%s)" % (label, result["error"]["code"]))


class PrintExecutor:
    """Stands in for Docker: prints what it WOULD have run."""

    def __init__(self):
        self.ran = []

    def restart(self, service):
        self.ran.append(("restart", service))
        say("    >> docker restart %s   (this is the real effect)" % service)

    def scale(self, service, replicas):
        self.ran.append(("scale", service, replicas))
        say("    >> docker scale %s=%d" % (service, replicas))

    def replicas(self, service):
        return 1

    def rollback(self, service, to_version):
        self.ran.append(("rollback", service, to_version))
        say("    >> rollback %s to %s" % (service, to_version))

    def current_version(self, service):
        return "3.1.0"


def flag_store():
    return InMemoryFlagStore({
        "paymentFailure": {"state": "ENABLED", "variants": {"100%": 1, "off": 0}, "defaultVariant": "100%"},
        "cartFailure": {"state": "ENABLED", "variants": {"100%": 1, "off": 0}, "defaultVariant": "off"},
    })
