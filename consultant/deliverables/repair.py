"""Targeted revision pass for validator flags: no flagged number ships as fact.

1. One storyliner call rewrites only the flagged parts of the draft (re-cite the evidence the
   number really comes from, or drop the number); everything else is left as it was.
2. Whatever is still flagged (or everything, when there is no budget for the call) has the
   number replaced by UNVERIFIED and is listed so the memo can say what was removed."""

import json
import re

from pydantic import BaseModel, Field

from consultant.agents.storyliner import Draft
from consultant.deliverables.validate import _known, validate_draft
from consultant.llm import StructuredOutputError
from consultant.tracing import BudgetExceeded

UNVERIFIED = "[unverified figure]"

REPAIR_SYS = (
    "You are the storyliner fixing a validation report. Each item below states a number that "
    "is not in the evidence it cites. For each item return its 'where' and the corrected "
    "fields (same field names as given): either cite the evidence id the number really comes "
    "from (it must appear there exactly AND that evidence must be for the same entity, metric "
    "and period the sentence states: a figure for another quarter or year is not support), "
    "or rewrite the sentence without that number. When unsure, remove the number. Change "
    "nothing else; never add a new number that is not in the evidence. If the problem is "
    "that the answer defers the decision, rewrite the answer as a committed recommendation "
    "drawn from the evidence (say what it depends on); collecting data belongs in the next "
    "steps, not in the answer.")


class Fix(BaseModel):
    """Flat on purpose: with a nested 'content' object gpt-oss-20b closed one brace too many
    on every try. Give only the fields the item has."""
    where: str = Field(description="the item's 'where', exactly as given")
    text: str | None = None
    title: str | None = None
    action_title: str | None = None
    points: list[str] | None = None
    items: list[str] | None = None
    refs: list[str] | None = None

    def content(self) -> dict:
        return {k: v for k, v in self.model_dump(exclude={"where"}).items() if v is not None}


class Fixes(BaseModel):
    fixes: list[Fix]


def _get(d: Draft, where: str) -> dict:
    kind, _, n = where.partition(" ")
    if where == "answer":
        return {"text": d.answer}
    if where == "confidence reason":
        return {"text": d.confidence_reason}
    if where == "would change":
        return {"items": d.would_change}
    if kind == "argument":
        a = d.arguments[int(n) - 1]
        return {"title": a.title, "text": a.text, "refs": a.refs}
    if kind == "slide":
        s = d.slides[int(n) - 1]
        return {"action_title": s.action_title, "points": s.points, "refs": s.refs}
    return {"items": d.risks if where == "risks" else d.next_steps}


def _set(d: Draft, where: str, c: dict) -> Draft:
    """Apply a fix, keeping only the fields the location has (bad shapes are ignored)."""
    d = d.model_copy(deep=True)
    old = _get(d, where)
    new = {k: c.get(k, v) for k, v in old.items()}
    if any(type(new[k]) is not type(old[k]) for k in old):
        return d
    kind, _, n = where.partition(" ")
    if where == "answer":
        d.answer = new["text"]
    elif where == "confidence reason":
        d.confidence_reason = new["text"]
    elif where == "would change":
        d.would_change = new["items"]
    elif kind == "argument":
        d.arguments[int(n) - 1] = d.arguments[int(n) - 1].model_copy(update=new)
    elif kind == "slide":
        d.slides[int(n) - 1] = d.slides[int(n) - 1].model_copy(update=new)
    elif where == "risks":
        d.risks = new["items"]
    else:
        d.next_steps = new["items"]
    return d


def _strip(text: str, numbers: list[str]) -> str:
    for num in numbers:
        text = re.sub(rf"(?<![\w.]){re.escape(num)}(?![\w])(\s?%)?", UNVERIFIED, text)
    return text


def _strip_location(d: Draft, where: str, numbers: list[str]) -> Draft:
    c = _get(d, where)
    out = {k: (_strip(v, numbers) if isinstance(v, str) else
               [_strip(x, numbers) for x in v] if k in ("points", "items") else v)
           for k, v in c.items()}
    return _set(d, where, out)


NUMBER_FLAGS = ("unsupported_number", "period_mismatch")  # a number shipped as fact


def _flags(ws, d: Draft) -> list[dict]:
    return [i for i in validate_draft(ws, d) if i["kind"] in (*NUMBER_FLAGS, "deferred_answer")]


def repair(llm, ws, d: Draft, evidence: str, role: str = "storyliner") -> tuple[Draft, list[str]]:
    """Returns the repaired draft and a note per number removed as unverified."""
    d = d.model_copy(deep=True)
    for part in [*d.arguments, *d.slides]:  # a ref naming no evidence supports nothing
        part.refs = [r for r in part.refs if _known(ws, r.strip())]
    flags = _flags(ws, d)
    if not flags:
        return d, []
    items = [{"where": f["where"], "problem": f["detail"], **_get(d, f["where"])}
             for f in flags]
    try:
        fixes = llm.structured(role, [
            {"role": "system", "content": REPAIR_SYS},
            {"role": "user", "content": f"ITEMS:\n{json.dumps(items, ensure_ascii=False)}\n\n"
                                        f"EVIDENCE:\n{evidence}"}], Fixes)
        flagged = {f["where"] for f in flags}
        for fx in fixes.fixes:
            if fx.where in flagged:  # only flagged locations may change
                d = _set(d, fx.where, fx.content())
    except (BudgetExceeded, StructuredOutputError):
        pass  # no budget or no usable fix: fall through to removal
    removed = []
    for f in [f for f in _flags(ws, d) if f["kind"] in NUMBER_FLAGS]:
        numbers = list(dict.fromkeys(f["numbers"]))
        d = _strip_location(d, f["where"], numbers)
        why = ("cited evidence is for another period" if f["kind"] == "period_mismatch"
               else "not in the evidence it cites")
        removed += [f"{n} in {f['where']} ({why})" for n in numbers]
    return d, removed
