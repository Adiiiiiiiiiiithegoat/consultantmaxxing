"""Baseline: one model, one plain consultant prompt, no tools or playbooks, through the same
simulator and judge. Resumable per case.

Usage: uv run python -m consultant.evals.baseline [n_cases]
"""

import json
import sys
from typing import Literal

from pydantic import BaseModel

from consultant.config import ROOT
from consultant.evals.judge import RUBRIC, Submission, judge
from consultant.evals.simulator import Interviewer
from consultant.knowledge.extract import load_cases
from consultant.knowledge.schema import Case
from consultant.llm import LLM, QuotaExhausted
from consultant.llm.context import clip
from consultant.tracing import Tracer

RESULTS = ROOT / "eval_results"
MAX_QUESTIONS = 8

BASELINE_SYS = (
    "You are a management consultant solving a case interview. Each turn is one of two "
    "actions. 'ask': say something to the interviewer, i.e. one question to clarify the "
    "problem or request data; if the interviewer asked you something, reply briefly here "
    "and continue. 'final_answer': END the case with your answer to the original problem: "
    "the recommendation first, then the supporting analysis and next steps. You have a "
    "limited number of questions.")


class Turn(BaseModel):
    action: Literal["ask", "final_answer"]
    content: str


def run_case(llm: LLM, case: Case, role: str = "baseline") -> Submission:
    iv = Interviewer(case, llm)
    transcript = [{"role": "interviewer", "content": iv.opening()}]
    final = ""
    for i in range(MAX_QUESTIONS + 1):
        left = MAX_QUESTIONS - i
        convo = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in transcript)
        note = (f"Questions left: {left}." if left else
                "No questions left: give your final answer now (action=final_answer).")
        msgs = [{"role": "system", "content": BASELINE_SYS},
                {"role": "user", "content": f"{clip(convo, 4000)}\n\n{note}"}]
        turn = llm.structured(role, msgs, Turn)
        if turn.action == "final_answer" or not left:
            final = turn.content
            break
        transcript += [{"role": "candidate", "content": turn.content},
                       {"role": "interviewer", "content": iv.ask(turn.content)}]
    return Submission(transcript=transcript, final_answer=final,
                      questions_asked=len(iv.log))


def run(split: str = "test", limit: int | None = None, name: str = "baseline") -> dict:
    out_dir = RESULTS / name
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = LLM(tracer=Tracer(run_id=f"eval-{name}"))
    cases = load_cases(split)[:limit]
    for case in cases:
        path = out_dir / f"{case.id}.json"
        if path.exists():
            continue
        try:
            sub = run_case(llm, case)
            verdict = judge(llm, case, sub)
        except QuotaExhausted as e:
            print(f"quota exhausted at {case.id}: {e}; rerun later to resume", flush=True)
            break
        path.write_text(json.dumps({"case_id": case.id, "case_type": case.case_type,
                                    "submission": sub.model_dump(),
                                    "verdict": verdict.model_dump(),
                                    "scores": verdict.scores(), "overall": verdict.overall()},
                                   indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"{case.id}: {verdict.overall():.2f} {verdict.scores()}", flush=True)
    rows = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(out_dir.glob("*.json"))]
    summary = {
        "name": name, "split": split, "cases_done": len(rows), "cases_total": len(cases),
        "overall": sum(r["overall"] for r in rows) / len(rows) if rows else None,
        "by_dimension": {k: sum(r["scores"][k] for r in rows) / len(rows) for k in RUBRIC}
        if rows else {},
        "usage": llm.tracer.usage(),
        "cases": {r["case_id"]: r["overall"] for r in rows},
    }
    (RESULTS / f"{name}.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    return summary


if __name__ == "__main__":
    s = run(limit=int(sys.argv[1]) if len(sys.argv) > 1 else None)
    print(json.dumps({k: v for k, v in s.items() if k != "cases"}, indent=1))
