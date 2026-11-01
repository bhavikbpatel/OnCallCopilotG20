"""Red-team tools.

    python -m eval.adversarial check "$EVAL_PRIVATE_DIR/redteam"     # counts only; never prints payloads
    python -m eval.adversarial probe                                  # does request text reach the traces?

``check`` validates the private attack set and prints counts by category, vector and goal, how
many the harness can deliver today, and that every base case exists. The output is safe to
share: no payloads, titles or IDs.

``probe`` sends a few harmless marker requests to the shop (User-Agent, query string, path) and
then searches Jaeger's recent traces for the marker, to show which HTTP vectors actually land
in what the agent reads. Read-only for the shop apart from those requests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from ..schemas.attack import load_attacks
from ..schemas.case import load_cases
from .deliver import DEFAULT_SHOP_URL

DEV = Path(__file__).resolve().parents[1] / "cases" / "dev"
REPO_ROOT = Path(__file__).resolve().parents[2]
MIN_ATTACKS = 30
PROBE_SERVICES = ("frontend-proxy", "frontend", "product-catalog")


def check(folder: Path, dev: Path = DEV) -> int:
    p = folder.resolve()
    if p == REPO_ROOT or REPO_ROOT in p.parents:
        print("PROBLEM: the attack folder is inside the repo; it must live outside (EVAL_PRIVATE_DIR)")
        return 1
    try:
        attacks = load_attacks(folder)
    except Exception as exc:  # validation messages can quote payloads: report the type only
        print(f"cannot read attacks: {type(exc).__name__} (load one file privately for details)", file=sys.stderr)
        return 2
    dev_ids = {c.case_id for c in load_cases(dev)}
    missing = sorted({a.base_case for a in attacks} - dev_ids)
    for title, counter in (("by category", Counter(a.category for a in attacks)),
                           ("by vector", Counter(a.vector for a in attacks)),
                           ("by goal", Counter(a.goal for a in attacks))):
        print(title)
        for k, v in sorted(counter.items()):
            print(f"  {k:28}{v:>4}")
    deliverable = sum(a.deliverable for a in attacks)
    print(f"\n{len(attacks)} attacks; {deliverable} deliverable by the harness today, "
          f"{len(attacks) - deliverable} waiting for a way in (runbook / tool description / chat)")
    problems = []
    if len(attacks) < MIN_ATTACKS:
        problems.append(f"only {len(attacks)} attacks; need at least {MIN_ATTACKS}")
    if missing:
        problems.append(f"{len(missing)} base case(s) not found in the dev set")
    for x in problems:
        print(f"PROBLEM: {x}")
    return 1 if problems else 0


def _get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def probe(shop_url: str, jaeger_url: str, *, wait_s: float = 90.0, every_s: float = 15.0,
          sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> int:
    marker = f"evalprobe{uuid.uuid4().hex[:12]}"
    sends = {
        "http_header (User-Agent)": urllib.request.Request(shop_url + "/", headers={"User-Agent": marker}),
        "http_query": urllib.request.Request(shop_url + "/api/products?" + urllib.parse.urlencode({"q": marker})),
        "http_path": urllib.request.Request(shop_url + "/api/products/" + marker),
    }
    for name, req in sends.items():
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read(256)
            print(f"sent {name}: {r.status}")
        except urllib.error.HTTPError as e:
            print(f"sent {name}: {e.code}")
        except (urllib.error.URLError, OSError) as e:
            print(f"cannot reach the shop at {shop_url}: {e}", file=sys.stderr)
            return 2
    deadline = clock() + wait_s
    found: dict[str, list[str]] = {}
    while True:
        for svc in PROBE_SERVICES:
            q = urllib.parse.urlencode({"service": svc, "lookback": "15m", "limit": 200})
            try:
                data = _get_json(f"{jaeger_url}/api/traces?{q}")
            except (urllib.error.URLError, OSError, ValueError) as e:
                print(f"cannot query Jaeger at {jaeger_url}: {e}", file=sys.stderr)
                return 2
            for trace in data.get("data") or []:
                for span in trace.get("spans") or []:
                    for tag in span.get("tags") or []:
                        if marker in str(tag.get("value")):
                            found.setdefault(svc, [])
                            key = f"{span.get('operationName')}: {tag.get('key')}"
                            if key not in found[svc]:
                                found[svc].append(key)
        if found or clock() >= deadline:
            break
        sleep(every_s)
    if not found:
        print(f"marker not found in Jaeger traces of {list(PROBE_SERVICES)} within {wait_s:.0f} s")
        return 1
    print("marker found in traces (service -> span: tag):")
    for svc, keys in found.items():
        for k in keys:
            print(f"  {svc:16} {k}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.adversarial", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="validate the private attack set (counts only)")
    c.add_argument("folder")
    pr = sub.add_parser("probe", help="check which HTTP vectors reach the traces")
    pr.add_argument("--shop-url", default=os.environ.get("SHOP_URL", DEFAULT_SHOP_URL))
    pr.add_argument("--jaeger-url", default=os.environ.get("JAEGER_URL", DEFAULT_SHOP_URL + "/jaeger/ui"))
    pr.add_argument("--wait", type=float, default=90.0)
    args = p.parse_args(argv)
    if args.cmd == "check":
        return check(Path(args.folder))
    return probe(args.shop_url.rstrip("/"), args.jaeger_url.rstrip("/"), wait_s=args.wait)


if __name__ == "__main__":
    sys.exit(main())
