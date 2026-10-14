"""Write EVAL_REPORT.md from eval_results/: per-dimension scores, system vs baseline, tokens
per run, Phase 7 gate verdict, and failure analysis by layer.
Usage: uv run python -m consultant.evals.report [--system system-test] [--baseline baseline]"""

import argparse
import json
from collections import Counter
from pathlib import Path

from consultant.config import ROOT
from consultant.evals.judge import RUBRIC

# Which layer a weak rubric dimension most likely points to (first-pass triage; refine by
# reading the workspace of each failing case).
LAYER = {"scoping": "orchestration", "structure": "playbook gap", "hypotheses": "playbook gap",
         "key_insight": "research", "evidence": "research", "maths": "analysis",
         "recommendation": "synthesis", "communication": "synthesis",
         "efficiency": "orchestration"}
WEAK = 2  # a dimension scoring <= this on a case counts as a failure


def load_rows(results: Path, name: str) -> list[dict]:
    d = results / name
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))] \
        if d.exists() else []


def dims(rows: list[dict]) -> dict[str, float]:
    return {k: sum(r["scores"][k] for r in rows) / len(rows) for k in RUBRIC} if rows else {}


def gate(system: list[dict], baseline: list[dict]) -> dict:
    """System must beat baseline on aggregate and on >= 6 of 9 dimensions (same cases)."""
    common = {r["case_id"] for r in system} & {r["case_id"] for r in baseline}
    s = [r for r in system if r["case_id"] in common]
    b = [r for r in baseline if r["case_id"] in common]
    if not common:
        return {"cases": 0, "pass": False, "reason": "no overlapping cases"}
    ds, db = dims(s), dims(b)
    wins = [k for k in RUBRIC if ds[k] > db[k]]
    agg_s = sum(r["overall"] for r in s) / len(s)
    agg_b = sum(r["overall"] for r in b) / len(b)
    return {"cases": len(common), "system": round(agg_s, 2), "baseline": round(agg_b, 2),
            "dimensions_won": wins, "pass": agg_s > agg_b and len(wins) >= 6}


# The 6 train cases studied by hand on 2026-10-14 (diagnosis of the first Claude run). Fixes
# were developed against these, so results on them are reported apart from untouched cases.
STUDIED = ("iimb-p012-semiconductor-chip-manufacturer", "iimb-p017-indian-domestic-airline",
           "iimb-p025-defence-sector", "iimb-p031-sustainability-strategy-for-fmcg",
           "iimb-p033-packaging-manufacturer", "iimb-p039-port-operator")


def table(system: list[dict], baseline: list[dict]) -> str:
    """Per-dimension markdown table, system vs baseline, on the cases both have."""
    common = {r["case_id"] for r in system} & {r["case_id"] for r in baseline}
    s = [r for r in system if r["case_id"] in common]
    b = [r for r in baseline if r["case_id"] in common]
    if not common:
        return "(no overlapping cases)"
    ds, db = dims(s), dims(b)
    rows = [f"| {k} | {ds[k]:.2f} | {db[k]:.2f} | {ds[k] - db[k]:+.2f} |" for k in RUBRIC]
    os_, ob = (sum(r["overall"] for r in x) / len(x) for x in (s, b))
    # ASCII header: printed to Windows consoles (cp1252)
    return "\n".join([f"n = {len(common)}", "| Dimension | System | Baseline | Diff |",
                      "|---|---|---|---|", *rows,
                      f"| **overall** | **{os_:.2f}** | **{ob:.2f}** | **{os_ - ob:+.2f}** |"])


def split_report(system: list[dict], baseline: list[dict]) -> str:
    """Studied cases, untouched cases and all, each as its own table."""
    groups = {"Studied 6 (fixes developed against these)": lambda r: r["case_id"] in STUDIED,
              "Untouched cases": lambda r: r["case_id"] not in STUDIED,
              "Combined": lambda r: True}
    return "\n\n".join(f"### {name}\n" + table([r for r in system if keep(r)],
                                                [r for r in baseline if keep(r)])
                       for name, keep in groups.items())


def failures(rows: list[dict]) -> Counter:
    c = Counter()
    for r in rows:
        for k, v in r["scores"].items():
            if v <= WEAK:
                c[LAYER[k]] += 1
    return c


def render(system_name: str = "system-test", baseline_name: str = "baseline",
           results: Path = ROOT / "eval_results") -> str:
    sys_rows, base_rows = load_rows(results, system_name), load_rows(results, baseline_name)
    g = gate(sys_rows, base_rows)
    ds, db = dims(sys_rows), dims(base_rows)
    table = ["| Dimension | System | Baseline | Δ |", "|---|---|---|---|"]
    for k in RUBRIC:
        s, b = ds.get(k), db.get(k)
        delta = f"{s - b:+.2f}" if s is not None and b is not None else "–"
        table.append(f"| {k} | {s if s is None else f'{s:.2f}'} | "
                     f"{b if b is None else f'{b:.2f}'} | {delta} |")
    usage = {}
    for name in (system_name, baseline_name):
        p = results / f"{name}.json"
        if p.exists():
            usage[name] = json.loads(p.read_text(encoding="utf-8")).get("usage", {})
    tok = "\n".join(f"- **{n}**: " + ", ".join(
        f"{m}: {int(u.get('prompt_tokens', 0) + u.get('completion_tokens', 0)):,} tokens / "
        f"{int(u.get('calls', 0))} calls" for m, u in us.items()) for n, us in usage.items())
    fails = failures(sys_rows)
    per_case = "\n".join(f"| {r['case_id']} | {r['overall']:.2f} | "
                         f"{', '.join(k for k, v in r['scores'].items() if v <= WEAK) or '–'} |"
                         for r in sys_rows)
    bt = results / "backtests.json"
    backtests = json.loads(bt.read_text()) if bt.exists() else None
    return f"""# Evaluation report

Generated by `python -m consultant.evals.report` from `eval_results/`.

## Gate (Phase 7)
System must beat the baseline on aggregate judge score and on at least 6 of 9 dimensions.

- Cases compared: {g.get('cases', 0)}
- Aggregate: system {g.get('system', '–')} vs baseline {g.get('baseline', '–')}
- Dimensions won: {len(g.get('dimensions_won', []))}/9 ({', '.join(g.get('dimensions_won', []))})
- **Result: {'PASS' if g['pass'] else 'FAIL'}**

## Scores by dimension (1–5, mean over cases)
{chr(10).join(table)}

## Tokens per run
{tok or '- no usage recorded'}

## Failure analysis by layer (dimensions scoring ≤ {WEAK}, first-pass triage)
{chr(10).join(f'- {layer}: {n}' for layer, n in fails.most_common()) or '- none'}

| Case | Overall | Weak dimensions |
|---|---|---|
{per_case or '| – | – | – |'}

## Research backtests
{json.dumps(backtests, indent=1) if backtests else 'not run'}

## Notes
Layer triage maps weak rubric dimensions to the layer most likely at fault (structure and
hypotheses → playbook gap; key insight and evidence → research; maths → analysis;
recommendation and communication → synthesis; scoping and efficiency → orchestration).
Confirm by reading the failing engagements' workspaces before changing a layer.
"""


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--system", default="system-test")
    p.add_argument("--baseline", default="baseline")
    p.add_argument("--out", default=str(ROOT / "EVAL_REPORT.md"))
    p.add_argument("--split", action="store_true", help="print studied / untouched / combined "
                   "tables instead of writing the report")
    a = p.parse_args(argv)
    if a.split:
        r = ROOT / "eval_results"
        print(split_report(load_rows(r, a.system), load_rows(r, a.baseline)))
        return
    Path(a.out).write_text(render(a.system, a.baseline), encoding="utf-8")
    print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
