"""Deliverable validator: every number in the memo/storyline must appear in evidence the same
argument or slide cites (a fact's value/quote/claim, an analysis output, an assumption), or
in the client's own question, and a number taken from a fact must be for the period the
claim states. The answer must commit. Every slide needs an action title."""

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


# The answer must commit. ponytail: phrase patterns catch the usual deferrals ("cannot
# conclusively recommend", "collect data before deciding", an answer that is only a data
# request); a paraphrase can slip through: the partner's non_sequitur check is the backstop.
_DEFERRAL = re.compile(
    r"\b(?:cannot|can ?not|can't|unable to|not possible to|too early to)\b[^.]{0,60}?"
    r"\b(?:recommend|conclude|decide|determine|commit)"
    r"|\b(?:before|until)\b[^.]{0,50}?\b(?:decid|recommend|commit|choos)"
    r"|^\W*(?:we recommend that |the client should |recommendation: )?(?:first )?"
    r"(?:collect|gather|obtain|conduct|commission)\b[^.]{0,80}?\b(?:data|analysis|study|"
    r"research|information)\b[^.]*\.?\s*$", re.I)


# A period stated in a claim: FY24, Q4 FY24, FY2023-24, 2023-24, 2024.
_PERIOD = re.compile(r"\b(?:Q[1-4]\s*)?FY\s*['’]?\d{2,4}(?:\s*[-–—‑‐/]\s*\d{2,4})?\b"
                     r"|\b(?:19|20)\d{2}(?:\s*[-–—‑‐/]\s*\d{2,4})?\b", re.I)
# Sub-year periods. Month names are not used: "Mar 2024" often labels a full fiscal year.
_SUB_YEAR = re.compile(r"\bQ[1-4]\b|\bH[12]\b|quarter|\b\d+M\b|\bmonths?\b", re.I)


def _segments(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+", text) if s.strip()]


def period_mismatches(ws, segments: list[str], refs: list[str]) -> list[tuple[str, str]]:
    """(number, why) for numbers whose cited fact is for another period than the claim states
    (e.g. a Jul 2026 quarterly margin cited for FY24). Only facts whose own value is the
    number count (a number elsewhere in a table-row quote has no known column); a claim with
    no stated period isn't checked; client-interview facts carry no period and are skipped."""
    from consultant.research.triangulate import period_key
    facts = {f"F{f.id}": f for f in ws.facts.facts(ws.state.engagement_id)}
    cited = [facts[r.strip()] for r in refs if r.strip() in facts
             and facts[r.strip()].value is not None]
    out = []
    for seg in segments:
        stated = {period_key(m.group()) for m in _PERIOD.finditer(seg)}
        if not stated or not cited:
            continue
        annual = not _SUB_YEAR.search(seg)

        def fits(f, stated=stated, annual=annual) -> bool:
            key = period_key(f.period or "")
            if not key.isdigit():
                return not f.source_url.startswith("http")
            return key in stated and not (annual and _SUB_YEAR.search(f.period))
        for m in _NUM.finditer(_FY.sub(" ", _REFS.sub(" ", seg))):
            n = m.group(1)
            hits = [f for f in cited if _matches(n, [f.value])]
            if hits and not any(fits(f) for f in hits) and not re.fullmatch(r"(19|20)\d{2}", n):
                out.append((n, ", ".join(f"F{f.id} is for '{f.period or 'no stated period'}'"
                                         for f in hits)
                            + f", the claim is for {'/'.join(sorted(stated))}"
                            + ("" if not annual else " (full year)")))
    return out


def defers(answer: str) -> bool:
    """True if the answer postpones the decision instead of recommending something."""
    return bool(_DEFERRAL.search(answer.strip()))


def validate_draft(ws, d: Draft) -> list[dict]:
    question = ws.state.problem_statement + " " + str(ws.state.scope.get("restated_question", ""))
    context = numbers_in(question)
    all_refs = sorted({r for a in d.arguments for r in a.refs} | {r for s in d.slides
                                                                  for r in s.refs}
                      | set(d.key_evidence))
    every = ref_numbers(ws, all_refs) + context
    issues = []

    def check(where: str, text: str, refs: list[str], pool=None, segs=None):
        bad = unsupported_numbers(text, pool if pool is not None else
                                  ref_numbers(ws, refs) + context)
        if bad:
            issues.append({"where": where, "kind": "unsupported_number", "numbers": bad,
                           "detail": f"{', '.join(bad)} not found in cited evidence {refs}"})
        if wrong := period_mismatches(ws, segs if segs is not None else _segments(text), refs):
            issues.append({"where": where, "kind": "period_mismatch",
                           "numbers": [n for n, _ in wrong],
                           "detail": "; ".join(f"{n}: {why}" for n, why in wrong)})
    check("answer", d.answer, all_refs, every)
    if defers(d.answer):
        issues.append({"where": "answer", "kind": "deferred_answer",
                       "detail": "the answer defers the decision (e.g. 'collect data before "
                                 "deciding'); commit to the best-supported recommendation and "
                                 "move data collection to the next steps"})
    check("confidence reason", d.confidence_reason, all_refs, every)
    check("would change", " ".join(d.would_change), all_refs, every, d.would_change)
    for i, a in enumerate(d.arguments, 1):
        check(f"argument {i}", f"{a.title} {a.text}", a.refs)
    for i, s in enumerate(d.slides, 1):
        check(f"slide {i}", " ".join([s.action_title, *s.points]), s.refs, None,
              [s.action_title, *s.points])
        t = s.action_title.strip()
        if len(t.split()) < 4 or t.lower().strip(" .:") in GENERIC_TITLES:
            issues.append({"where": f"slide {i}", "kind": "no_action_title",
                           "detail": f"'{t}' is a topic, not an insight"})
    for name, items in (("risks", d.risks), ("next steps", d.next_steps)):
        check(name, " ".join(items), all_refs, every, items)
    unknown = [r for r in all_refs if not ref_numbers(ws, [r]) and not _known(ws, r)]
    if unknown:
        issues.append({"where": "refs", "kind": "unknown_ref", "detail": f"unknown {unknown}"})
    return issues


def _known(ws, ref: str) -> bool:
    eid = ws.state.engagement_id
    return (ref.startswith("F") and ws.facts.get(int(ref[1:])) is not None) or \
        (ref.startswith("A") and any(a.id == ref for a in ws.state.analyses)) or \
        (ref.startswith("S") and any(a.id == int(ref[1:]) for a in ws.facts.assumptions(eid)))
