"""Make `eval`, `tools` and `control` importable when pytest runs from anywhere, and keep tests
off the real approval state: spent tokens live in memory unless a test asks for a file."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["APPROVAL_STATE_DB"] = ":memory:"
