"""The team's Macs run Python 3.9: keep newer runtime features out of eval/."""
from __future__ import annotations

import re
from pathlib import Path

EVAL = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    r"zip\([^)]*strict=": "zip(strict=) is 3.10+",
    r"isinstance\([^)]*\|": "isinstance with X | Y is 3.10+",
    r"^\s*match \S.*:\s*$": "match statement is 3.10+",
    r"slots=True|kw_only=True": "dataclass slots/kw_only is 3.10+",
    r"\b(datetime\.UTC|dt\.UTC)\b": "datetime.UTC is 3.11+",
    r"^\s*import tomllib|from tomllib": "tomllib is 3.11+",
    r"except\*": "except* is 3.11+",
    r"from typing import .*\b(Self|TypeAlias|ParamSpec|TypeGuard)\b": "typing name is 3.10+",
}


def test_no_python_310_plus_features():
    problems = []
    for path in EVAL.rglob("*.py"):
        if path.name == "test_py39_compat.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            for pattern, why in FORBIDDEN.items():
                if re.search(pattern, line):
                    problems.append(f"{path.relative_to(EVAL)}:{n}: {why}")
    assert not problems, "\n".join(problems)


def test_future_annotations_in_modules_using_new_type_syntax():
    # `X | None` and `list[int]` in annotations need this import on 3.9.
    missing = []
    for path in EVAL.rglob("*.py"):
        text = path.read_text()
        uses_union = re.search(r":\s*[\w\[\], ]+\|\s*None|->\s*[\w\[\], ]+\|", text)
        if uses_union and "from __future__ import annotations" not in text:
            missing.append(str(path.relative_to(EVAL)))
    assert not missing, missing
