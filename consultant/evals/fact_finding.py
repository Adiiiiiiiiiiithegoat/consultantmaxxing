"""Phase 2 gate eval: the Research Associate answers 20 factual questions from the open web.
Scores: accuracy within tolerance, citation-verifier pass rate, and sourced-eligibility.
Resumable per question. Usage: uv run python -m consultant.evals.fact_finding [--dev] [n]
"""

import json
import sys
from pathlib import Path

import yaml

from consultant.agents.research_associate import ResearchAssociate
from consultant.config import ROOT
from consultant.llm import LLM, QuotaExhausted
from consultant.research.extract import to_base
from consultant.research.fact_store import FactStore
from consultant.research.verify import verify_facts
from consultant.research.web import Web
from consultant.tracing import Tracer

QUESTIONS = Path(__file__).with_name("fact_questions.yaml")
OUT = ROOT / "eval_results" / "fact_finding"
CAPS = {"searches": 4, "pages": 10, "tokens": 25000}


def score(value: float | None, unit: str, q: dict) -> tuple[bool, float | None]:
    if value is None:
        return False, None
    got, _ = to_base(value, unit)
    want = float(q["answer"])  # YAML 1.1 reads 1.5e12 as a string
    err = abs(got - want) / abs(want)
    return err <= q["tol"], got


def run(limit: int | None = None, dev: bool = False) -> dict:
    out = OUT.with_name("fact_finding_dev") if dev else OUT
    out.mkdir(parents=True, exist_ok=True)
    src = QUESTIONS.with_name("fact_questions_dev.yaml") if dev else QUESTIONS
    qs = yaml.safe_load(src.read_text(encoding="utf-8"))["questions"][:limit]
    tracer = Tracer(run_id="eval-fact-finding" + ("-dev" if dev else ""))
    llm = LLM(tracer=tracer)
    web = Web(tracer=tracer)
    store = FactStore(out / "facts.sqlite")
    for q in qs:
        path = out / f"{q['id']}.json"
        if path.exists():
            continue
        eid = f"factfind-{q['id']}"
        try:
            res = ResearchAssociate(llm, web, store, eid, caps=CAPS,
                                    allow_estimate=False).research(q["question"])
            ver = verify_facts(store, web, eid, llm)
        except QuotaExhausted as e:
            print(f"quota exhausted at {q['id']}: {e}; rerun later to resume", flush=True)
            break
        ok, got = score(res.brief.value, res.brief.unit, q)
        row = {"id": q["id"], "correct": ok, "expected": float(q["answer"]), "got_base": got,
               "brief": res.brief.model_dump(), "n_facts": len(res.facts),
               "rejected_facts": res.rejected_facts, "spent": res.spent, "verification": ver,
               "sources": sorted({f.source_url for f in res.facts})}
        path.write_text(json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{q['id']}: {'OK ' if ok else 'BAD'} got={got} want={q['answer']} "
              f"facts={len(res.facts)} verified={ver['verified']}/{ver['verified'] + ver['failed']}"
              f" spent={res.spent}", flush=True)
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out.glob("*.json"))
            if p.name != "summary.json"]
    total_v = sum(r["verification"]["verified"] for r in rows)
    total_f = sum(r["verification"]["failed"] for r in rows)
    unsourced = store.db.execute("SELECT COUNT(*) FROM facts WHERE verified = 1 AND "
                                 "(source_url = '' OR quote = '')").fetchone()[0]
    summary = {"done": len(rows), "total": len(qs),
               "accuracy": sum(r["correct"] for r in rows) / len(rows) if rows else None,
               "verifier_rate": total_v / (total_v + total_f) if total_v + total_f else None,
               "facts": total_v + total_f, "eligible_without_source": unsourced,
               "caps": CAPS, "usage": tracer.usage()}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dev"]
    print(json.dumps(run(int(args[0]) if args else None, dev="--dev" in sys.argv), indent=1))
