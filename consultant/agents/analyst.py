"""Analyst: turns a quantitative question into sandboxed Python. Fact values are injected as
FACTS[id] and assumptions as ASSUMPTIONS[id], so no number is retyped from prose; the code's
RESULT is the only source of computed numbers."""

from pydantic import BaseModel, Field

from consultant.research.fact_store import Assumption, Fact
from consultant.workspace import AnalysisRecord, Workspace

TEMPLATES_DOC = """Available in the sandbox (import from consultant.analysis.templates):
top_down(total, *shares), bottom_up(units, price, freq=1), break_even_units(fixed, price, var),
break_even_revenue(fixed, cm_ratio), unit_economics(price, variable_cost, cac,
orders_per_year, years, churn_per_year=0), price_volume_mix(base, current) with
{product: (units, price)}, margin_bridge(base, current) with {line: amount},
sensitivity(f, base, ranges), scenario_table(f, scenarios, base), grid(f, base, a, a_vals,
b, b_vals), npv(rate, cashflows), irr(cashflows), payback_period(cashflows), cagr(start, end,
years), present_value_from_cagr(fv, rate, years)."""

ANALYST_SYS = (
    "You are the analyst. Write a short Python script that answers the analysis request. "
    "Use ONLY numbers from FACTS[<fact id>] and ASSUMPTIONS['S<id>'] (already defined) or "
    "from the request itself; never invent data. You may import the templates below and the "
    "standard library (no network, no files). Assign a dict of named, JSON-serialisable "
    "outputs to RESULT; include the key inputs you used so a reader can audit the result. "
    "Also give `excel` rows: each headline output that is plain arithmetic, written as a "
    "formula over F<id>/S<id> (same calculation as the code; it must give the same number)."
    "\n\n"
    + TEMPLATES_DOC)


class FormulaRow(BaseModel):
    name: str = Field(description="a RESULT key (python identifier)")
    expression: str = Field(description="arithmetic over F<id>, S<id> and earlier row names "
                            "with + - * / ** and parentheses, e.g. (F4 - F3) / F3 * 100")
    unit: str = ""


class AnalysisCode(BaseModel):
    name: str = Field(description="short name, e.g. margin_bridge_fy23_fy24")
    purpose: str
    code: str = Field(description="python that sets RESULT = {...}")
    excel: list[FormulaRow] = Field(default_factory=list, description="for each headline "
                                    "output that is plain arithmetic, the same calculation as "
                                    "a formula (becomes a live spreadsheet formula)")


def preamble(facts: list[Fact], assumptions: list[Assumption]) -> str:
    fv = {f.id: f.value for f in facts if f.value is not None}
    av = {f"S{a.id}": a.value for a in assumptions}
    return f"FACTS = {fv!r}\nASSUMPTIONS = {av!r}\n\n"


def evidence_table(facts: list[Fact], assumptions: list[Assumption], limit: int = 40) -> str:
    rows = [f"F{f.id}: {f.claim} = {f.value} {f.unit} ({f.period})" for f in facts
            if f.value is not None][:limit]
    rows += [f"S{a.id}: {a.name} = {a.value} {a.unit} (estimate: {a.rationale[:80]})"
             for a in assumptions][:limit]
    return "\n".join(rows) or "(no quantitative evidence yet)"


def analyse(llm, ws: Workspace, request: str, hypothesis_ids: list[str] | None = None,
            role: str = "analyst", attempts: int = 2) -> AnalysisRecord:
    facts = ws.facts.facts(ws.state.engagement_id)
    assumptions = ws.facts.assumptions(ws.state.engagement_id)
    msgs = [{"role": "system", "content": ANALYST_SYS},
            {"role": "user", "content": f"ANALYSIS REQUEST: {request}\n\nEVIDENCE:\n"
                                        f"{evidence_table(facts, assumptions)}"}]
    rec = None
    for _ in range(attempts):
        plan = llm.structured(role, msgs, AnalysisCode)
        rec = ws.run_analysis(plan.name, plan.purpose, preamble(facts, assumptions) + plan.code,
                              hypothesis_ids=hypothesis_ids,
                              formulas=[r.model_dump() for r in plan.excel])
        if rec.ok and rec.result is not None and not rec.formula_issues:
            return rec
        problem = rec.error or ("; ".join(rec.formula_issues) if rec.formula_issues
                                else "the script did not set RESULT")
        msgs += [{"role": "assistant", "content": plan.code},
                 {"role": "user", "content": f"That failed: {problem}. Fix the script."}]
    return rec
