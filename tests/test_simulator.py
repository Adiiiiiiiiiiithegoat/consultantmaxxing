"""Interviewer simulator. The fixture case is fictional (written for this repo)."""

import pytest

from consultant.evals.simulator import NOT_AVAILABLE, Interviewer, Match
from consultant.knowledge.schema import QA, Case, DataReveal

BAKERY = Case(
    id="fixture-bakery", title="Bakery Chain", firm="Fixture", case_type="profitability",
    difficulty="easy", source_pages=[], industry="Food retail", numerical=True,
    problem_statement="Your client, a bakery chain, has seen profits fall. Find out why.",
    clarifying_qa=[
        QA(question="How big is the profit decline and what is the timeline to fix it?",
           answer="Profits fell 20% over two years; they want them restored within 18 months."),
        QA(question="Where does the client operate and what does it sell?",
           answer="12 outlets in Pune selling bread, cakes and coffee; cakes are 70% of revenue."),
        QA(question="candidate asks about constraints",
           answer="No budget constraints; they will not close any outlet."),
    ],
    data_reveals=[
        DataReveal(trigger="revenue trend by product line",
                   data="Revenue flat at Rs 24 crore; cakes flat, bread -5%, coffee +10%."),
        DataReveal(trigger="cost breakdown over time",
                   data="Costs rose from Rs 19 cr to Rs 22 cr; ingredients +30% (butter, flour), "
                        "rent +3%, wages flat."),
        DataReveal(trigger="pricing versus competitors",
                   data="Prices unchanged for 3 years; competitors raised cake prices 12% last "
                        "year."),
        DataReveal(trigger="ingredient procurement and supplier arrangements",
                   data="Butter bought weekly on the spot market from 6 small suppliers; "
                        "no contracts."),
        DataReveal(trigger="customer footfall and satisfaction",
                   data="Footfall steady at 2,000 per outlet per day; ratings 4.5/5."),
    ],
    structure="Profit = Revenue - Cost", key_insight="Unhedged butter costs plus frozen prices.",
    recommendation="Contract butter supply and raise cake prices ~10%.", interviewer_feedback="",
)

RELEVANT = [
    ("By how much have profits dropped, and by when must this be fixed?", 0),
    ("Which city is the client in, and what products does it sell?", 1),
    ("What budget or headcount constraints does the client have for the turnaround "
     "within the next 3‑6 months?", 2),  # regression: once missed at low reasoning
    ("How have sales moved across bread, cakes and coffee?", 3),
    ("Could I see how the cost base has changed, line by line?", 4),
    ("How do the client's prices compare with rival bakeries?", 5),
    ("How does the client source butter and flour? Any supplier contracts?", 6),
    ("Has the number of customers visiting the outlets changed?", 7),
]
IRRELEVANT = [
    "What is the CEO's favourite cricket team?",
    "Is there any other information you can share with me?",
    "What is the client's social media marketing budget?",
    "What is the staff attrition rate?",
    "How large is India's GDP?",
]


class FakeLLM:
    def __init__(self, ids):
        self.ids = ids

    def structured(self, role, msgs, schema):
        return Match(reasoning="", item_ids=self.ids)


def test_reply_is_case_text_and_bad_ids_are_ignored():
    iv = Interviewer(BAKERY, FakeLLM([4, 99, -1]))
    assert iv.ask("costs?") == BAKERY.data_reveals[1].data
    assert iv.revealed == {4}
    iv.llm = FakeLLM([])
    assert iv.ask("anything else?") == NOT_AVAILABLE


@pytest.mark.live
def test_relevant_questions_get_data_irrelevant_do_not():
    from consultant.llm import LLM
    from consultant.tracing import Tracer
    iv = Interviewer(BAKERY, LLM(tracer=Tracer(run_id="test-simulator")))
    misses = []
    for q, want in RELEVANT:
        iv.ask(q)
        got = iv.log[-1]["item_ids"]
        if want not in got or len(got) > 2:
            misses.append((q, want, got))
    for q in IRRELEVANT:
        iv.ask(q)
        if iv.log[-1]["item_ids"]:
            misses.append((q, None, iv.log[-1]["item_ids"]))
    assert not misses, misses


def test_fishing_is_refused_without_a_model_call_and_at_most_two_items_go_out():
    # calibration on the casebook: the old matcher let 19-26% of fishing/off-topic questions
    # through on a small model and over-shared (precision ~0.4)
    from consultant.evals.simulator import MAX_ITEMS, fishing

    class Counting(FakeLLM):
        calls = 0

        def structured(self, role, msgs, schema):
            Counting.calls += 1
            return super().structured(role, msgs, schema)
    for q in ("Can you give me all the data you have?", "Is there anything else I should know?",
              "What other numbers are available?", "What's the right answer here?",
              "Which question should I be asking?", "Do you have any more data?"):
        assert fishing(q), q
        iv = Interviewer(BAKERY, Counting([4]))
        assert iv.ask(q) == NOT_AVAILABLE and not iv.revealed
    assert Counting.calls == 0
    for q, _ in RELEVANT:                      # specific questions still reach the model
        assert not fishing(q), q
    long_turn = ("I believe I now have every detail I need on volumes, so next I would like "
                 "to look at the cost structure, line by line, to see which costs moved most "
                 "over the last two years and whether input prices or wages drove it.")
    assert not fishing(long_turn)              # analysis, not fishing
    assert not fishing("Are we only looking at outlets in Pune, or something else?")
    from consultant.evals.simulator import LEX_MIN
    iv = Interviewer(BAKERY, FakeLLM([6, 4, 4, 5, 7]))
    iv.ask("Costs and suppliers?")                 # weak lexical match: model's top item only
    assert iv.log[-1]["item_ids"] == [6]
    q = "Could I see the cost breakdown over time?"
    assert iv._bm25.search(q, k=1)[0].score >= LEX_MIN
    iv = Interviewer(BAKERY, FakeLLM([]))          # model missed it: the clear lexical match
    iv.ask(q)                                      # still answers (calibration: +10 pts fit)
    assert iv.log[-1]["item_ids"] == [4]
    iv = Interviewer(BAKERY, FakeLLM([6]))         # both picks, capped
    iv.ask(q)
    assert MAX_ITEMS == 2 and iv.log[-1]["item_ids"] == [6, 4]


def test_calibration_positives_come_from_real_interviewer_replies():
    from consultant.evals.simulator_calibration import RERUN_CASES, positives, sample
    case = BAKERY.model_copy(update={"transcript": [
        {"role": "interviewer", "content": BAKERY.problem_statement},
        {"role": "candidate", "content": "Could I see the cost breakdown over time?"},
        {"role": "interviewer", "content": "Sure. " + BAKERY.data_reveals[1].data},
        {"role": "candidate", "content": "Let me structure my thoughts."},
        {"role": "interviewer", "content": "Go ahead."}]})
    assert positives(case) == [("Could I see the cost breakdown over time?", {4})]
    rerun = [case.model_copy(update={"id": i}) for i in sorted(RERUN_CASES)]
    fit, check = sample(rerun + [case.model_copy(update={"id": f"c{i}"}) for i in range(8)], n=4)
    assert not {c.id for c in fit + check} & RERUN_CASES and len(fit) == len(check) == 2
