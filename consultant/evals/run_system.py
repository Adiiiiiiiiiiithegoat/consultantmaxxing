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

from consultant.agents.engagement_manager import Engagement, hypotheses_text
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


def work_record(ws: Workspace, max_facts: int = 25) -> str:
    """What the judge sees of the system's process: issue tree, hypotheses with status and
    evidence, analyses with results, and the facts gathered."""
    st = ws.state

    def tree(n, depth=0):
        return [f"{'  ' * depth}- {n.id} {n.text}"] + [x for c in n.children
                                                       for x in tree(c, depth + 1)]
    analyses = [f"{a.id} {a.name}: {a.purpose} -> "
                + (json.dumps(a.result, default=str)[:300] if a.ok else f"FAILED ({a.error})")
                for a in st.analyses]
    facts = ws.facts.facts(st.engagement_id)
    return "\n".join(
        ["ISSUE TREE:", *(tree(st.issue_tree) if st.issue_tree else ["(none)"]),
         "HYPOTHESES:", hypotheses_text(ws) or "(none)",
         "ANALYSES:", *(analyses or ["(none)"]),
         f"FACTS ({len(facts)} gathered):",
         *[f"F{f.id}: {f.claim[:160]}" for f in facts[:max_facts]]])


def final_draft(ws: Workspace) -> Draft:
    """The draft that was delivered: after the repair pass if it ran, else the last story."""
    n = ws.state.notes
    if "repair" in n:
        return Draft(**n["repair"])
    last = max(int(k[5:]) for k in n if k.startswith("story") and k[5:].isdigit())
    return Draft(**n[f"story{last}"])


def submission(iv: Interviewer, draft: Draft, searches: int = 0, work: str = "") -> Submission:
    t = [{"role": "interviewer", "content": iv.opening()}]
    for e in iv.log:
        t += [{"role": "candidate", "content": e["question"]},
              {"role": "interviewer", "content": e["reply"]}]
    return Submission(transcript=t, final_answer=render_text(draft),
                      questions_asked=len(iv.log), searches=searches, work=work)


def run_case(llm, case: Case, root: Path, profile: str = "dev") -> dict:
    ws = Workspace.open(f"case-{case.id}", root=root, mode="case", profile=profile)
    iv = Interviewer(case, llm)
    # replay the interview log so a resumed engagement's transcript is complete
    iv.log = ws.state.notes.get("interview_log", [])
    out = Engagement(llm, ws, interviewer=iv).run()
    sub = submission(iv, out["draft"], work=work_record(ws))
    sub.tokens = out["tokens"]
    verdict = judge(llm, case, sub)
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


def rejudge(name: str = "system-train", out: str | None = None) -> list[dict]:
    """Re-score finished case-mode runs with the current judge (no engagement calls): same
    transcript and delivered answer, new verdict. System rows are rebuilt from the workspace;
    baseline rows carry their submission. Writes eval_results/<out>/; resumable."""
    out_dir = RESULTS / (out or f"{name}-rejudged")
    out_dir.mkdir(parents=True, exist_ok=True)
    llm = LLM(tracer=Tracer(run_id=f"eval-{out_dir.name}"))
    cases = {c.id: c for split in ("train", "test") for c in load_cases(split)}
    rows = []
    for p in sorted((RESULTS / name).glob("*.json")):
        if (out_dir / p.name).exists():
            rows.append(json.loads((out_dir / p.name).read_text(encoding="utf-8")))
            continue
        old = json.loads(p.read_text(encoding="utf-8"))
        case = cases[old["case_id"]]
        if "submission" in old:
            sub = Submission(**old["submission"])
        else:
            ws = Workspace.load(f"case-{case.id}", root=ROOT / "engagements" / name)
            iv = Interviewer(case, llm)
            iv.log = ws.state.notes.get("interview_log", [])
            sub = submission(iv, final_draft(ws), work=work_record(ws))
        try:
            verdict = judge(llm, case, sub)
        except QuotaExhausted as e:
            print(f"quota exhausted at {case.id}: {e}; rerun to resume", flush=True)
            break
        row = {**old, "judge_version": code_version(), "scores": verdict.scores(),
               "overall": verdict.overall(), "verdict": verdict.model_dump()}
        (out_dir / p.name).write_text(json.dumps(row, indent=1, ensure_ascii=False),
                                      encoding="utf-8")
        rows.append(row)
    return rows


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
    j = sub.add_parser("rejudge")
    j.add_argument("name")
    j.add_argument("--out", default=None)
    r = sub.add_parser("real")
    r.add_argument("question")
    r.add_argument("--name", default="real")
    r.add_argument("--profile", default="dev")
    a = p.parse_args(argv)
    if a.cmd == "cases":
        res = run_cases(a.split, a.n, a.name or f"system-{a.split}", a.profile)
    elif a.cmd == "rejudge":
        res = {r["case_id"]: r["scores"] for r in rejudge(a.name, a.out)}
    else:
        res = run_real(a.question, a.name, a.profile)
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
