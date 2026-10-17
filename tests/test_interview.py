"""Case-mode interview as a conversation (offline). All wording here is synthetic; the rules
were checked against the casebook's real train interviews locally (numbers in PROGRESS.md)."""

from consultant.agents import interview as I
from consultant.agents.engagement_manager import NO_INFO, Engagement
from consultant.knowledge.retrieval import Retriever
from consultant.workspace import Workspace
from tests.test_engagement import BAKERY, ScriptedLLM

REFUSAL = (f"{NO_INFO} for this case. What would you need it for, and how would it change "
           "your approach?")


def test_prompts_candidate():
    for prompt in ("Sales went up. What could explain that?",
                   "Fine. Let's look at the cost side now.",
                   "Let’s focus on the delivery network first.",       # curly apostrophe
                   "Sure, go ahead.", "Correct, proceed with the volume side.",
                   "Please start by walking me through the revenue drivers.",
                   "We sell three products. How would you size the market"):
        assert I.prompts_candidate(prompt), prompt
    for data in ("Costs rose by a fifth while revenue stayed flat.",
                 "The client runs twelve outlets in two cities.",
                 "There is no change in pricing."):
        assert not I.prompts_candidate(data), data


def test_search_style_queries_are_told_apart_from_speech():
    for query in ("Acme Airlines fuel cost per block hour FY2024-FY2026",
                  "Average jet fuel price at two airports 2024-2026",
                  "Bakery chain cake price benchmarks Pune 2025"):
        assert I.search_style(query), query
    for said in ("How has the cost base changed?", "I'd like to look at the cost structure.",
                 "Could you share the revenue split by product",
                 "First, I want to understand the customer segments.",
                 "Revenue can be split into price and volume. Price looks stable, so let me "
                 "look at volume."):
        assert not I.search_style(said), said


def test_repeats():
    asked = ["What is the client's budget and appetite for investment?"]
    assert I.repeats("What budget and investment appetite does the client have?", asked)
    assert not I.repeats("How do our prices compare with rivals?", asked)
    assert not I.repeats("anything", [])


class Iv:
    """Scripted interviewer: replies in order; records what it was told."""
    def __init__(self, replies):
        self.case, self.replies, self.log = BAKERY, list(replies), []

    def opening(self):
        return BAKERY.problem_statement

    def ask(self, q):
        r = self.replies.pop(0) if self.replies else REFUSAL
        self.log.append({"question": q, "item_ids": [], "reply": r})
        return r


class Talk(ScriptedLLM):
    def CandidateTurn(self, user):
        assert "INTERVIEWER'S LAST MESSAGE" in user
        return {"message": "Volume, I think: the new store format may draw more visits. "
                           "How has footfall moved?"}


def eng(tmp_path, replies, profile="claude"):
    llm = Talk()
    ws = Workspace.open("iv", root=tmp_path, mode="case", profile=profile)
    iv = Iv(replies)
    e = Engagement(llm, ws, interviewer=iv, retriever=Retriever([]))
    ws.state.problem_statement = BAKERY.problem_statement
    ws.state.scope = llm.Scope("")
    return e, ws, iv, llm


def test_a_prompt_is_answered_before_the_next_question(tmp_path):
    e, ws, iv, llm = eng(tmp_path, ["Revenue is up but profit is down. What might drive the "
                                    "revenue rise?", "Footfall is up by a third since the "
                                    "refit.", "Prices are unchanged."])
    e._ask_client(["Is the decline in revenue or in costs?", "How do prices compare?"])
    said = [x["question"] for x in iv.log]
    assert said[1].startswith("Volume, I think") and llm.calls.count("CandidateTurn") == 1
    assert said[2] == "How do prices compare?"
    assert any("Footfall is up" in f.claim for f in ws.facts.facts("iv"))


def test_search_queries_and_repeats_never_reach_the_client(tmp_path):
    e, ws, iv, _ = eng(tmp_path, ["Costs rose by a fifth.", "Prices are unchanged."])
    e._ask_client(["How have costs moved?", "Bakery chain ingredient cost index Pune 2024-2026",
                   "How have the costs moved over time?", "How do prices compare?"])
    assert [x["question"] for x in iv.log] == ["How have costs moved?", "How do prices compare?"]
    assert len(ws.state.notes["not_asked"]) == 2


def test_two_replies_with_nothing_new_end_the_interview_before_the_cap(tmp_path):
    e, ws, iv, _ = eng(tmp_path, ["Costs rose by a fifth.", REFUSAL, "Costs rose by a fifth."])
    qs = ["How have costs moved?", "What is the board's view?", "Which cost line moved most?",
          "How do prices compare?", "Has footfall changed?"]
    e._ask_client(qs)
    assert len(iv.log) == 3 < e.prof["max_questions"]         # stopped: refusal, then a repeat
    assert ws.state.notes["stale_run"] == 2 and e._interview_over()
    assert ws.state.open_questions == qs[3:]
    assert "The interview is over" in e._case_note() and "ALREADY ASKED" in e._case_note()
    assert len([f for f in ws.facts.facts("iv") if f.source_type == "client"]) == 1


def test_case_note_only_in_case_mode(tmp_path):
    e, ws, *_ = eng(tmp_path, [])
    assert "CASE INTERVIEW" in e._case_note()
    ws.state.mode = "real"
    assert e._case_note() == ""


def test_rules_hold_on_real_casebook_interviews():
    # Train split only, and not the 12 eval cases (the first 12 in train order), so the cases
    # the system is scored on played no part in setting these rules. Skips without local data.
    import re

    import pytest

    from consultant.knowledge.extract import load_cases
    try:
        held = load_cases("train")[12:]
    except FileNotFoundError:
        held = []
    if len(held) < 10:
        pytest.skip("extracted train cases not available")
    tp = fp = fn = 0
    for c in held:
        t = c.transcript
        for k in range(1, len(t) - 1):
            if t[k]["role"] != "interviewer" or t[k + 1]["role"] != "candidate":
                continue
            nxt = t[k + 1]["content"]
            # proxy label: the real candidate answered with substance, not just a question
            answered = len(nxt.split()) >= 25 and any(
                not s.endswith("?") for s in re.split(r"(?<=[.?!])\s+", nxt.strip()) if s)
            flag = I.prompts_candidate(t[k]["content"])
            tp, fp, fn = tp + (flag and answered), fp + (flag and not answered), \
                fn + (not flag and answered)
    assert tp / (tp + fp) >= 0.8 and tp / (tp + fn) >= 0.6, (tp, fp, fn)   # 0.87 / 0.68
    turns = [t["content"] for c in held for t in c.transcript if t["role"] == "candidate"]
    assert sum(map(I.search_style, turns)) <= 0.02 * len(turns)             # 2 of 291
    rep = sum(I.repeats(q, [t["content"] for t in c.transcript[:i] if t["role"] == "candidate"])
              for c in held for i, t in enumerate(c.transcript) if t["role"] == "candidate"
              for q in [t["content"]])
    assert rep <= 0.01 * len(turns)                                           # 1 of 291
