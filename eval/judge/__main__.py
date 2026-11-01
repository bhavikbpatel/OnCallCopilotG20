"""Judge calibration commands (run from the repo root, Python 3.12 or newer).

    # 1. Build the labelling sheet from dev runs (refuses held-out results)
    python -m eval.judge export --runs eval/results/runs/<batch>/results.jsonl [more.jsonl ...] \
                                --out eval/results/judge/cal1
    # 2. A person fills human_status_note_quality and human_evidence_support (1-5) in labels.csv,
    #    without looking at judge scores.
    # 3. Score the same items with the judge (calls OpenRouter; key from eval/.env)
    python -m eval.judge score --dir eval/results/judge/cal1
    # 4. Agreement
    python -m eval.judge kappa --dir eval/results/judge/cal1

``score`` appends to ``judge.jsonl`` and skips items already scored with the same rubric, so it can be
re-run after an interruption. ``kappa`` writes ``agreement.json`` and exits 0 only when calibrated.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .kappa import agreement_from_files, format_report, read_judge, write_report
from .labels import NotDevError, export, read_items


def _export(args) -> int:
    try:
        rep = export([Path(p) for p in args.runs], Path(args.out))
    except (NotDevError, FileExistsError, ValueError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"{rep.items} item(s) to label · skipped: {rep.skipped_no_note} without a status note, "
          f"{rep.skipped_attack} red-team run(s), {rep.skipped_duplicate} duplicate run id(s)")
    for f in rep.files:
        print("wrote", f)
    if rep.items < 20:
        print(f"note: calibration needs at least 20 labelled items; this sheet has {rep.items}")
    return 0


def _score(args) -> int:
    from .judge import Judge                       # needs the key only when scoring
    folder = Path(args.dir)
    items = read_items(folder / "items.jsonl")
    out = folder / "judge.jsonl"
    judge = Judge()
    done = {j.item_id for j in read_judge(out) if j.prompt_hash == judge.prompt_hash and j.error is None} \
        if out.exists() else set()
    todo = [i for i in items if i.item_id not in done][: args.limit or None]
    errors = 0
    with out.open("a") as fh:
        for n, item in enumerate(todo, 1):
            res = judge.score(item)
            fh.write(res.model_dump_json() + "\n")
            fh.flush()
            errors += res.error is not None
            print(f"[{n}/{len(todo)}] {item.item_id}: "
                  + (f"error ({res.error})" if res.error else
                     f"note {res.status_note_quality} · evidence {res.evidence_support} · {res.model}"))
    print(f"scored {len(todo) - errors}, errors {errors}, already done {len(done)} -> {out}")
    return 1 if errors else 0


def _kappa(args) -> int:
    folder = Path(args.dir) if args.dir else None
    labels = Path(args.labels) if args.labels else folder / "labels.csv"
    judged = Path(args.judge) if args.judge else folder / "judge.jsonl"
    items = folder / "items.jsonl" if folder else None
    try:
        rep = agreement_from_files(labels, judged, items)
    except (ValueError, OSError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(format_report(rep))
    out = (folder or judged.parent) / "agreement.json"
    write_report(rep, out)
    print("wrote", out)
    return 0 if rep.calibrated else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m eval.judge", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export", help="build items.jsonl and labels.csv from dev result lines")
    e.add_argument("--runs", nargs="+", required=True)
    e.add_argument("--out", required=True)
    s = sub.add_parser("score", help="score items.jsonl with the judge (calls OpenRouter)")
    s.add_argument("--dir", required=True)
    s.add_argument("--limit", type=int, default=0, help="score at most N new items (0 = all)")
    k = sub.add_parser("kappa", help="Cohen's kappa between labels.csv and judge.jsonl")
    k.add_argument("--dir")
    k.add_argument("--labels")
    k.add_argument("--judge")
    args = p.parse_args(argv)
    if args.cmd == "kappa" and not args.dir and not (args.labels and args.judge):
        p.error("kappa needs --dir, or both --labels and --judge")
    return {"export": _export, "score": _score, "kappa": _kappa}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
