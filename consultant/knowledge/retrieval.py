"""Retrieval over the case library (train split only, indexed by problem pattern) and
industry primers (casebook primers + primers the agent adds from its own research).

Lexical BM25 in pure Python. ponytail: no embedding model or vector DB — the corpus is
~200 docs and the machine is memory-constrained; add embeddings (Ollama nomic-embed-text)
and LanceDB if retrieval quality or corpus size demands it.

Usage: uv run python -m consultant.knowledge.retrieval build
"""

import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from consultant.config import ROOT

INDEX = ROOT / "data/index"
CHUNKS = ROOT / "data/derived/industry_chunks.jsonl"
STOP = set("""a an the and or of to in on for with by from at as is are was were be been it its
this that these those what which who how why when where do does did can could should would
our their your client company firm we they has have had not no more most than into over""".split())


def tokenize(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in STOP or len(w) < 2:
            continue
        for suf in ("ing", "ers", "ies", "ed", "es", "s"):  # crude stemmer, enough for BM25
            if len(w) > len(suf) + 3 and w.endswith(suf):
                w = w[: -len(suf)] + ("y" if suf == "ies" else "")
                break
        out.append(w)
    return out


@dataclass
class Doc:
    id: str
    kind: str          # "case" | "primer"
    title: str
    text: str
    meta: dict


@dataclass
class Hit:
    doc: Doc
    score: float


class BM25:
    def __init__(self, docs: list[Doc], k1: float = 1.5, b: float = 0.75):
        self.docs, self.k1, self.b = docs, k1, b
        self.tfs = [Counter(tokenize(f"{d.title} {d.text}")) for d in docs]
        self.lens = [sum(tf.values()) for tf in self.tfs]
        self.avg = sum(self.lens) / len(self.lens) if docs else 0
        df = Counter(t for tf in self.tfs for t in tf)
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def search(self, query: str, k: int = 5, kind: str | None = None) -> list[Hit]:
        q = tokenize(query)
        scores = []
        for i, (d, tf) in enumerate(zip(self.docs, self.tfs, strict=True)):
            if kind and d.kind != kind:
                continue
            s = 0.0
            for t in q:
                if t in tf:
                    f = tf[t]
                    s += self.idf[t] * f * (self.k1 + 1) / (
                        f + self.k1 * (1 - self.b + self.b * self.lens[i] / self.avg))
            if s > 0:
                scores.append(Hit(d, s))
        return sorted(scores, key=lambda h: -h.score)[:k]


# ---- index building ------------------------------------------------------------------------
def case_docs() -> list[Doc]:
    """Train split only: the held-out test cases must never be retrievable."""
    from consultant.knowledge.extract import load_cases
    test_ids = {c.id for c in load_cases("test")}
    docs = []
    for c in load_cases("train"):
        if c.id in test_ids:
            raise RuntimeError(f"leakage: test case {c.id} found in train split")
        pattern = (f"{c.case_type.replace('_', ' ')} case, {c.industry}. Problem: "
                   f"{c.problem_statement} Crux: {c.key_insight}")
        docs.append(Doc(c.id, "case", c.title, pattern,
                        {"case_type": c.case_type, "industry": c.industry,
                         "difficulty": c.difficulty, "structure": c.structure,
                         "recommendation": c.recommendation,
                         "feedback": c.interviewer_feedback}))
    return docs


def primer_docs(index_dir: Path = INDEX) -> list[Doc]:
    docs = []
    if CHUNKS.exists():
        for i, line in enumerate(CHUNKS.read_text(encoding="utf-8").splitlines()):
            c = json.loads(line)
            docs.append(Doc(f"primer-{i}", "primer", f"{c['industry']}: {c['aspect']}",
                            c["text"], {"industry": c["industry"], "aspect": c["aspect"],
                                        "source": "casebook primer"}))
    extra = index_dir / "primers_extra.jsonl"
    if extra.exists():
        for i, line in enumerate(extra.read_text(encoding="utf-8").splitlines()):
            c = json.loads(line)
            docs.append(Doc(f"primer-x{i}", "primer", f"{c['industry']}: {c['aspect']}",
                            c["text"], {**c, "source": "research"}))
    return docs


def build(index_dir: Path = INDEX) -> dict:
    index_dir.mkdir(parents=True, exist_ok=True)
    docs = case_docs() + primer_docs(index_dir)
    (index_dir / "docs.json").write_text(json.dumps([vars(d) for d in docs], indent=1,
                                                    ensure_ascii=False), encoding="utf-8")
    return {"cases": sum(d.kind == "case" for d in docs),
            "primers": sum(d.kind == "primer" for d in docs)}


def add_primer(industry: str, aspect: str, text: str, fact_ids: list[int],
               index_dir: Path = INDEX) -> None:
    """Auto-extend primers when the agent researches a new industry (facts must be sourced;
    the primer cites their ids). Rebuild or reload the retriever to pick it up."""
    index_dir.mkdir(parents=True, exist_ok=True)
    row = {"industry": industry, "aspect": aspect, "text": text, "fact_ids": fact_ids}
    with (index_dir / "primers_extra.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class Retriever:
    def __init__(self, docs: list[Doc]):
        self.docs = docs
        self.bm25 = BM25(docs)

    @classmethod
    def load(cls, index_dir: Path = INDEX) -> "Retriever":
        path = index_dir / "docs.json"
        if not path.exists():
            return cls([])  # no private data on this machine: retrieval simply returns nothing
        return cls([Doc(**d) for d in json.loads(path.read_text(encoding="utf-8"))])

    def similar_cases(self, problem: str, k: int = 3) -> list[Hit]:
        return self.bm25.search(problem, k, kind="case") if self.docs else []

    def primers(self, query: str, k: int = 5) -> list[Hit]:
        return self.bm25.search(query, k, kind="primer") if self.docs else []

    def has_industry(self, industry: str) -> bool:
        words = set(tokenize(industry))
        return any(words & set(tokenize(d.meta.get("industry", "")))
                   for d in self.docs if d.kind == "primer")


if __name__ == "__main__" and sys.argv[1:] == ["build"]:
    print(build())
