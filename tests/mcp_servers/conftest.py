"""Make `eval`, `mcp_servers` and `copilot` importable when pytest runs from anywhere, and keep tests
off the real approval state: spent tokens live in memory unless a test asks for a file."""
import os
import sys

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
for path in (ROOT, HERE):      # HERE: shared fakes (mcp_fakes.py)
    if path not in sys.path:
        sys.path.insert(0, path)

os.environ["APPROVAL_STATE_DB"] = ":memory:"
