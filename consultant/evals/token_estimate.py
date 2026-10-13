"""Tokens per engagement and Phase 7 duration on a given model plan.

Two sources:
- `estimate(profile)`: structural model. Measures the real system prompts/playbooks with the
  same token estimator the client uses, counts calls from the engagement loop and profile
  limits, and uses observed numbers where available. Every assumption is listed in the
  output. Use until live runs exist.
- `from_traces(run_id)`: actual usage per model from traces/<run_id>/events.jsonl. Replace
  the estimate with this after the first live engagements.

Usage: uv run python -m consultant.evals.token_estimate [dev|full] [--traces RUN_ID]
"""

import json
import sys
from collections import defaultdict

from consultant.agents import analyst, partner, storyliner
from consultant.agents import engagement_manager as em
from consultant.config import models_config
from consultant.config import profile as load_profile
from consultant.knowledge import playbooks
from consultant.llm.context import estimate_tokens as est
from consultant.tracing import Tracer

ASSUMPTIONS = {
    "fact_row_tokens": 55,          # one evidence row (F#: claim | value | period | source)
    "facts_dev": 25, "facts_full": 60,
    "hypotheses": 5, "hypothesis_row_tokens": 45,
    "interviewer_items": 15, "interviewer_item_tokens": 70,
    "output_fraction": 0.5,         # typical output vs role max_tokens (reasoning included)
    "ra_remote_tokens_per_question": 3500,  # observed in Phase 2 dev runs (gpt-oss-20b)
    "review_rounds": 2, "analyses_per_iteration": 1,
    "judge_prompt": 5200,           # judge clips reference + transcript + answer
    "baseline_tokens_per_case": 12000,  # observed: Phase 1 baseline on gpt-oss-120b
}


def _role(cfg, role):
    r = cfg["roles"][role]
    return r["model"], r.get("params", {}).get("max_tokens", 1024)


def estimate(profile_name: str = "dev", mode: str = "case", cfg: dict | None = None) -> dict:
    cfg, p, a = cfg or models_config(), load_profile(profile_name), ASSUMPTIONS
    facts = a["facts_dev"] if profile_name == "dev" else a["facts_full"]
    evidence = facts * a["fact_row_tokens"]
    hyps = a["hypotheses"] * a["hypothesis_row_tokens"]
    playbook = est(playbooks.playbook("profitability"))
    calls = []  # (role, n, prompt_tokens)
    calls.append(("engagement_manager", 1, est(em.SCOPE_SYS) + 300))
    calls.append(("engagement_manager", 1, est(em.PLAN_SYS) + playbook + 900 + evidence // 3))
    calls.append(("engagement_manager", p["max_iterations"], est(em.ASSESS_SYS) + hyps +
                  evidence))
    calls.append(("analyst", p["max_iterations"] * a["analyses_per_iteration"],
                  est(analyst.ANALYST_SYS) + evidence))
    story_sys = est(storyliner.STORY_SYS) + est(playbooks.load("deliverables"))
    calls.append(("storyliner", a["review_rounds"], story_sys + hyps + evidence + 300))
    calls.append(("partner", a["review_rounds"], est(partner.PARTNER_SYS) + 1100 + evidence))
    calls.append(("judge", 1, a["judge_prompt"]))
    if mode == "case":
        calls.append(("interviewer", p["max_questions"],
                      300 + a["interviewer_items"] * a["interviewer_item_tokens"]))
    per_model: dict[str, float] = defaultdict(float)
    rows = []
    for role, n, prompt in calls:
        model, max_out = _role(cfg, role)
        tokens = n * (prompt + a["output_fraction"] * max_out)
        per_model[model] += tokens
        rows.append({"role": role, "model": model, "calls": n, "tokens": round(tokens)})
    if mode == "real":
        model, _ = _role(cfg, "research_associate")
        ra = p["max_iterations"] * p["max_research"] * a["ra_remote_tokens_per_question"]
        per_model[model] += ra
        rows.append({"role": "research_associate", "model": model,
                     "calls": p["max_iterations"] * p["max_research"], "tokens": ra})
    return {"profile": profile_name, "mode": mode, "rows": rows,
            "per_model": {m: round(t) for m, t in per_model.items()},
            "total": round(sum(per_model.values())), "assumptions": a}


def phase7_days(cfg: dict | None = None, n_cases: int = 15, profile_name: str = "dev",
                backtests: int = 3) -> dict:
    """Days to run Phase 7 (system + judge on n test cases, baseline on n, real-mode
    backtests) given each model's daily token quota (models with no tpd = local/unlimited)."""
    cfg = cfg or models_config()
    case = estimate(profile_name, "case", cfg)["per_model"]
    real = estimate(profile_name, "real", cfg)["per_model"]
    need: dict[str, float] = defaultdict(float)
    for m, t in case.items():
        need[m] += n_cases * t
    for m, t in real.items():
        need[m] += backtests * t
    base_model, _ = _role(cfg, "baseline")
    judge_model, _ = _role(cfg, "judge")
    need[base_model] += n_cases * ASSUMPTIONS["baseline_tokens_per_case"]
    need[judge_model] += n_cases * (ASSUMPTIONS["judge_prompt"] + 700)  # judging the baseline
    days = {}
    for m, t in need.items():
        tpd = cfg["models"].get(m, {}).get("tpd")
        days[m] = round(t / tpd, 1) if tpd else 0.0
    return {"tokens_per_model": {m: round(t) for m, t in need.items()}, "days_per_model": days,
            "days": max(days.values()) if days else 0.0, "n_cases": n_cases}


def from_traces(run_id: str) -> dict:
    return Tracer(run_id=run_id).usage()


if __name__ == "__main__":
    if "--traces" in sys.argv:
        print(json.dumps(from_traces(sys.argv[sys.argv.index("--traces") + 1]), indent=1))
    else:
        prof = sys.argv[1] if len(sys.argv) > 1 else "dev"
        for mode in ("case", "real"):
            e = estimate(prof, mode)
            print(json.dumps({k: e[k] for k in ("profile", "mode", "per_model", "total")}))
        for n in (5, 15):
            print(json.dumps(phase7_days(n_cases=n, profile_name=prof)))
