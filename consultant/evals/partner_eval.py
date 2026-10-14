"""Phase 5 gate (live): does the Partner catch every planted flaw as a blocking issue, and
approve the clean draft (comments allowed)? Usage: uv run python -m consultant.evals.partner_eval"""

import json
from pathlib import Path

import yaml

from consultant.agents.partner import Review, review
from consultant.config import ROOT

SEEDED = Path(__file__).with_name("partner_seeded.yaml")


def caught(r: Review, kind: str) -> bool:
    """Strict: the planted flaw must be named as a blocking issue (right kind, major or
    critical). A generic 'revise' without naming it doesn't count."""
    return any(i.kind == kind and i.blocking for i in r.blocking())


def run(llm=None, path: Path = SEEDED) -> dict:
    from consultant.llm import LLM
    from consultant.tracing import Tracer
    llm = llm or LLM(tracer=Tracer(run_id="eval-partner"))
    s = yaml.safe_load(path.read_text(encoding="utf-8"))
    rows = []
    for item in s["flawed"]:
        r = review(llm, s["question"], s["client_prior"], item["draft"], s["evidence"])
        rows.append({"kind": item["kind"], "caught": caught(r, item["kind"]),
                     "verdict": r.verdict, "issues": [i.model_dump() for i in r.issues]})
    clean = review(llm, s["question"], s["client_prior"], s["clean"], s["evidence"])
    n_caught = sum(r["caught"] for r in rows)
    out = {"caught": n_caught, "n": len(rows), "clean_verdict": clean.verdict,
           "clean_comments": [i.model_dump() for i in clean.comments()],
           "pass": n_caught == len(rows) and clean.verdict == "approve", "rows": rows}
    (ROOT / "eval_results").mkdir(exist_ok=True)
    (ROOT / "eval_results/partner_seeded.json").write_text(json.dumps(out, indent=1),
                                                            encoding="utf-8")
    return out


if __name__ == "__main__":
    print(json.dumps({k: v for k, v in run().items() if k != "rows"}, indent=1))
