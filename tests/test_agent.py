"""Offline tests for the single agent (consultant/agent.py): the loop and each action, the
write-up with citations, validator + repair, deliverables, the partner flag, real mode,
caps, kill-and-resume and the eval runner. Fictional bakery case, scripted LLM."""

import pytest

from consultant import agent as agent_mod
from consultant.agent import Agent, system_prompt
from consultant.agents.research_associate import Brief, ResearchResult
from consultant.evals.simulator import Interviewer
from consultant.llm import QuotaExhausted
from consultant.research.fact_store import Fact
from consultant.workspace import Workspace
from tests.test_engagement import ScriptedLLM
from tests.test_simulator import BAKERY

URL = "https://example.org/pune-bakeries-2025"


class AgentLLM(ScriptedLLM):
    """Scripted agent turns; the write-up cites the client fact, the analysis and the
    estimate, plus one number (37) that is in no evidence: the repair pass must remove it."""

    def __init__(self, turns, die_at: int | None = None, **kw):
        super().__init__(**kw)
        self.turns, self.prompts, self.story_inputs, self.matched = list(turns), [], [], []
        self.die_at = die_at

    def structured(self, role, msgs, schema):
        if schema.__name__ == "AgentTurn":
            if self.forbid:
                raise AssertionError("unexpected agent turn on resume")
            if self.die_at is not None and len(self.prompts) == self.die_at:
                raise QuotaExhausted("scripted kill")
            self.prompts.append(msgs)
            assert role == "agent"
            return schema(**self.turns.pop(0))
        if schema.__name__ == "Match":
            self.matched.append(msgs[-1]["content"])
        return super().structured(role, msgs, schema)

    def StoryDraft(self, user):
        self.story_inputs.append(user)
        return {"answer": "Raise cake prices about 10% and contract butter supply.",
                "arguments": [{"title": "Profits fell because costs rose while prices froze",
                               "text": "Profits fell 20% and costs grew 15.8%.",
                               "refs": ["F1", "A1"]},
                              {"title": "A price rise in line with rivals restores margin",
                               "text": "A 10% rise is feasible; margins would recover 37%.",
                               "refs": ["S1"]}],
                "risks": ["Volume loss if rivals hold prices"],
                "next_steps": ["Negotiate butter contracts"], "open_questions": [],
                "confidence": "medium",
                "confidence_reason": "client data and one analysis agree",
                "key_evidence": ["F1", "A1"], "would_change": ["butter prices fall back"],
                "slides": [{"action_title": "Costs rose 15.8% while prices stayed frozen",
                            "points": ["profits fell 20%"], "refs": ["F1", "A1"]}]}

    def Fixes(self, user):
        return {"fixes": []}  # no usable fix: flagged numbers are removed


class FakeRA:
    """Stores what a research associate stores: each fact with its URL and verbatim quote."""
    questions: list[str] = []

    def __init__(self, llm, web, store, eid, caps=None, **k):
        self.store, self.eid = store, eid

    def research(self, question, hypothesis_id=None, context=""):
        FakeRA.questions.append(question)
        quote = "Pune has about 1,400 bakeries."
        f = Fact(engagement_id=self.eid, claim="Pune has about 1,400 bakeries", value=1400,
                 unit="bakeries", period="2025", source_url=URL, source_title="Pune survey",
                 quote=quote, source_type="press", credibility=4)
        f.id = self.store.add(f)
        brief = Brief(answer=f"About 1,400 [F{f.id}].", value=1400, unit="bakeries",
                      period="2025", fact_ids=[f.id], confidence="medium", gaps="")
        return ResearchResult(question, brief, [f], [], spent={"tokens": 100, "searches": 2})


@pytest.fixture
def fake_research(monkeypatch):
    FakeRA.questions = []
    verified = []
    monkeypatch.setattr(agent_mod, "ResearchAssociate", FakeRA)
    monkeypatch.setattr(agent_mod, "verify_facts",
                        lambda store, web, eid, llm: verified.append(eid) or {"ok": 1})
    return verified


CASE_TURNS = [
    {"action": "ask", "content": "How big is the profit decline and the timeline?"},
    {"action": "analyse",
     "content": "# cost growth and the price rise we assume\n"
                "RESULT = {'cost_growth_pct': round((22 - 19) / 19 * 100, 1), "
                "'price_rise_pct': ASSUMPTIONS['S1']}",
     "assumptions": [{"name": "feasible cake price rise", "value": 10, "unit": "%",
                      "rationale": "rivals raised cake prices 12%"}]},
    {"action": "research", "content": "How many bakeries are there in Pune in 2025?"},
    {"action": "finish", "content": "Raise cake prices 10% and contract butter: costs grew "
                                    "15.8% while prices froze."},
]


WEB = object()  # research is faked; the agent only checks that a web is there


def case_agent(tmp_path, llm, profile="agent", eid="a1", web=WEB):
    ws = Workspace.open(eid, root=tmp_path, mode="case", profile=profile)
    iv = Interviewer(BAKERY, llm)
    iv.log = ws.state.notes.get("interview_log", [])
    return Agent(llm, ws, interviewer=iv, web=web), ws, iv


def test_case_mode_loop_actions_citations_and_deliverables(tmp_path, fake_research):
    llm = AgentLLM(CASE_TURNS)
    ag, ws, iv = case_agent(tmp_path, llm)
    out = ag.run()
    st, eid = ws.state, "a1"
    system = llm.prompts[0][0]["content"]
    assert all(f"'{a}'" in system for a in ("ask", "analyse", "research", "finish"))
    assert "Playbook: profitability" in system and "break_even_units" in system
    # ask: the reply is a verified client fact, shown to the agent with its id
    f1 = ws.facts.get(1)
    assert f1.source_type == "client" and f1.verified and f1.quote.startswith("Profits fell 20%")
    assert "INTERVIEWER [F1]: Profits fell 20%" in llm.prompts[1][1]["content"]
    # analyse: the estimate is registered first (S1) and readable in the script; A1 holds RESULT
    a = ws.facts.assumptions(eid)
    assert [(x.id, x.name, x.value) for x in a] == [(1, "feasible cake price rise", 10)]
    assert st.analyses[0].ok and st.analyses[0].result == {"cost_growth_pct": 15.8,
                                                           "price_rise_pct": 10}
    assert st.analyses[0].purpose == "cost growth and the price rise we assume"
    assert "ANALYSIS [S1]" in llm.prompts[2][1]["content"]
    assert 'RESULT [A1]: {"cost_growth_pct": 15.8' in llm.prompts[2][1]["content"]
    assert not any("RESULT" in m for m in llm.matched)          # interviewer never sees code
    # research: facts stored with URL + quote, ids back to the agent
    f2 = ws.facts.get(2)
    assert f2.source_url == URL and f2.quote and "FINDINGS [F2]" in llm.prompts[3][1]["content"]
    assert FakeRA.questions == ["How many bakeries are there in Pune in 2025?"]
    assert fake_research == [eid]                               # web facts get verified
    # the write-up: same agent, its final answer, the whole work log and the evidence ids
    (brief,) = llm.story_inputs
    assert "YOUR FINAL ANSWER:\nRaise cake prices 10%" in brief
    assert "INTERVIEWER [F1]" in brief and "EVIDENCE:" in brief and "S1: feasible" in brief
    # validator + repair: the unsourced 37 is removed, the sourced numbers stay
    assert st.notes["unverified_removed"] == ["37 in argument 2 (not in the evidence it cites)"]
    d = out["draft"]
    assert "[unverified figure]" in d.arguments[1].text and "15.8%" in d.arguments[0].text
    assert st.steps_done == ["verify", "story0", "repair", "deliver"]   # partner off
    assert llm.reviews == 0 and len(iv.log) == 1
    files = {p.name for p in (ws.dir / "deliverables").iterdir()}
    assert {"memo.md", "storyline.md", "storyline.json", "model.xlsx", "sources.md",
            "assumptions.md", "validation.json"} <= files
    assert URL in (ws.dir / "deliverables" / "sources.md").read_text(encoding="utf-8")
    assert "feasible cake price rise" in (ws.dir / "deliverables" / "assumptions.md"
                                          ).read_text(encoding="utf-8")
    assert out["final"].startswith("Raise cake prices") and not out["stopped"]


def test_partner_flag_reviews_once_revises_once_and_ships_the_better_draft(tmp_path,
                                                                          fake_research):
    llm = AgentLLM(CASE_TURNS)
    ag, ws, _ = case_agent(tmp_path, llm, profile="agent-partner")
    ag.run()
    st = ws.state
    # ScriptedLLM's partner blocks the first draft and approves the revision
    assert st.steps_done == ["verify", "story0", "review0", "story1", "review1", "repair",
                             "deliver"]
    assert llm.reviews == 2 and st.notes["shipped_draft"] == "story1"
    assert "PARTNER REVIEW" in llm.story_inputs[1] and "YOUR PREVIOUS DRAFT" in llm.story_inputs[1]


def test_real_mode_has_no_interviewer_and_researches(tmp_path, fake_research):
    llm = AgentLLM([{"action": "ask", "content": "What are your margins?"},
                    {"action": "research", "content": "How many bakeries are there in Pune?"},
                    {"action": "finish", "content": "Enter Pune."}])
    ws = Workspace.create("r1", root=tmp_path, mode="real", profile="agent-real",
                          problem_statement="Should a bakery chain enter Pune?")
    out = Agent(llm, ws, web=object()).run()
    system = llm.prompts[0][0]["content"]
    assert "'ask'" not in system and "no interviewer" in system
    assert "CLIENT: Should a bakery chain enter Pune?" in llm.prompts[0][1]["content"]
    assert "Questions left" not in llm.prompts[0][1]["content"]
    assert "there is no interviewer" in llm.prompts[1][1]["content"]   # ask refused, loop goes on
    assert "Research questions left: 10" in llm.prompts[0][1]["content"]   # agent-real caps
    assert FakeRA.questions and out["deliverables"]["memo"] == "deliverables/memo.md"
    assert system_prompt("case") != system_prompt("real")


def test_out_of_actions_the_next_turn_is_the_answer(tmp_path, monkeypatch):
    prof = agent_mod.load_profile("agent") | {"max_questions": 1, "max_analyses": 0,
                                              "max_research": 0}
    monkeypatch.setattr(agent_mod, "load_profile", lambda name: prof)
    llm = AgentLLM([{"action": "ask", "content": "How big is the profit decline?"},
                    {"action": "ask", "content": "Raise cake prices."}])
    ag, ws, _ = case_agent(tmp_path, llm, web=None)
    out = ag.run()
    assert "Give your final answer now (action=finish)" in llm.prompts[1][1]["content"]
    assert out["final"] == "Raise cake prices." and len(llm.prompts) == 2


def test_killed_mid_loop_resumes_without_repeating_actions(tmp_path, fake_research):
    llm = AgentLLM(CASE_TURNS, die_at=2)
    ag, ws, iv = case_agent(tmp_path, llm)
    with pytest.raises(QuotaExhausted):
        ag.run()
    assert [e["role"] for e in ws.state.notes["agent_log"]] == [
        "interviewer", "candidate", "interviewer", "analysis", "result"]
    llm2 = AgentLLM(CASE_TURNS[2:])
    ag2, ws2, iv2 = case_agent(tmp_path, llm2)
    ag2.run()
    assert len(llm2.prompts) == 2 and len(iv2.log) == 1               # nothing asked twice
    assert len(ws2.state.analyses) == 1 and len(ws2.facts.facts("a1")) == 2
    assert "RESULT [A1]" in llm2.prompts[0][1]["content"]             # earlier work still seen
    # a finished engagement makes no calls on resume
    ag3, _, _ = case_agent(tmp_path, AgentLLM([], forbid_calls=True))
    assert ag3.run()["draft"].answer.startswith("Raise cake prices")


def test_eval_runner_runs_the_agent_by_default(tmp_path):
    from consultant.evals import run_system
    from consultant.evals.judge import OUTCOME, PROCESS

    class Judged(AgentLLM):
        def ProcessVerdict(self, user):
            assert "ANALYSIS" in user and "RESULT [A1]" in user          # the work record
            return {k: {"justification": "ok", "score": 4} for k in PROCESS}

        def OutcomeVerdict(self, user):
            answer = user.split("CANDIDATE FINAL ANSWER")[1]
            assert "**Recommendation:** Raise cake prices" in answer
            return {k: {"justification": "ok", "score": 4} for k in OUTCOME}
    turns = [t for t in CASE_TURNS if t["action"] != "research"]   # no web in this runner
    row = run_system.run_case(Judged(turns), BAKERY, tmp_path)
    sub = row["submission"]
    assert [t["role"] for t in sub["transcript"]] == ["interviewer", "candidate", "interviewer"]
    assert sub["questions_asked"] == 1 and "S1" not in sub["final_answer"]
    assert all(row["scores"][k] == 4 for k in PROCESS + OUTCOME)
