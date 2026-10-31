"""Reproduce the labelled development retrieval comparison.

Run from the repository root with ``python -m eval.knowledge.evaluate``.
The benchmark is development-only; no held-out cases are read.
"""
from __future__ import annotations

import json
from pathlib import Path

from .retrieval import KnowledgeIndex, load_passages

ROOT = Path(__file__).resolve().parent


def recall_at_k(index: KnowledgeIndex, queries: list[dict], k: int, mode: str) -> dict:
    hits = 0
    details = []
    for query in queries:
        results = index.search(
            query["query"], service=query.get("service"), symptom=query.get("symptom"),
            subqueries=query.get("subqueries"), k=k, mode=mode,
        )
        returned = {passage.passage_id for passage in results}
        relevant = set(query["relevant"])
        hit = bool(returned & relevant)
        hits += int(hit)
        details.append({"id": query["id"], "hit": hit,
                        "returned": [p.passage_id for p in results]})
    return {"recall_at_k": hits / len(queries) if queries else 0.0,
            "hits": hits, "total": len(queries), "details": details}


def evaluate() -> dict:
    queries = json.loads((ROOT / "retrieval_queries.json").read_text(encoding="utf-8"))
    if len(queries) < 30:
        raise ValueError("retrieval benchmark requires at least 30 labelled development queries")
    if any(not item["id"].startswith("Q-") or any(doc.startswith("H-") for doc in item["relevant"])
           for item in queries):
        raise ValueError("benchmark must use labelled development queries only")
    index = KnowledgeIndex(load_passages(ROOT))
    return {
        "corpus_passages": len(index.passages),
        "queries": len(queries),
        "k": 5,
        "baseline_bm25": recall_at_k(index, queries, 5, "bm25"),
        "change_hybrid_reranker": recall_at_k(index, queries, 5, "hybrid"),
    }


def main() -> None:
    result = evaluate()
    print(json.dumps({
        "corpus_passages": result["corpus_passages"],
        "queries": result["queries"],
        "Recall@5": {
            "BM25 baseline": result["baseline_bm25"]["recall_at_k"],
            "hybrid + reranker": result["change_hybrid_reranker"]["recall_at_k"],
        },
        "hits": {
            "BM25 baseline": f"{result['baseline_bm25']['hits']}/{result['baseline_bm25']['total']}",
            "hybrid + reranker": (f"{result['change_hybrid_reranker']['hits']}/"
                                   f"{result['change_hybrid_reranker']['total']}"),
        },
    }, indent=2))


if __name__ == "__main__":
    main()
