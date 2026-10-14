"""Deliverable validator: every number in the memo/storyline must appear in evidence the same
argument or slide cites (a fact's value/quote/claim, an analysis output, an assumption), or
in the client's own question. Every slide needs an action title."""

import re

from consultant.agents.storyliner import Draft
from consultant.deliverables.model_xlsx import _flatten
from consultant.research.extract import numbers_in

_NUM = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)(?![\w])")
_REFS = re.compile(r"\b[FAS]\d+\b")
# fiscal-year labels (FY24, FY'24, FY2023-24, FY22‑23) name a period; they aren't data
_FY = re.compile(r"\bFY\s*['’]?\d{2,4}(?:\s*[-–—‑‐/]\s*\d{2,4})?\b", re.I)
GENERIC_TITLES = {"overview", "introduction", "background", "summary", "analysis",
                  "recommendation", "recommendations", "next steps", "agenda", "appendix"}


def ref_numbers(ws, refs: list[str]) -> list[float]:
    eid = ws.state.engagement_id
    facts = {f"F{f.id}": f for f in ws.facts.facts(eid)}
    assumptions = {f"S{a.id}": a for a in ws.facts.assumptions(eid)}
    analyses = {f"A{a.id[1:]}": a for a in ws.state.analyses}
    pool: list[float] = []
    for r in refs:
        r = r.strip()
        if r in facts:
            f = facts[r]
            pool += ([f.value] if f.value is not None else []) + numbers_in(f.quote) + \
                numbers_in(f.claim) + numbers_in(f.header)
        elif r in assumptions:
            pool.append(assumptions[r].value)
        elif r in analyses and analyses[r].ok:
            pool += list(_flatten(analyses[r].result).values())
    return pool


def _matches(shown: str, pool: list[float]) -> bool:
    d = float(shown.replace(",", ""))
    decimals = len(shown.split(".")[1]) if "." in shown else 0
    half = 0.5 * 10 ** -decimals + 1e-9
    for v in pool:
        for cand in (v, v * 100, v / 100):           # 0.158 shown as 15.8%, and the reverse
            if abs(d - cand) <= half or (cand and abs(d - cand) / abs(cand) <= 0.005):
                return True
    return False


def _strip_enumeration(text: str) -> str:
    """Remove list markers only when they count 1, 2, 3... so a real '2.' isn't skipped."""
    out, expected, last = [], 1, 0
    for m in re.finditer(r"(?:^|(?<=\s))\(?(\d{1,2})[.)]\s+(?=[A-Za-z])", text):
        if int(m.group(1)) == expected:
            out.append(text[last:m.start()])
            last, expected = m.end(), expected + 1
    return "".join(out) + text[last:]


def unsupported_numbers(text: str, pool: list[float]) -> list[str]:
    text = _strip_enumeration(_FY.sub(" ", _REFS.sub(" ", text)))
    out = []
    for m in _NUM.finditer(text):
        s = m.group(1)
        if re.fullmatch(r"(19|20)\d{2}", s):        # years
            continue
        if not _matches(s, pool):
            out.append(s)
    return out


def validate_draft(ws, d: Draft) -> list[dict]:
    question = ws.state.problem_statement + " " + str(ws.state.scope.get("restated_question", ""))
    context = numbers_in(question)
    all_refs = sorted({r for a in d.arguments for r in a.refs} | {r for s in d.slides
                                                                  for r in s.refs})
    every = ref_numbers(ws, all_refs) + context
    issues = []

    def check(where: str, text: str, refs: list[str], pool=None):
        bad = unsupported_numbers(text, pool if pool is not None else
                                  ref_numbers(ws, refs) + context)
        if bad:
            issues.append({"where": where, "kind": "unsupported_number", "numbers": bad,
                           "detail": f"{', '.join(bad)} not found in cited evidence {refs}"})
    check("answer", d.answer, all_refs, every)
    for i, a in enumerate(d.arguments, 1):
        check(f"argument {i}", f"{a.title} {a.text}", a.refs)
    for i, s in enumerate(d.slides, 1):
        check(f"slide {i}", " ".join([s.action_title, *s.points]), s.refs)
        t = s.action_title.strip()
        if len(t.split()) < 4 or t.lower().strip(" .:") in GENERIC_TITLES:
            issues.append({"where": f"slide {i}", "kind": "no_action_title",
                           "detail": f"'{t}' is a topic, not an insight"})
    for name, items in (("risks", d.risks), ("next steps", d.next_steps)):
        check(name, " ".join(items), all_refs, every)
    unknown = [r for r in all_refs if not ref_numbers(ws, [r]) and not _known(ws, r)]
    if unknown:
        issues.append({"where": "refs", "kind": "unknown_ref", "detail": f"unknown {unknown}"})
    return issues


def _known(ws, ref: str) -> bool:
    eid = ws.state.engagement_id
    return (ref.startswith("F") and ws.facts.get(int(ref[1:])) is not None) or \
        (ref.startswith("A") and any(a.id == ref for a in ws.state.analyses)) or \
        (ref.startswith("S") and any(a.id == int(ref[1:]) for a in ws.facts.assumptions(eid)))
