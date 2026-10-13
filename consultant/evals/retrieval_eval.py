"""Phase 4 retrieval eval: hand-written queries, is the right case/primer in the top-k?
Offline and deterministic (BM25). Usage: uv run python -m consultant.evals.retrieval_eval"""

import json
from pathlib import Path

import yaml

from consultant.config import ROOT
from consultant.knowledge.retrieval import Retriever

QUERIES = ROOT / "data/derived/evals/retrieval_queries.yaml"


def run(k: int = 5, queries: Path = QUERIES, retriever: Retriever | None = None) -> dict:
    r = retriever or Retriever.load()
    rows = []
    for item in yaml.safe_load(queries.read_text(encoding="utf-8"))["queries"]:
        if "expect_case" in item:
            hits = r.similar_cases(item["q"], k)
            ok = item["expect_case"] in [h.doc.id for h in hits]
        else:
            hits = r.primers(item["q"], k)
            ok = item["expect_industry"] in [h.doc.meta.get("industry") for h in hits]
        rows.append({"q": item["q"], "ok": ok, "top": [h.doc.title for h in hits]})
    out = {"k": k, "n": len(rows), "hit_rate": sum(r["ok"] for r in rows) / len(rows),
           "misses": [r for r in rows if not r["ok"]]}
    (ROOT / "eval_results").mkdir(exist_ok=True)
    (ROOT / "eval_results/retrieval.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps(run(), indent=1))
