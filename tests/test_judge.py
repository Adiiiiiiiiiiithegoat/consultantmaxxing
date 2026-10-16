"""Judge discrimination (Phase 1 gate). Needs extracted train cases in data/derived (local only)."""

import random

import pytest

from consultant.evals.judge import RUBRIC, Submission, Verdict, judge
from consultant.knowledge.schema import Case

GAP = 1.0  # "clearly higher": strong beats flawed by >= 1 point on the 1-5 mean


def test_verdict_overall():
    v = Verdict(**{k: {"justification": "", "score": 5 if k == "maths" else 2} for k in RUBRIC})
    assert v.scores()["maths"] == 5 and v.overall() == pytest.approx((8 * 2 + 5) / 9)


def strong_submission(case: Case) -> Submission:
    """The real (successful) interview from the casebook, then its actual answer."""
    final = f"{case.recommendation}\n\nWhy: {case.key_insight}"
    asked = sum(t["role"] == "candidate" for t in case.transcript)
    return Submission(transcript=case.transcript, final_answer=final, questions_asked=asked)


FLAW_SYS = (
    "Write the final answer of a weak case-interview candidate. It must sound confident and "
    "professional but: blame a different root cause than the true one, include one arithmetic "
    "error in a calculation, and give a generic recommendation that does not follow from the "
    "case data. 120-180 words. Plain text.")


def flawed_submission(llm, case: Case) -> Submission:
    msgs = [{"role": "system", "content": FLAW_SYS},
            {"role": "user", "content": f"Case: {case.problem_statement}\n"
                                        f"True key insight (avoid it): {case.key_insight}"}]
    final = llm.complete("baseline", msgs, max_tokens=1200).content
    t = [{"role": "interviewer", "content": case.problem_statement},
         {"role": "candidate", "content": "I'll jump straight in with my hypothesis."}]
    return Submission(transcript=t, final_answer=final, questions_asked=0)


@pytest.mark.live
def test_judge_prefers_strong_over_flawed():
    from consultant.knowledge.extract import load_cases
    from consultant.llm import LLM
    from consultant.tracing import Tracer
    train = load_cases("train")
    if len(train) < 5:
        pytest.skip("extracted train cases not available")
    cases = random.Random(7).sample(train, 5)
    llm = LLM(tracer=Tracer(run_id="test-judge"))
    rows = []
    for c in cases:
        good = judge(llm, c, strong_submission(c)).overall()
        bad = judge(llm, c, flawed_submission(llm, c)).overall()
        rows.append((c.id, round(good, 2), round(bad, 2)))
    wins = sum(g - b >= GAP for _, g, b in rows)
    print(rows)
    assert wins >= 4, rows


def test_efficiency_is_measured_and_an_efficient_run_beats_a_wasteful_one():
    # system and baseline both scored 1.00 on efficiency with only a raw question count
    # behind the score; it's now measured from the transcript, the same way for both
    from consultant.evals.judge import efficiency
    from consultant.evals.simulator import NOT_AVAILABLE
    from tests.test_engagement import BAKERY

    def run(replies: list[str], tokens: int = 0) -> Submission:
        t = [{"role": "interviewer", "content": "opening"}]
        for i, r in enumerate(replies):
            t += [{"role": "candidate", "content": f"q{i}"}, {"role": "interviewer",
                                                            "content": r}]
        return Submission(transcript=t, final_answer="a", questions_asked=len(replies),
                          tokens=tokens)
    efficient = run(["costs rose", "prices frozen", "butter on spot market", "footfall flat"])
    wasteful = run([NOT_AVAILABLE] * 5 + ["costs rose", "costs rose", "prices frozen",
                                          NOT_AVAILABLE, "costs rose"], tokens=90_000)
    for n_ref in (4, 8, 16):                       # whatever the reference interview's length
        case = BAKERY.model_copy(update={"transcript": [
            {"role": "candidate", "content": "q"}, {"role": "interviewer", "content": "a"}]
            * n_ref})
        good, bad = efficiency(case, efficient), efficiency(case, wasteful)
        assert good.score == 5 and bad.score == 1 and good.score > bad.score
    s = efficiency(case, wasteful).justification
    assert "10 questions" in s and "6 unanswered" in s and "2 with no new information" in s
    assert "90,000 tokens" in s and "80% of questions wasted" in s
    # monotonic: adding waste never raises the score
    scores = [efficiency(case, run([f"new {i}" for i in range(10 - w)]
                                   + [NOT_AVAILABLE] * w)).score for w in range(11)]
    assert scores == sorted(scores, reverse=True) and scores[0] == 5 and scores[-1] == 1
    assert efficiency(case, run([])).score == 3    # asked nothing: nothing wasted or gathered


def test_judge_sees_the_whole_answer_and_process_sees_the_answer_too():
    # the outcome call used to clip answers to 900 tokens (the system's 7-9K-char answers lost
    # every argument) and the process call never saw the answer (the baseline's correct
    # expected-value maths, done in its answer, scored maths 1)
    from consultant.evals.judge import OUTCOME, PROCESS
    from tests.test_engagement import BAKERY
    seen = {}

    class L:
        def structured(self, role, msgs, schema):
            seen[schema.__name__] = msgs[-1]["content"]
            keys = PROCESS if schema.__name__ == "ProcessVerdict" else OUTCOME
            return schema(**{k: {"justification": "", "score": 3} for k in keys})
    answer = ("Outsource. " + "Supporting argument with maths: 0.6 x 90 + 0.4 x 50 = 74. "
              * 150 + "LAST LINE")  # ~9K chars, longer than any answer so far
    judge(L(), BAKERY, Submission(transcript=[], final_answer=answer))
    for call in ("ProcessVerdict", "OutcomeVerdict"):
        assert answer in seen[call], call
