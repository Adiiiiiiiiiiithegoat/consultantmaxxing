"""Estimation fallback: when research can't find a number, build a top-down or bottom-up
guesstimate. The LLM proposes factors and a formula; Python evaluates it (no maths in prose).
Each factor is either a cited fact or an assumptions-register entry labelled 'estimate'."""

import ast
import operator
from typing import Literal

from pydantic import BaseModel, Field

from consultant.llm import LLM
from consultant.research.fact_store import Assumption, FactStore


class Factor(BaseModel):
    name: str = Field(description="python identifier, e.g. urban_households")
    value: float
    unit: str
    rationale: str = Field(description="why this value; cite the fact id if it comes from one")
    fact_id: int | None = Field(default=None, description="id of the supporting fact, if any")


class EstimatePlan(BaseModel):
    approach: Literal["top_down", "bottom_up"]
    factors: list[Factor]
    formula: str = Field(description="arithmetic over factor names only, e.g. a * b / c")
    unit: str


class Estimate(BaseModel):
    value: float
    unit: str
    approach: str
    formula: str
    assumption_ids: list[int]
    fact_ids: list[int]


_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow, ast.USub: operator.neg}


def safe_eval(formula: str, names: dict[str, float]) -> float:
    """Evaluate + - * / ** and parentheses over known names and numbers; nothing else."""
    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.Name) and node.id in names:
            return names[node.id]
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError(f"disallowed expression element: {ast.dump(node)[:80]}")
    return float(ev(ast.parse(formula, mode="eval")))


EST_SYS = ("Build a transparent estimate for the question. Break it into 3-7 factors; use the "
           "given facts where they apply (cite fact_id) and state a rationale for every assumed "
           "value. The formula may only combine factor names with + - * / ** and parentheses.")


def estimate(llm: LLM, store: FactStore, engagement_id: str, question: str,
             facts_brief: str = "", role: str = "research_associate") -> Estimate:
    msgs = [{"role": "system", "content": EST_SYS},
            {"role": "user", "content": f"QUESTION: {question}\n\nKNOWN FACTS:\n"
                                        f"{facts_brief or '(none)'}"}]
    plan = llm.structured(role, msgs, EstimatePlan)
    names = {f.name: f.value for f in plan.factors}
    value = safe_eval(plan.formula, names)
    known = {f.id for f in store.facts(engagement_id)}
    a_ids, f_ids = [], []
    for f in plan.factors:
        if f.fact_id in known:
            f_ids.append(f.fact_id)
        else:
            a_ids.append(store.add_assumption(Assumption(
                engagement_id=engagement_id, name=f.name, value=f.value, unit=f.unit,
                rationale=f.rationale)))
    return Estimate(value=value, unit=plan.unit, approach=plan.approach, formula=plan.formula,
                    assumption_ids=a_ids, fact_ids=f_ids)
