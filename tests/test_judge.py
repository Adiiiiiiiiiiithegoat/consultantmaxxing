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
