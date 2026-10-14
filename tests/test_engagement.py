"""Phase 5 offline: the full engagement loop in case mode with a scripted LLM, the real
simulator, sandbox, fact store and workspace. Fictional bakery case."""

import re
from contextlib import contextmanager

import pytest

from consultant.agents.engagement_manager import Engagement
from consultant.agents.partner import Review, review
from consultant.evals.simulator import Interviewer, Match
from consultant.knowledge.retrieval import Retriever
from consultant.research.extract import numbers_in
from consultant.tracing import BudgetExceeded
from consultant.workspace import Workspace
from tests.test_simulator import BAKERY

KEYWORDS = {"profit": 0, "sell": 1, "constraint": 2, "cost": 4, "price": 5, "butter": 6,
            "footfall": 7}


class ScriptedLLM:
    """Returns canned outputs per schema; records calls. `fail_on` raises BudgetExceeded."""

    def __init__(self, fail_on: str | None = None, forbid_calls: bool = False):
        self.calls: list[str] = []
        self.reviews = 0
        self.fail_on, self.forbid = fail_on, forbid_calls

    @contextmanager
    def scoped(self, budget):
        yield

    def structured(self, role, msgs, schema):
        name = schema.__name__
        if self.forbid:
            raise AssertionError(f"unexpected LLM call on resume: {name}")
        self.calls.append(name)
        if name == self.fail_on:
            raise BudgetExceeded("tokens budget: scripted")
        user = msgs[-1]["content"]
        return schema(**getattr(self, name)(user))

    def Match(self, user):
        q = user.split("CANDIDATE:")[-1].lower()
        return {"reasoning": "", "item_ids": [i for k, i in KEYWORDS.items() if k in q][:1]}

    def Scope(self, user):
        return {"restated_question": "Why have the bakery's profits fallen, and what should it do?",
                "objective": "restore profit", "success_metric": "profit back to prior level",
                "horizon": "18 months", "constraints": [], "client_prior":
                "customers are leaving", "clarifying_questions":
                ["How much have profits dropped, and by when must it be fixed?",
                 "Where does the client sell and what does it sell?"],
                "case_type": "profitability", "industry": "Food retail", "entity": "Bakery"}

    def PageFacts(self, user):
        excerpt = user.split("EXCERPT:\n", 1)[1]
        part = next(p for p in re.split(r"[;.]\s", excerpt) if numbers_in(p))
        return {"facts": [{"claim": part, "metric": "case_number", "value": numbers_in(part)[0],
                           "unit": "", "entity": "Bakery", "period": "", "quote": part}]}

    def TreePlan(self, user):
        return {"nodes": [{"id": "T", "text": "Profit", "parent": None},
                          {"id": "T1", "text": "Revenue", "parent": "T"},
                          {"id": "T2", "text": "Costs", "parent": "T"}],
                "hypotheses": [
                    {"id": "H1", "statement": "Ingredient costs rose", "branch_id": "T2",
                     "priority": 1, "test": "cost lines over time",
                     "questions": ["How did each cost line change?", "How is butter bought?"]},
                    {"id": "H2", "statement": "Prices lag competitors", "branch_id": "T1",
                     "priority": 1, "test": "price benchmark",
                     "questions": ["How do our prices compare with rivals?"]},
                    {"id": "H3", "statement": "Customers are leaving", "branch_id": "T1",
                     "priority": 2, "test": "footfall trend",
                     "questions": ["Has footfall changed?"]}]}

    def Assessment(self, user):
        return {"updates": [
                    {"id": "H1", "status": "supported", "evidence": ["F3", "F999"],
                     "reasoning": "ingredients +30%"},
                    {"id": "H2", "status": "supported", "evidence": ["F5"], "reasoning": "x"},
                    {"id": "H3", "status": "killed", "evidence": ["F1"], "reasoning": "steady"}],
                "analyses_needed": ["Count the quantitative facts available"],
                "next_questions": [], "new_hypotheses": [], "done": True}

    def AnalysisCode(self, user):
        return {"name": "fact count", "purpose": "audit",
                "code": "RESULT = {'n_numeric_facts': len(FACTS)}"}

    def Draft(self, user):
        return {"answer": "Unhedged butter costs and frozen prices cut profit.",
                "arguments": [{"title": "Ingredient costs drove the decline",
                               "text": "Costs rose.", "refs": ["F3", "A1"]}],
                "risks": ["Competitors may cut prices"], "next_steps": ["Contract butter"],
                "open_questions": [], "confidence": "medium",
                "slides": [{"action_title": "Butter costs, not customers, cut profit",
                            "points": ["costs up"], "refs": ["F3"]}]}

    def Review(self, user):
        self.reviews += 1
        major = self.reviews == 1
        return {"issues": [{"kind": "unsupported_claim", "severity": "major", "where": "arg 1",
                            "detail": "no number"}] if major else [],
                "strongest_counterargument": "maybe footfall", "ceo_questions": ["a", "b", "c"],
                "agrees_with_client_prior": False, "prior_agreement_justified": False,
                "verdict": "revise" if major else "approve"}


def engagement(tmp_path, llm, eid="e1"):
    ws = Workspace.open(eid, root=tmp_path, mode="case", profile="dev")
    iv = Interviewer(BAKERY, llm)
    return Engagement(llm, ws, interviewer=iv, retriever=Retriever([])), ws, iv


def test_case_mode_engagement_end_to_end(tmp_path):
    llm = ScriptedLLM()
    eng, ws, iv = engagement(tmp_path, llm)
    out = eng.run()
    st = ws.state
    assert st.steps_done == ["scope", "plan", "iter0", "story0", "review0", "story1",
                             "review1", "repair", "deliver"]
    assert st.problem_statement == BAKERY.problem_statement and st.case_type == "profitability"
    # clarifying + hypothesis questions went to the client; answers stored as sourced facts
    assert len(iv.log) >= 4
    facts = ws.facts.facts("e1")
    assert facts and all(f.source_url.startswith("case://") and f.verified for f in facts)
    numeric = [f for f in facts if f.value is not None]
    replies = " ".join(e["reply"] for e in iv.log)
    assert numeric and all(f.quote in replies for f in numeric)  # numbers quoted verbatim
    h1 = ws.hypothesis("H1")
    assert h1.status == "supported" and 999 not in h1.evidence_fact_ids  # unknown id dropped
    assert ws.hypothesis("H3").status == "killed"
    assert st.analyses[0].ok and st.analyses[0].result["n_numeric_facts"] >= 1
    assert llm.reviews == 2 and out["draft"].answer.startswith("Unhedged")
    memo = (ws.dir / "deliverables/memo.md").read_text(encoding="utf-8")
    assert "## Answer" in memo and "Unhedged butter costs" in memo and "## Risks" in memo



def test_failed_analysis_does_not_crash_the_engagement(tmp_path):
    from consultant.llm import StructuredOutputError

    class BadAnalyst(ScriptedLLM):
        def AnalysisCode(self, user):
            raise StructuredOutputError("analyst: no valid AnalysisCode after 3 tries")
    eng, ws, _ = engagement(tmp_path, BadAnalyst())
    eng.run()
    assert "deliver" in ws.state.steps_done and not ws.state.analyses
    assert any("analysis failed" in d.decision for d in ws.state.decisions)

def test_resume_after_completion_makes_no_llm_calls(tmp_path):
    eng, ws, _ = engagement(tmp_path, ScriptedLLM())
    eng.run()
    again, ws2, _ = engagement(tmp_path, ScriptedLLM(forbid_calls=True))
    again.run()                                  # every step is done: replayed from disk
    assert ws2.state.steps_done == ws.state.steps_done


def test_budget_cap_still_delivers_with_open_question(tmp_path):
    llm = ScriptedLLM(fail_on="Assessment")
    eng, ws, _ = engagement(tmp_path, llm, eid="capped")
    out = eng.run()
    assert out["stopped"] and "deliver" in ws.state.steps_done
    assert any("budget cap" in q for q in ws.state.open_questions)
    assert any(d.actor == "system" for d in ws.state.decisions)


def test_partner_flags_unjustified_agreement_with_client_prior():
    class L:
        def structured(self, role, msgs, schema):
            return Review(issues=[], strongest_counterargument="", ceo_questions=[],
                          agrees_with_client_prior=True, prior_agreement_justified=False,
                          verdict="approve")
    r = review(L(), "q", "it's the competition", "draft", "evidence")
    assert r.verdict == "revise" and r.blocking()[0].kind == "client_pleasing"


def test_case_mode_requires_interviewer(tmp_path):
    ws = Workspace.create("x", root=tmp_path, mode="case")
    with pytest.raises(ValueError):
        Engagement(ScriptedLLM(), ws, retriever=Retriever([]))


def test_interviewer_match_schema_used(tmp_path):
    iv = Interviewer(BAKERY, ScriptedLLM())
    assert iv.ask("How do our prices compare with rivals?") == BAKERY.data_reveals[2].data
    assert Match  # schema shared with the simulator


class RealModeLLM(ScriptedLLM):
    """Adds the Research Associate's schemas on top of the case-mode script."""

    def SearchPlan(self, user):
        return {"queries": ["acme revenue fy2024", "acme annual report 2024"]}

    def GapCheck(self, user):
        return {"answered": True, "missing": "", "next_query": None}

    def Brief(self, user):
        ids = [int(i) for i in re.findall(r"\[F(\d+)\]", user)][:1]
        return {"answer": f"Rs 1,200 crore [F{ids[0]}]" if ids else "not found",
                "value": 1200 if ids else None, "unit": "crore", "period": "FY2024",
                "fact_ids": ids, "confidence": "medium", "gaps": ""}

    def Entailment(self, user):
        return {"supports": True, "reason": "stated"}

    def EstimatePlan(self, user):
        return {"approach": "top_down", "unit": "INR crore", "formula": "outlets * revenue",
                "factors": [{"name": "outlets", "value": 12, "unit": "count",
                             "rationale": "assumed"},
                            {"name": "revenue", "value": 2, "unit": "INR crore",
                             "rationale": "assumed per outlet"}]}

    def PageFacts(self, user):
        if "reported revenue" in user:
            q = "Acme Ltd reported revenue from operations of Rs 1,200 crore in FY2024"
        elif "posted revenue" in user:
            q = "Acme Ltd posted revenue of Rs 1,201 crore in FY2024"
        else:
            return {"facts": []}
        return {"facts": [{"claim": "Acme revenue FY2024", "metric": "revenue", "value":
                           1200 if "1,200" in q else 1201, "unit": "Rs crore",
                           "entity": "Acme Ltd", "period": "FY2024", "quote": q}]}


def test_real_mode_engagement_researches_verifies_and_extends_primers(tmp_path):
    import httpx

    from consultant.research.web import Web
    from tests.test_research import Provider, site
    site.hits = []
    llm = RealModeLLM()
    web = Web(Provider(), cache_dir=tmp_path / "w", sleep=lambda s: None,
              client=httpx.Client(transport=httpx.MockTransport(site)))
    ws = Workspace.create("real1", root=tmp_path, mode="real", profile="dev",
                          problem_statement="Acme's profits fell; what should it do?")
    import consultant.knowledge.retrieval as r
    added = []
    r_add = r.add_primer
    import consultant.agents.engagement_manager as em
    em.add_primer = lambda *a, **k: added.append(a)
    try:
        out = Engagement(llm, ws, web=web, retriever=Retriever([])).run()
    finally:
        em.add_primer = r_add
    st = ws.state
    assert "verify" in st.steps_done and "deliver" in st.steps_done
    facts = ws.facts.facts("real1")
    assert facts and all(f.source_url.startswith("https://") for f in facts)
    assert ws.facts.eligible("real1")                     # verified on re-fetch
    assert added and added[0][0] == "Food retail"          # unknown industry -> new primer
    assert any("(unanswered scoping)" in q for q in st.open_questions)  # no client to ask
    assert out["deliverables"]["memo"].endswith("memo.md")


def test_partner_eval_scoring_is_strict(tmp_path):
    from consultant.evals import partner_eval

    class FakePartner:
        """Names the planted flaw for 4 kinds; for maths it only says 'revise' generically."""
        def structured(self, role, msgs, schema):
            draft = msgs[-1]["content"].lower()
            kind = next((k for k, cue in [("unsupported_claim", "market share fell"),
                                         ("maths", "25% increase"),
                                         ("non_sequitur", "open five new outlets"),
                                         ("missing_branch", "renegotiate leases"),
                                         ("client_pleasing", "as the client suspected")]
                         if cue in draft), None)
            issues = []
            if kind and kind != "maths":
                issues = [{"kind": kind, "severity": "major", "where": "x", "detail": "y"}]
            elif kind == "maths":
                issues = [{"kind": "other", "severity": "minor", "where": "x", "detail": "y"}]
            return Review(issues=issues, strongest_counterargument="", ceo_questions=[],
                          agrees_with_client_prior=False, prior_agreement_justified=True,
                          verdict="revise" if kind else "approve")
    import consultant.evals.partner_eval as pe
    pe.ROOT = tmp_path
    out = partner_eval.run(FakePartner())
    assert out["caught"] == 4 and out["pass"] and out["clean_verdict"] == "approve"
    assert [r["kind"] for r in out["rows"] if not r["caught"]] == ["maths"]


def test_run_case_produces_judged_result(tmp_path):
    from consultant.evals import run_system
    from consultant.evals.judge import RUBRIC

    class Judged(ScriptedLLM):
        def Verdict(self, user):
            assert "CANDIDATE TRANSCRIPT" in user and "Unhedged butter" in user
            return {k: {"justification": "ok", "score": 3} for k in RUBRIC}
    import consultant.agents.engagement_manager as em
    orig = em.Retriever.load
    em.Retriever.load = classmethod(lambda cls, *a, **k: cls([]))
    try:
        row = run_system.run_case(Judged(), BAKERY, tmp_path, "dev")
    finally:
        em.Retriever.load = orig
    assert row["overall"] == 3 and row["case_id"] == BAKERY.id and not row["stopped"]
    assert row["code_version"]
    ws = Workspace.load(f"case-{BAKERY.id}", root=tmp_path)
    assert ws.state.notes["interview_log"]      # persisted for resume


def test_token_estimate_is_consistent():
    from consultant.evals.token_estimate import estimate, phase7_days
    dev, full = estimate("dev", "case"), estimate("full", "case")
    assert dev["total"] == sum(dev["per_model"].values()) > 10_000
    assert full["total"] > dev["total"]
    assert estimate("dev", "real")["total"] > dev["total"] - 20_000   # RA replaces interviewer
    d5, d15 = phase7_days(n_cases=5), phase7_days(n_cases=15)
    assert d15["days"] > d5["days"] > 0


def test_estimates_alone_cannot_settle_a_hypothesis(tmp_path):
    class Ungrounded(ScriptedLLM):
        def Assessment(self, user):
            out = super().Assessment(user)
            out["updates"] = [{"id": "H1", "status": "supported", "evidence": ["S1"],
                               "reasoning": "an estimate"},
                              {"id": "H3", "status": "killed", "evidence": [],
                               "reasoning": "no evidence"}]
            return out
    eng, ws, _ = engagement(tmp_path, Ungrounded(), eid="ungrounded")
    eng.run()
    assert ws.hypothesis("H1").status == "needs_more"
    assert ws.hypothesis("H3").status == "needs_more"


def test_synthesis_budget_stop_delivers_latest_draft_with_unresolved_points(tmp_path):
    class TightSynthesis(ScriptedLLM):
        def structured(self, role, msgs, schema):
            if schema.__name__ == "Draft" and self.reviews >= 1:
                raise BudgetExceeded("tokens budget: scripted synthesis cap")
            return super().structured(role, msgs, schema)
    eng, ws, _ = engagement(tmp_path, TightSynthesis(), eid="tight")
    out = eng.run()
    assert "deliver" in ws.state.steps_done and out["stopped"]
    memo = (ws.dir / "deliverables/memo.md").read_text(encoding="utf-8")
    assert "Unresolved review points" in memo and "no number" in memo
    assert any("Synthesis stopped" in q for q in ws.state.open_questions)



def test_synthesis_gets_the_reserve_plus_unspent_investigation_tokens(tmp_path):
    # system-train: investigation used 23-34K of its 42K share while synthesis stopped at its
    # fixed 18K, though the runs were far under the 60K run cap
    cap = 60000
    eng, ws, _ = engagement(tmp_path, ScriptedLLM(), eid="lend")
    eng.investigate.charge("tokens", 25000)
    eng.run()
    assert eng.synthesis.limits["tokens"] == cap - 25000      # total still capped at 60K
    eng, ws, _ = engagement(tmp_path, ScriptedLLM(), eid="reserve")
    eng.investigate.charge("tokens", 50000)
    eng.run()
    assert eng.synthesis.limits["tokens"] == cap - int(cap * 0.7)  # reserve always kept

def test_research_questions_get_engagement_context(tmp_path, monkeypatch):
    import consultant.agents.engagement_manager as em
    seen = []

    class FakeRA:
        def __init__(self, *a, **k):
            pass

        def research(self, q, hypothesis_id=None, context=""):
            seen.append(context)
            from consultant.agents.research_associate import Brief, ResearchResult
            return ResearchResult(q, Brief(answer="", value=None, unit="", period="",
                                           fact_ids=[], confidence="low", gaps=""), [], [],
                                  spent={"tokens": 10})
    monkeypatch.setattr(em, "ResearchAssociate", FakeRA)
    ws = Workspace.create("ctx", root=tmp_path, mode="real", problem_statement="p",
                          scope={"entity": "Acme Ltd", "industry": "Paints",
                                 "restated_question": "Why did Acme's margin change?"})
    e = Engagement(ScriptedLLM(), ws, web=object(), retriever=Retriever([]))
    e._research(["What was the EBITDA margin in FY2023-24?"])
    assert seen and "Acme Ltd" in seen[0] and "Paints" in seen[0]


def test_partner_fixture_numbers_are_internally_consistent():
    # The clean draft used to say profits fell 20% while its own evidence implied -60%
    # (revenue 24, costs 19 -> 22); the partner rightly rejected it.
    import re

    import yaml

    from consultant.evals.partner_eval import SEEDED
    s = yaml.safe_load(SEEDED.read_text(encoding="utf-8"))
    acc = s["accounts"]
    for y in (0, 1):
        assert acc["revenue"][y] - acc["costs"][y] == acc["profit"][y]
        assert abs(sum(v[y] for v in acc["cost_lines"].values()) - acc["costs"][y]) < 1e-9
    pairs = {tuple(acc[k]) for k in ("revenue", "costs", "profit")}

    def pct(a, b):
        return (b - a) / a * 100
    # every "% change" stated for a cost line in the evidence matches the accounts
    for line, (a, b) in acc["cost_lines"].items():
        stated = re.search(rf"{line}[^;|]*?Rs {a:g} crore ([+-]?\d+)%|{line}[^;|]*?flat",
                           s["evidence"])
        assert stated, line
        assert stated.group(1) is None or abs(float(stated.group(1)) - pct(a, b)) < 0.5, line
    assert re.search(rf"Profits fell {-pct(*acc['profit']):g}%", s["evidence"])
    a1 = re.search(r"'base_cost_cr': (\S+), 'current_cost_cr': (\S+), 'increase_pct': (\S+)}",
                   s["evidence"])
    assert (float(a1[1]), float(a1[2])) == tuple(acc["costs"])
    assert abs(float(a1[3]) - pct(*acc["costs"])) < 0.01
    # every "from Rs A to Rs B" names a real pair; a % stated with it must be right,
    # except in the draft seeded with the maths flaw, where it must be wrong
    move = re.compile(r"from Rs (\d+(?:\.\d+)?)(?: crore)? to Rs (\d+(?:\.\d+)?) crore"
                      r"(?:[ ,]*\(|, an? )?(-?\d+(?:\.\d+)?)?%?")
    texts = [("evidence", s["evidence"]), ("clean", s["clean"])] + \
        [(f["kind"], f["draft"]) for f in s["flawed"]]
    for name, text in texts:
        for m in move.finditer(text):
            a, b = float(m[1]), float(m[2])
            assert (a, b) in pairs, (name, m[0])
            if m[3] is not None:
                right = abs(abs(float(m[3])) - abs(pct(a, b))) < 0.5
                assert right == (name != "maths"), (name, m[0])
    maths = next(f["draft"] for f in s["flawed"] if f["kind"] == "maths")
    assert any(m[3] for m in move.finditer(maths))  # the seeded flaw is actually present
