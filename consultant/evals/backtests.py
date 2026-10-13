"""Phase 7 research backtests: real-mode engagements answered as of a past date (sources
restricted to on/before it), scored against the actual outcome.
Usage: uv run python -m consultant.evals.backtests [--include-unverified]"""

import json
import sys
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from consultant.agents.engagement_manager import Engagement
from consultant.agents.storyliner import render_text
from consultant.config import ROOT
from consultant.evals.common import code_version

BACKTESTS = Path(__file__).with_name("backtests.yaml")
OUT = ROOT / "eval_results" / "backtests"


class BacktestVerdict(BaseModel):
    call_correct: bool = Field(description="did the answer get the direction/call right?")
    key_points_found: list[bool] = Field(description="one per key point, in order")
    calibration: int = Field(ge=1, le=5, description="confidence matched the evidence")
    evidence_quality: int = Field(ge=1, le=5)
    justification: str


SCORE_SYS = ("You score a consulting answer written as of a past date against what actually "
             "happened later. Judge the call, which key points it anticipated (as drivers or "
             "risks), whether its stated confidence was appropriate, and the quality of its "
             "evidence. Do not penalise it for not knowing the future exactly; reward "
             "identifying the right drivers and risks.")


def score(llm, bt: dict, answer: str, role: str = "judge") -> BacktestVerdict:
    user = (f"AS OF: {bt['as_of']}\nQUESTION: {bt['question']}\n\nANSWER GIVEN:\n{answer}\n\n"
            f"WHAT HAPPENED: {bt['outcome']}\nKEY POINTS: {bt['key_points']}")
    return llm.structured(role, [{"role": "system", "content": SCORE_SYS},
                                 {"role": "user", "content": user}], BacktestVerdict)


def run_one(llm, web, bt: dict, root: Path, profile: str = "dev") -> dict:
    from consultant.workspace import Workspace
    ws = Workspace.open(f"backtest-{bt['id']}", root=root, mode="real", profile=profile,
                        problem_statement=bt["question"])
    out = Engagement(llm, ws, web=web, as_of=bt["as_of"]).run()
    v = score(llm, bt, render_text(out["draft"]))
    return {"id": bt["id"], "as_of": bt["as_of"], "code_version": code_version(),
            "verdict": v.model_dump(), "call_correct": v.call_correct,
            "key_points": sum(v.key_points_found) / max(1, len(bt["key_points"])),
            "workspace": str(ws.dir), "stopped": out["stopped"]}


def run(include_unverified: bool = False, path: Path = BACKTESTS, llm=None, web=None,
        root: Path | None = None) -> dict:
    from consultant.llm import LLM
    from consultant.research.web import Web
    from consultant.tracing import Tracer
    tracer = Tracer(run_id="eval-backtests")
    llm = llm or LLM(tracer=tracer)
    web = web or Web(tracer=tracer)
    OUT.mkdir(parents=True, exist_ok=True)
    rows, skipped = [], []
    for bt in yaml.safe_load(path.read_text(encoding="utf-8"))["backtests"]:
        if not bt.get("verified") and not include_unverified:
            skipped.append(bt["id"])
            continue
        p = OUT / f"{bt['id']}.json"
        if not p.exists():
            p.write_text(json.dumps(run_one(llm, web, bt, root or ROOT / "engagements"),
                                    indent=1), encoding="utf-8")
        rows.append(json.loads(p.read_text(encoding="utf-8")))
    summary = {"done": len(rows), "skipped_unverified": skipped,
               "calls_correct": sum(r["call_correct"] for r in rows),
               "key_point_recall": sum(r["key_points"] for r in rows) / len(rows) if rows else None}
    (ROOT / "eval_results" / "backtests.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    print(json.dumps(run("--include-unverified" in sys.argv), indent=1))
