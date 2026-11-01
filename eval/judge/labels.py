"""Build the hand-labelling sheet for judge calibration from dev runs.

    python -m eval.judge export --runs eval/results/runs/<batch>/results.jsonl --out eval/results/judge/cal1

Writes two files into ``--out``:

- ``items.jsonl``: one JudgeItem per run (what the judge will see);
- ``labels.csv``: the same runs for a person to label, with empty score columns.

The sheet never shows the judge's scores, so a labeller is not anchored by them. Only dev cases
(``D-``) are allowed: calibration on held-out outputs is refused (capstone guide p. 5; Person 4
guide section 10). Runs that carried a red-team attack, and runs without a status note, are skipped.
An existing ``labels.csv`` is never overwritten, because hand labels cannot be regenerated.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from ..schemas.result import ResultLine
from .judge import DIMENSIONS, JudgeItem, build_item

DEV_PREFIX = "D-"
HUMAN_COLUMNS = tuple(f"human_{d}" for d in DIMENSIONS)
SHEET_COLUMNS = ("item_id", "case_id", "config_id", "final_status", "independent_recovery_check",
                 "agent_hypotheses", "actions_executed", "status_note", "cited_evidence",
                 *HUMAN_COLUMNS, "labeller", "comment")


class NotDevError(ValueError):
    pass


@dataclass
class ExportReport:
    items: int = 0
    skipped_no_note: int = 0
    skipped_attack: int = 0
    skipped_duplicate: int = 0
    files: list[str] = field(default_factory=list)


def read_results(paths: Iterable[Path]) -> list[ResultLine]:
    out = []
    for path in paths:
        for n, line in enumerate(Path(path).read_text().splitlines(), 1):
            if line.strip():
                try:
                    out.append(ResultLine.model_validate_json(line))
                except ValueError as e:
                    raise ValueError(f"{path} line {n} is not a valid result line") from e
    return out


def select_items(results: list[ResultLine], report: ExportReport) -> list[JudgeItem]:
    not_dev = sum(1 for r in results if not r.case_id.startswith(DEV_PREFIX))
    if not_dev:
        # Counts only: never name a held-out case.
        raise NotDevError(f"{not_dev} result line(s) are not dev cases ({DEV_PREFIX}...); "
                          "judge calibration uses dev outputs only")
    items, seen = [], set()
    for r in results:
        if r.attack_id:
            report.skipped_attack += 1
            continue
        if r.run_id in seen:
            report.skipped_duplicate += 1
            continue
        item = build_item(r)
        if item is None:
            report.skipped_no_note += 1
            continue
        seen.add(r.run_id)
        items.append(item)
    report.items = len(items)
    return items


def _cell(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def sheet_row(item: JudgeItem) -> dict[str, str]:
    cited = "\n".join(
        f"{c.evidence_id}: {c.summary}" + ("" if c.found else " [NOT IN RUN]")
        + (f" [for {c.for_hypothesis}" + ("" if c.supports is None else f", supports={str(c.supports).lower()}")
           + "]" if c.for_hypothesis else ("" if c.supports is None else f" (supports={str(c.supports).lower()})"))
        for c in item.cited_evidence) or "(none cited)"
    f = item.facts
    return {"item_id": item.item_id, "case_id": item.case_id, "config_id": item.config_id,
            "final_status": _cell(f.get("final_status")),
            "independent_recovery_check": _cell(f.get("independent_recovery_check")),
            "agent_hypotheses": "\n".join(f.get("agent_hypotheses") or []),
            "actions_executed": _cell(f.get("actions_executed")),
            "status_note": item.status_note, "cited_evidence": cited,
            **{c: "" for c in HUMAN_COLUMNS}, "labeller": "", "comment": ""}


def export(result_files: list[Path], out_dir: Path) -> ExportReport:
    report = ExportReport()
    items = select_items(read_results(result_files), report)
    out_dir.mkdir(parents=True, exist_ok=True)
    labels = out_dir / "labels.csv"
    if labels.exists():
        raise FileExistsError(f"{labels} exists; hand labels are never overwritten. Use a new --out folder.")
    (out_dir / "items.jsonl").write_text("".join(i.model_dump_json() + "\n" for i in items))
    with labels.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS)
        w.writeheader()
        for item in items:
            w.writerow(sheet_row(item))
    report.files = [str(out_dir / "items.jsonl"), str(labels)]
    return report


def read_items(path: Path) -> list[JudgeItem]:
    return [JudgeItem.model_validate_json(x) for x in Path(path).read_text().splitlines() if x.strip()]


def read_labels(path: Path) -> dict[str, dict[str, int]]:
    """item_id -> {dimension: 1..5} for rows a person has labelled. Blank cells are unlabelled."""
    out: dict[str, dict[str, int]] = {}
    with Path(path).open(newline="") as fh:
        for n, row in enumerate(csv.DictReader(fh), 2):          # row 1 is the header
            scores = {}
            for dim, col in zip(DIMENSIONS, HUMAN_COLUMNS):
                raw = (row.get(col) or "").strip()
                if not raw:
                    continue
                if raw not in {"1", "2", "3", "4", "5"}:
                    raise ValueError(f"labels.csv row {n}: {col} must be a whole number 1-5, got {raw!r}")
                scores[dim] = int(raw)
            if scores:
                out[row["item_id"]] = scores
    return out
