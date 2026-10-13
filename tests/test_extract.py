"""Deterministic parts of case extraction: turn labels -> Case, and the stratified split.
All content here is synthetic."""

from consultant.knowledge.casebook import RawCase
from consultant.knowledge.extract import build_case, stratified_split
from consultant.knowledge.schema import CaseAnnotation, TurnLabel

RAW = RawCase(
    id="x", title="T", difficulty="easy", firm="F", case_type="pricing", pages=[1], notes="",
    turns=[("interviewer", "Price a new soap."), ("candidate", "Who are the customers?"),
           ("interviewer", "Urban households."), ("candidate", "What does it cost to make?"),
           ("interviewer", "Rs 12 per bar."), ("candidate", "Shall I proceed?"),
           ("interviewer", "Go ahead."), ("candidate", "Competitor prices?"),
           ("interviewer", "Okay, rivals sell at 40."), ("candidate", "Margins?"),
           ("interviewer", "Margins are thin.")])


def ann(labels):
    return CaseAnnotation(industry="FMCG", numerical=True, labels=labels, structure="s",
                          key_insight="k", recommendation="r", interviewer_feedback="")


def test_build_case_keeps_interviewer_text_verbatim():
    case = build_case(RAW, ann([TurnLabel(turn=2, kind="scoping", trigger="customers"),
                                TurnLabel(turn=4, kind="data", trigger="unit cost"),
                                TurnLabel(turn=6, kind="none", trigger="proceed"),
                                TurnLabel(turn=8, kind="none", trigger="rivals")]))
    assert case.problem_statement == "Price a new soap."
    assert [(q.question, q.answer) for q in case.clarifying_qa] == [("customers",
                                                                     "Urban households.")]
    data = [d.data for d in case.data_reveals]
    assert "Rs 12 per bar." in data
    assert "Go ahead." not in data
    assert [o.data for o in case.other_replies] == ["Go ahead."]
    assert "Okay, rivals sell at 40." in data  # labelled none but has a number -> data
    assert "Margins are thin." in data         # unlabelled -> kept as data
    assert len(case.transcript) == len(RAW.turns)


def make_cases(n_per_type):
    from consultant.knowledge.schema import Case
    out = []
    for t, n in n_per_type.items():
        for i in range(n):
            out.append(Case(id=f"{t}-{i}", title="t", firm="f", case_type=t,
                            difficulty=["easy", "moderate", "challenging"][i % 3],
                            source_pages=[], industry="i", numerical=False,
                            problem_statement="p", clarifying_qa=[], structure="s",
                            data_reveals=[], key_insight="k", recommendation="r",
                            interviewer_feedback=""))
    return out


def test_split_is_stratified_deterministic_and_disjoint():
    counts = {"profitability": 20, "market_entry": 10, "pricing": 5, "growth": 8, "ma_pe": 2,
              "unconventional": 8, "guesstimate": 8}
    cases = make_cases(counts)
    train, test = stratified_split(cases, n_test=15, seed=1)
    assert len(test) == 15 and len(train) == len(cases) - 15
    assert not {c.id for c in train} & {c.id for c in test}
    assert {c.case_type for c in test} == set(counts)  # every type represented
    again = stratified_split(cases, n_test=15, seed=1)[1]
    assert [c.id for c in again] == [c.id for c in test]
