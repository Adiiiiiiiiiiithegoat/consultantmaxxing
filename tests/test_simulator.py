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
