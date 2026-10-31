"""Write JSON Schema files for the case and result-line formats.

Run from the repo root:  python -m eval.schemas.export
"""
from __future__ import annotations

import json
from pathlib import Path

from .case import Case
from .result import ResultLine

HERE = Path(__file__).resolve().parent


def main() -> None:
    for name, model in (("case", Case), ("result_line", ResultLine)):
        path = HERE / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n")
        print("wrote", path.relative_to(HERE.parent.parent))


if __name__ == "__main__":
    main()
