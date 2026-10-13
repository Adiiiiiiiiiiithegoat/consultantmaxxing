"""Run the full system and score it with the judge.

Case mode (Phase 5 gate on train cases; Phase 7 on the test split):
  uv run python -m consultant.evals.run_system cases --split train --n 3 --name system-train
  uv run python -m consultant.evals.run_system cases --split test --name system-test
Real mode (one open-web question, no judge reference):
  uv run python -m consultant.evals.run_system real "Why did X's margins fall in FY24?"
Resumable: engagements resume from their workspace; finished case results are skipped.
"""

import argparse
import json
from pathlib import Path

from consultant.agents.engagement_manager import Engagement
from consultant.agents.storyliner import Draft, render_text
from consultant.config import ROOT
from consultant.evals.common import code_version
from consultant.evals.judge import Submission, judge
from consultant.evals.simulator import Interviewer
from consultant.knowledge.extract import load_cases
from consultant.knowledge.schema import Case
from consultant.llm import LLM, QuotaExhausted
from consultant.tracing import Tracer
from consultant.workspace import Workspace

RESULTS = ROOT / "eval_results"


def submission(iv: Interviewer, draft: Draft, searches: int = 0) -> Submission:
    t = [{"role": "interviewer", "content": iv.opening()}]
    for e in iv.log:
        t += [{"role": "candidate", "content": e["question"]},
              {"role": "interviewer", "content": e["reply"]}]
    return Submission(transcript=t, final_answer=render_text(draft),
                      questions_asked=len(iv.log), searches=searches)


def run_case(llm, case: Case, root: Path, profile: str = "dev") -> dict:
    ws = Workspace.open(f"case-{case.id}", root=root, mode="case", profile=profile)
    iv = Interviewer(case, llm)
    # replay the interview log so a resumed engagement's transcript is complete
    iv.log = ws.state.notes.get("interview_log", [])
    out = Engagement(llm, ws, interviewer=iv).run()
    verdict = judge(llm, case, submission(iv, out["draft"]))
    return {"case_id": case.id, "case_type": case.case_type, "code_version": code_version(),
            "stopped": out["stopped"], "scores": verdict.scores(),
            "overall": verdict.overall(), "verdict": verdict.model_dump(),
            "workspace": str(ws.dir)}


def run_cases(split: str = "train", n: int | None = 3, name: str = "system-train",
              profile: str = "dev") -> dict:
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    tracer = Tracer(run_id=f"eval-{name}")
    llm = LLM(tracer=tracer)
    cases = load_cases(split)[:n]
    for case in cases:
        path = out_dir / f"{case.id}.json"
        if path.exists():
            continue
        try:
            row = run_case(llm, case, ROOT / "engagements" / name, profile)
        except QuotaExhausted as e:
            print(f"quota exhausted at {case.id}: {e}; rerun to resume", flush=True)
            break
        path.write_text(json.dumps(row, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{case.id}: {row['overall']:.2f} {row['scores']}", flush=True)
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("*.json"))]
    summary = {"name": name, "split": split, "done": len(rows), "total": len(cases),
               "overall": sum(r["overall"] for r in rows) / len(rows) if rows else None,
               "within_budget": sum(not r["stopped"] for r in rows),
               "code_versions": sorted({r["code_version"] for r in rows}),
               "usage": tracer.usage()}
    (RESULTS / f"{name}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


def run_real(question: str, name: str = "real", profile: str = "dev") -> dict:
    from consultant.research.web import Web
    tracer = Tracer(run_id=f"eval-{name}")
    llm = LLM(tracer=tracer)
    eid = name
    ws = Workspace.open(eid, root=ROOT / "engagements", mode="real", profile=profile,
                        problem_statement=question)
    out = Engagement(llm, ws, web=Web(tracer=tracer)).run()
    return {"workspace": str(ws.dir), "stopped": out["stopped"],
            "deliverables": out["deliverables"], "usage": tracer.usage()}


def main(argv=None):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cases")
    c.add_argument("--split", default="train")
    c.add_argument("--n", type=int, default=None)
    c.add_argument("--name", default=None)
    c.add_argument("--profile", default="dev")
    r = sub.add_parser("real")
    r.add_argument("question")
    r.add_argument("--name", default="real")
    r.add_argument("--profile", default="dev")
    a = p.parse_args(argv)
    if a.cmd == "cases":
        res = run_cases(a.split, a.n, a.name or f"system-{a.split}", a.profile)
    else:
        res = run_real(a.question, a.name, a.profile)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
