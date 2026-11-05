"""Agreement between the judge and hand labels: Cohen's kappa.

    python -m eval.judge kappa --labels eval/results/judge/cal1/labels.csv \
                               --judge eval/results/judge/cal1/judge.jsonl

For each dimension it reports Cohen's kappa **quadratic-weighted** (the headline: the scale is
ordinal, so a 4-vs-5 disagreement counts less than 1-vs-5) and **unweighted** (stricter), exact and
within-one agreement, the confusion matrix (rows = human, columns = judge) and a verbosity check
(correlation of note length with each rater's score).

Reading (Landis & Koch 1977; lecture AAI-13; Evaluation Criteria): below 0.4 means the rubric is
rewritten; 0.6 is the target; 0.8 the aim. The judge counts as calibrated only with at least 20
labelled items and a quadratic-weighted kappa of at least 0.6 on every dimension.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Literal, Optional, Sequence

from pydantic import BaseModel

from .judge import DIMENSIONS, JudgeResult
from .labels import read_items, read_labels

SCALE = (1, 2, 3, 4, 5)
MIN_LABELS = 20
TARGET = 0.6
AIM = 0.8
REWRITE_BELOW = 0.4


def cohen_kappa(a: Sequence[int], b: Sequence[int], categories: Sequence[int] = SCALE,
                weights: Optional[Literal["quadratic"]] = None) -> Optional[float]:
    """Cohen's kappa of two raters over the same items; ``None`` when it is undefined
    (no items, or both raters' marginals leave no room for chance disagreement)."""
    if len(a) != len(b):
        raise ValueError("both raters must rate the same items")
    if not a:
        return None
    idx = {c: i for i, c in enumerate(categories)}
    if any(x not in idx for x in (*a, *b)):
        raise ValueError(f"ratings must be in {list(categories)}")
    k, n = len(categories), len(a)
    obs = [[0.0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[idx[x]][idx[y]] += 1 / n
    row = [sum(r) for r in obs]
    col = [sum(obs[i][j] for i in range(k)) for j in range(k)]

    def w(i: int, j: int) -> float:
        if weights == "quadratic":
            return ((i - j) / (k - 1)) ** 2
        return 0.0 if i == j else 1.0

    observed = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k))
    expected = sum(w(i, j) * row[i] * col[j] for i in range(k) for j in range(k))
    if expected == 0:
        return None
    return 1 - observed / expected


def reading(kappa: Optional[float]) -> str:
    if kappa is None:
        return "undefined (too little variation in the scores)"
    if kappa < REWRITE_BELOW:
        return "poor: rewrite the rubric and re-label"
    if kappa < TARGET:
        return "moderate: below the 0.6 target"
    if kappa < AIM:
        return "substantial: meets the 0.6 target"
    return "almost perfect: meets the 0.8 aim"


def pearson(x: Sequence[float], y: Sequence[float]) -> Optional[float]:
    if len(x) < 3:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx == 0 or sy == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


class DimensionAgreement(BaseModel):
    dimension: str
    n: int
    kappa_quadratic: Optional[float]
    kappa_unweighted: Optional[float]
    reading: str
    exact_agreement: Optional[float]
    within_one: Optional[float]
    mean_human: Optional[float]
    mean_judge: Optional[float]
    confusion: list[list[int]]            # rows = human 1..5, columns = judge 1..5
    length_corr_judge: Optional[float]    # verbosity check: note length vs judge score
    length_corr_human: Optional[float]


class AgreementReport(BaseModel):
    rubric_version: Optional[str]
    prompt_hash: Optional[str]
    models: dict[str, int]                # model -> items it scored (fallback use shows here)
    labelled: int
    judged: int
    paired: int
    judge_errors: int
    unjudged_labels: int
    dimensions: list[DimensionAgreement]
    calibrated: bool
    verdict: str


def _r(x: Optional[float]) -> Optional[float]:
    return None if x is None else round(x, 3)


def agreement(human: dict[str, dict[str, int]], judged: list[JudgeResult],
              note_lengths: Optional[dict[str, int]] = None) -> AgreementReport:
    versions = {(j.rubric_version, j.prompt_hash) for j in judged}
    if len(versions) > 1:
        raise ValueError("judge results come from more than one rubric version; score them again with one")
    ok = {j.item_id: j for j in judged if j.error is None}
    models: dict[str, int] = {}
    for j in ok.values():
        models[j.model or "?"] = models.get(j.model or "?", 0) + 1
    dims = []
    for dim in DIMENSIONS:
        pairs = [(h[dim], getattr(ok[i], dim)) for i, h in human.items()
                 if dim in h and i in ok and getattr(ok[i], dim) is not None]
        ids = [i for i, h in human.items() if dim in h and i in ok and getattr(ok[i], dim) is not None]
        hs, js = [p[0] for p in pairs], [p[1] for p in pairs]
        conf = [[0] * len(SCALE) for _ in SCALE]
        for x, y in pairs:
            conf[x - 1][y - 1] += 1
        kq = cohen_kappa(hs, js, weights="quadratic")
        lens = [float((note_lengths or {}).get(i, 0)) for i in ids]
        has_len = bool(note_lengths) and len(lens) == len(ids)
        dims.append(DimensionAgreement(
            dimension=dim, n=len(pairs), kappa_quadratic=_r(kq),
            kappa_unweighted=_r(cohen_kappa(hs, js)), reading=reading(kq),
            exact_agreement=_r(sum(x == y for x, y in pairs) / len(pairs)) if pairs else None,
            within_one=_r(sum(abs(x - y) <= 1 for x, y in pairs) / len(pairs)) if pairs else None,
            mean_human=_r(sum(hs) / len(hs)) if hs else None,
            mean_judge=_r(sum(js) / len(js)) if js else None,
            confusion=conf,
            length_corr_judge=_r(pearson(lens, js)) if has_len else None,
            length_corr_human=_r(pearson(lens, hs)) if has_len else None))
    paired = min((d.n for d in dims), default=0)
    calibrated = paired >= MIN_LABELS and all(d.kappa_quadratic is not None and d.kappa_quadratic >= TARGET
                                              for d in dims)
    if paired < MIN_LABELS:
        verdict = f"not calibrated yet: {paired} paired item(s), the minimum is {MIN_LABELS}"
    elif any(d.kappa_quadratic is None or d.kappa_quadratic < REWRITE_BELOW for d in dims):
        verdict = "not calibrated: kappa below 0.4 on a dimension; rewrite the rubric, bump its version, re-label"
    elif not calibrated:
        verdict = "not calibrated: kappa below the 0.6 target on a dimension"
    else:
        verdict = "calibrated: quadratic-weighted kappa meets the 0.6 target on every dimension"
    version = next(iter(versions), (None, None))
    return AgreementReport(
        rubric_version=version[0], prompt_hash=version[1], models=models,
        labelled=len(human), judged=len(judged), paired=paired,
        judge_errors=sum(1 for j in judged if j.error is not None),
        unjudged_labels=sum(1 for i in human if i not in ok),
        dimensions=dims, calibrated=calibrated, verdict=verdict)


def read_judge(path: Path) -> list[JudgeResult]:
    return [JudgeResult.model_validate_json(x) for x in Path(path).read_text().splitlines() if x.strip()]


def agreement_from_files(labels: Path, judge: Path, items: Optional[Path] = None) -> AgreementReport:
    lengths = None
    if items and Path(items).exists():
        lengths = {i.item_id: len(i.status_note) for i in read_items(items)}
    return agreement(read_labels(labels), read_judge(judge), lengths)


def format_report(r: AgreementReport) -> str:
    lines = [f"Judge agreement ({r.rubric_version}, prompt {r.prompt_hash})",
             f"labelled {r.labelled} · judged {r.judged} · paired {r.paired} · judge errors {r.judge_errors}"
             f" · labels without a judge score {r.unjudged_labels}",
             "models: " + (", ".join(f"{m} ×{n}" for m, n in sorted(r.models.items())) or "none")]
    for d in r.dimensions:
        lines += [f"\n{d.dimension}: n={d.n}",
                  f"  kappa quadratic-weighted {d.kappa_quadratic}  ({d.reading})",
                  f"  kappa unweighted         {d.kappa_unweighted}",
                  f"  exact agreement {d.exact_agreement} · within one {d.within_one}"
                  f" · mean human {d.mean_human} · mean judge {d.mean_judge}",
                  f"  note length vs score: judge r={d.length_corr_judge}, human r={d.length_corr_human}",
                  "  confusion (rows human 1-5, columns judge 1-5):"]
        lines += ["    " + " ".join(f"{c:3d}" for c in row) for row in d.confusion]
    lines.append("\n" + r.verdict)
    return "\n".join(lines)


def write_report(r: AgreementReport, out: Path) -> None:
    out.write_text(json.dumps(r.model_dump(), indent=2) + "\n")
