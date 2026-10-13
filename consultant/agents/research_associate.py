"""Research Associate: one research question -> sourced facts -> brief with confidence.

Loop: plan queries -> search -> read the most credible results -> extract validated facts ->
gap check -> next query, until answered or a cap is hit. Hard caps per question on searches,
pages and remote tokens; a token reserve guarantees the brief can still be written.
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from consultant.llm import LLM
from consultant.research.credibility import classify, credibility, domain
from consultant.research.estimate import Estimate, estimate
from consultant.research.extract import extract_facts
from consultant.research.fact_store import Fact, FactStore
from consultant.research.triangulate import Finding, period_key, triangulate
from consultant.research.web import Web
from consultant.tracing import Budget, BudgetExceeded

DEFAULT_CAPS = {"searches": 4, "pages": 8, "tokens": 30000}
BRIEF_RESERVE = 4000   # remote tokens kept back for the final brief
PAGES_PER_QUERY = 3     # successful reads per query
TRIES_PER_QUERY = 5     # results attempted per query (blocked sources fail fast)
MAX_FACTS_SHOWN = 25


class SearchPlan(BaseModel):
    queries: list[str] = Field(min_length=2, max_length=3,
                               description="2-3 web search queries, most specific first")


class GapCheck(BaseModel):
    answered: bool = Field(description="do the facts answer the question directly?")
    missing: str
    next_query: str | None = Field(description="the next search query, or null")


class Brief(BaseModel):
    answer: str = Field(description="1-3 sentences; cite facts as [F<id>]")
    value: float | None = Field(description="headline number exactly as in the cited fact")
    unit: str
    period: str
    fact_ids: list[int]
    confidence: Literal["low", "medium", "high"]
    gaps: str


@dataclass
class ResearchResult:
    question: str
    brief: Brief
    facts: list[Fact]
    findings: list[Finding]
    estimate: Estimate | None = None
    spent: dict = field(default_factory=dict)
    rejected_facts: int = 0


PLAN_SYS = ("You are a research associate at a strategy consultancy. Write web search queries "
            "that will find authoritative sources (annual reports, regulators, government "
            "statistics, established business press) for the question. One query should find "
            "the primary source's short results announcement (the company's results press "
            "release, or the ministry/regulator's press note) rather than a full annual "
            "report; another should find established business-press coverage of the figure. "
            "Write plain keyword queries: no quotation marks and no site: operators.")
GAP_SYS = ("Judge whether the facts found so far answer the research question directly "
           "(right entity, metric and period; a fact marked period=MISMATCH does not count). "
           "If not, propose ONE better plain keyword search query (no quotes, no site:).")
BRIEF_SYS = ("Write a short research brief answering the question from the facts only. Cite "
             "facts as [F<id>]. Prefer filings/government sources over press, press over "
             "others. Use only facts for the entity, metric and period asked; facts marked "
             "period=MISMATCH are a different year: never report them as the answer. If no "
             "fact matches, set value to null and say what is missing. When a quote is a "
             "table row, read the header to confirm the column year. If sources disagree, say "
             "so and give the range. Confidence: high = "
             "authoritative source or 2+ independent agreeing sources; medium = one decent "
             "source; low = weak, indirect or no evidence.")


def period_flag(fact: Fact, question: str) -> str:
    want = period_key(question)
    if not want.isdigit():
        return ""
    return "" if period_key(fact.period) == want else "=MISMATCH"


def facts_table(facts: list[Fact], question: str = "") -> str:
    def rank(f: Fact):
        return (period_flag(f, question) != "", -f.credibility)
    rows = []
    for f in sorted(facts, key=rank)[:MAX_FACTS_SHOWN]:
        row = (f"[F{f.id}] {f.claim} | value={f.value} {f.unit} | "
               f"period={f.period}{period_flag(f, question)} | "
               f"{f.source_type} {domain(f.source_url)}")
        if f.header:
            row += f" | table header: {f.header[:200]}"
        rows.append(row)
    return "\n".join(rows)


def brief_problem(brief: Brief, facts: list[Fact], question: str) -> str | None:
    """A reported value must equal a cited fact for the asked period (deterministic check)."""
    if brief.value is None:
        return None
    by_id = {f.id: f for f in facts}
    cited = [by_id[i] for i in brief.fact_ids if i in by_id]
    if not cited:
        return "the value cites no known fact"
    ok = [f for f in cited if f.value is not None and not period_flag(f, question)
          and abs(f.value - brief.value) <= 0.005 * max(abs(f.value), 1e-9)]
    if ok:
        return None
    if all(period_flag(f, question) for f in cited):
        return "every cited fact is for a different period (MISMATCH)"
    return "the value does not equal any cited fact for the asked period"


class ResearchAssociate:
    def __init__(self, llm: LLM, web: Web, store: FactStore, engagement_id: str,
                 caps: dict | None = None, role: str = "research_associate",
                 extractor_role: str = "extractor", allow_estimate: bool = True):
        self.llm, self.web, self.store = llm, web, store
        self.engagement_id, self.caps = engagement_id, {**DEFAULT_CAPS, **(caps or {})}
        self.role, self.extractor_role = role, extractor_role
        self.allow_estimate = allow_estimate

    def _ask(self, system: str, user: str, schema):
        return self.llm.structured(self.role, [{"role": "system", "content": system},
                                               {"role": "user", "content": user}], schema)

    def research(self, question: str, hypothesis_id: str | None = None,
                 context: str = "") -> ResearchResult:
        budget = Budget(self.caps)
        facts: list[Fact] = []
        rejected = 0
        with self.llm.scoped(budget):
            plan = self._ask(PLAN_SYS, f"QUESTION: {question}\n{context}", SearchPlan)
            queue, tried, seen = list(plan.queries[:3]), set(), set()
            while queue and (budget.remaining("tokens") or 0) > BRIEF_RESERVE:
                q = queue.pop(0)
                if q in tried:
                    continue
                tried.add(q)
                try:
                    results = self.web.search(q, budget=budget)
                except BudgetExceeded:
                    break
                ranked = sorted(results, key=lambda r: -credibility(r.url))  # stable sort
                read = 0
                for r in [r for r in ranked if r.url not in seen][:TRIES_PER_QUERY]:
                    if read >= PAGES_PER_QUERY:
                        break
                    seen.add(r.url)
                    try:
                        page = self.web.fetch(r.url, budget=budget)
                    except BudgetExceeded:
                        queue.clear()
                        break
                    if page.error or len(page.text) < 200:
                        continue  # blocked/failed sources are common; try the next result
                    read += 1
                    found, rej = extract_facts(self.llm, page, question, self.extractor_role)
                    rejected += rej
                    for pf in found:
                        fact = Fact(engagement_id=self.engagement_id, source_url=page.url,
                                    source_title=page.title, source_date=page.date,
                                    source_type=classify(page.url),
                                    credibility=credibility(page.url),
                                    hypothesis_ids=[hypothesis_id] if hypothesis_id else [],
                                    **pf.model_dump())
                        fact.id = self.store.add(fact)
                        facts.append(fact)
                if (budget.remaining("tokens") or 0) <= BRIEF_RESERVE:
                    break
                table = facts_table(facts, question) or "(none yet)"
                gap = self._ask(GAP_SYS, f"QUESTION: {question}\nQUERIES TRIED: {sorted(tried)}"
                                         f"\nFACTS:\n{table}", GapCheck)
                usable = [f for f in facts if f.value is not None and not period_flag(f, question)]
                if gap.answered and usable:  # never stop on 'answered' with nothing to cite
                    break
                if gap.next_query:
                    queue.append(gap.next_query)
            prompt = (f"QUESTION: {question}\nFACTS:\n"
                      f"{facts_table(facts, question) or '(none found)'}")
            brief = self._ask(BRIEF_SYS, prompt, Brief)
            if problem := brief_problem(brief, facts, question):
                try:  # one corrective retry, then fail safe below
                    brief = self._ask(BRIEF_SYS, f"{prompt}\n\nYOUR PREVIOUS ANSWER WAS "
                                                 f"REJECTED: {problem}", Brief)
                except BudgetExceeded:
                    pass
            if problem := brief_problem(brief, facts, question):
                brief.gaps = f"{brief.gaps} [value withheld: {problem}]".strip()
                brief.value, brief.confidence = None, "low"
            known = {f.id for f in facts}
            brief.fact_ids = [i for i in brief.fact_ids if i in known]
            if not brief.fact_ids:
                brief.confidence = "low"
            est = None
            if self.allow_estimate and brief.value is None:
                try:
                    est = estimate(self.llm, self.store, self.engagement_id, question,
                                   facts_table(facts, question), self.role)
                except BudgetExceeded:
                    est = None  # out of budget: the brief already says what's missing
        return ResearchResult(question, brief, facts, triangulate(facts), est,
                              dict(budget.spent), rejected)
