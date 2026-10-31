"""Small, deterministic hybrid retrieval baseline for the knowledge corpus.

The interfaces are deliberately local and injectable: a deployment can replace
the TF-IDF vectorizer and reranker with the planned embedding model and
cross-encoder without changing corpus ingestion or the search contract.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ..schemas.case import Action
from ..schemas.catalogue import FLAG_VARIANTS, SERVICES

TOKEN_RE = re.compile(r"[a-z0-9]+")
REQUIRED_METADATA = {
    "doc_id", "kind", "service", "symptom", "fault_family", "fix",
    "author", "written_on", "reviewed_on",
}


@dataclass(frozen=True)
class Passage:
    passage_id: str
    doc_id: str
    section: str
    text: str
    kind: str
    services: tuple[str, ...]
    symptoms: tuple[str, ...]
    fault_family: str
    fix: dict
    title: str


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(text.lower())


def _parse_document(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise ValueError(f"{path}: missing YAML front matter")
    try:
        _, front_matter, body = raw.split("---", 2)
    except ValueError as exc:
        raise ValueError(f"{path}: malformed YAML front matter") from exc
    metadata = yaml.safe_load(front_matter)
    if not isinstance(metadata, dict):
        raise ValueError(f"{path}: front matter must be a mapping")
    missing = REQUIRED_METADATA - set(metadata)
    if missing:
        raise ValueError(f"{path}: missing metadata: {sorted(missing)}")
    if metadata["kind"] not in {"runbook", "postmortem"}:
        raise ValueError(f"{path}: kind must be runbook or postmortem")
    services = metadata["service"]
    symptoms = metadata["symptom"]
    if isinstance(services, str):
        services = [services]
    if isinstance(symptoms, str):
        symptoms = [symptoms]
    if not services or set(services) - SERVICES:
        raise ValueError(f"{path}: unknown or empty service metadata")
    if not symptoms or not all(isinstance(item, str) and item.strip() for item in symptoms):
        raise ValueError(f"{path}: symptom metadata must contain non-empty strings")
    if not isinstance(metadata["fix"], dict):
        raise ValueError(f"{path}: fix must be a typed action mapping")
    # PyYAML's YAML 1.1 resolver reads the unquoted scalar "off" as False.
    args = metadata["fix"].get("args", {})
    if isinstance(args, dict):
        for key in ("value", "variant"):
            if args.get(key) is False:
                args[key] = "off"
    try:
        action = Action.model_validate(metadata["fix"])
        _validate_fix(action.tool, action.args)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{path}: fix is not an allow-listed typed action: {exc}") from exc
    metadata["service"] = services
    metadata["symptom"] = symptoms
    return metadata, body.strip()


def _validate_fix(tool: str, args: dict) -> None:
    if tool == "set_flag":
        flag = args.get("flag")
        value = args.get("value", args.get("variant"))
        if set(args) - {"flag", "value", "variant"} or flag not in FLAG_VARIANTS or value != "off":
            raise ValueError("set_flag must name a catalogue flag and set only its value/variant to 'off'")
    elif tool == "restart":
        if set(args) != {"service"} or args["service"] not in SERVICES:
            raise ValueError("restart requires one allow-listed service")
    elif tool == "scale":
        if (set(args) != {"service", "replicas"} or args["service"] not in SERVICES
                or isinstance(args["replicas"], bool) or args["replicas"] not in {1, 2, 3}):
            raise ValueError("scale requires an allow-listed service and replicas in 1..3")
    elif tool == "rollback":
        if not (set(args) == {"action_id"} and isinstance(args["action_id"], str) and args["action_id"]):
            raise ValueError("rollback requires the ID of a recorded action to undo")
    else:
        raise ValueError(f"unsupported action tool {tool!r}")


def load_passages(directory: str | Path) -> list[Passage]:
    """Parse Markdown documents into metadata-carrying section passages."""
    root = Path(directory)
    passages: list[Passage] = []
    seen: set[str] = set()
    for path in sorted(root.glob("**/*.md")):
        metadata, body = _parse_document(path)
        doc_id = metadata["doc_id"]
        if doc_id in seen:
            raise ValueError(f"duplicate knowledge doc_id {doc_id!r}")
        seen.add(doc_id)
        title = metadata.get("title", doc_id)
        section = "Overview"
        chunks: list[tuple[str, list[str]]] = []
        lines: list[str] = []
        for line in body.splitlines():
            if line.startswith("## "):
                if lines and "\n".join(lines).strip():
                    chunks.append((section, lines))
                section = line[3:].strip()
                lines = []
            else:
                lines.append(line)
        if lines and "\n".join(lines).strip():
            chunks.append((section, lines))
        for heading, contents in chunks:
            text = "\n".join(contents).strip()
            if not text:
                continue
            passages.append(Passage(
                passage_id=f"{doc_id}#{heading.lower().replace(' ', '-')}",
                doc_id=doc_id,
                section=heading,
                text=text,
                kind=metadata["kind"],
                services=tuple(services for services in metadata["service"]),
                symptoms=tuple(metadata["symptom"]),
                fault_family=metadata["fault_family"],
                fix=metadata["fix"],
                title=title,
            ))
    return passages


class KnowledgeIndex:
    """BM25 + local TF-IDF-vector cosine + deterministic reranking."""

    def __init__(self, passages: Iterable[Passage]):
        self.passages = list(passages)
        self._tokens = [_tokens(f"{p.title} {p.section} {p.text}") for p in self.passages]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._avg_length = sum(self._lengths) / max(1, len(self._lengths))
        self._term_freqs = [Counter(tokens) for tokens in self._tokens]
        self._doc_freq: Counter[str] = Counter()
        for terms in self._term_freqs:
            self._doc_freq.update(terms.keys())
        self._vectors = self._build_vectors()

    def _idf(self, term: str) -> float:
        count = self._doc_freq.get(term, 0)
        return math.log(1 + (len(self.passages) - count + 0.5) / (count + 0.5))

    def _build_vectors(self) -> list[dict[str, float]]:
        vectors = []
        for terms in self._term_freqs:
            vector = {term: (1 + math.log(freq)) * self._idf(term) for term, freq in terms.items()}
            norm = math.sqrt(sum(weight * weight for weight in vector.values())) or 1.0
            vectors.append({term: weight / norm for term, weight in vector.items()})
        return vectors

    def _bm25(self, query: str, candidates: set[int]) -> dict[int, float]:
        scores: dict[int, float] = {}
        qterms = _tokens(query)
        for index in candidates:
            score = 0.0
            for term in qterms:
                tf = self._term_freqs[index].get(term, 0)
                if tf:
                    denom = tf + 1.2 * (1 - 0.75 + 0.75 * self._lengths[index] / max(1, self._avg_length))
                    score += self._idf(term) * tf * 2.2 / denom
            scores[index] = score
        return scores

    def _vector(self, query: str, candidates: set[int]) -> dict[int, float]:
        terms = Counter(_tokens(query))
        vector = {term: (1 + math.log(freq)) * self._idf(term) for term, freq in terms.items()}
        norm = math.sqrt(sum(weight * weight for weight in vector.values())) or 1.0
        vector = {term: weight / norm for term, weight in vector.items()}
        return {
            index: sum(weight * self._vectors[index].get(term, 0.0) for term, weight in vector.items())
            for index in candidates
        }

    def _filter(self, service: Optional[str], symptom: Optional[str]) -> set[int]:
        return {
            index for index, passage in enumerate(self.passages)
            if (service is None or service in passage.services)
            and (symptom is None or any(symptom.lower() in item.lower() for item in passage.symptoms))
        }

    @staticmethod
    def _rank(scores: dict[int, float]) -> list[int]:
        return sorted((index for index in scores if scores[index] > 0),
                      key=lambda index: (-scores[index], index))

    def search(self, query: str, service: Optional[str] = None,
               symptom: Optional[str] = None, k: int = 5,
               subqueries: Optional[list[str]] = None,
               mode: str = "hybrid") -> list[Passage]:
        """Search using BM25, vector similarity, RRF and a local reranker.

        ``mode="bm25"`` is the reproducible pre-change baseline.
        """
        if k < 1:
            raise ValueError("k must be positive")
        if service is not None and service not in SERVICES:
            raise ValueError(f"unknown service filter {service!r}")
        if mode not in {"bm25", "hybrid"}:
            raise ValueError(f"unknown retrieval mode {mode!r}")
        candidates = self._filter(service, symptom)
        queries = [query] + (subqueries or [])
        if mode == "bm25":
            scores: dict[int, float] = defaultdict(float)
            for item in queries:
                for rank, index in enumerate(self._rank(self._bm25(item, candidates)), 1):
                    scores[index] += 1 / rank
            ranked = sorted(scores, key=lambda index: (-scores[index], index))
        else:
            fused: dict[int, float] = defaultdict(float)
            for item in queries:
                for component in (self._bm25(item, candidates), self._vector(item, candidates)):
                    for rank, index in enumerate(self._rank(component), 1):
                        fused[index] += 1 / (60 + rank)
            ranked = sorted(
                fused,
                key=lambda index: (-self._rerank(query, self.passages[index]), -fused[index], index),
            )
        return [self.passages[index] for index in ranked[:k]]

    @staticmethod
    def _rerank(query: str, passage: Passage) -> float:
        query_terms = set(_tokens(query))
        passage_terms = set(_tokens(f"{passage.title} {passage.section} {passage.text}"))
        coverage = len(query_terms & passage_terms) / max(1, len(query_terms))
        title_terms = set(_tokens(passage.title))
        title_match = len(query_terms & title_terms) / max(1, len(query_terms))
        exact_phrase = 1.0 if query.strip().lower() in passage.title.lower() else 0.0
        metadata = set(_tokens(" ".join(passage.services + passage.symptoms)))
        metadata_match = len(query_terms & metadata) / max(1, len(query_terms))
        return 2.0 * coverage + 2.0 * title_match + exact_phrase + metadata_match


def load_index(root: Optional[str | Path] = None) -> KnowledgeIndex:
    if root is None:
        root = Path(__file__).resolve().parent
    return KnowledgeIndex(load_passages(root))
