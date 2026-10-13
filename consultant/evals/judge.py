"""Rubric judge: scores a submission against the case's reference solution, 1-5 per dimension."""

from pydantic import BaseModel, Field

from consultant.knowledge.schema import Case
from consultant.llm import LLM
from consultant.llm.context import clip

RUBRIC = {
    "scoping": "Clarified objective, scope, timeline and business model before solving. "
               "1 = dove in blind; 3 = some clarification; 5 = sharp, relevant scoping.",
    "structure": "Issue tree is MECE and fits this problem. 1 = none or generic list; "
                 "3 = reasonable but overlapping/gappy; 5 = clean, tailored, prioritised.",
    "hypotheses": "Formed and tested explicit hypotheses, prioritising likely branches. "
                  "1 = none; 3 = implicit; 5 = explicit, ranked, updated with evidence.",
    "key_insight": "Found the reference key insight / root cause. 1 = missed; "
                   "3 = partially or alongside wrong causes; 5 = found it clearly.",
    "maths": "Calculations and quantitative claims are correct. If the case needs no "
             "calculation, judge any numbers used. 1 = material errors; 5 = all correct.",
    "evidence": "Claims rest on data revealed in the case (or cited sources), not assertion. "
                "1 = unsupported; 3 = mixed; 5 = every key claim backed.",
    "recommendation": "Recommendation follows from the evidence and addresses the objective. "
                      "1 = non-sequitur or missing; 3 = generic; 5 = specific, actionable, "
                      "follows from findings.",
    "communication": "Answer-first, pyramid structure, concise. 1 = rambling; "
                     "5 = crisp answer then supporting points.",
    "efficiency": "Reached the answer with few, purposeful questions/searches. "
                  "1 = scattershot or far too many; 5 = economical and targeted.",
}

JUDGE_SYS = (
    "You are a strict consulting-interview assessor. Score the candidate's submission on each "
    "rubric dimension from 1 to 5 using the reference solution as ground truth. Do not reward "
    "length or confident tone. A submission that misses the key insight cannot score above 2 "
    "on key_insight; one whose recommendation contradicts the evidence cannot score above 2 "
    "on recommendation. Justify each score in at most two short sentences.\n\nRUBRIC:\n"
    + "\n".join(f"- {k}: {v}" for k, v in RUBRIC.items()))


class Score(BaseModel):
    justification: str
    score: int = Field(ge=1, le=5)


class Verdict(BaseModel):
    scoping: Score
    structure: Score
    hypotheses: Score
    key_insight: Score
    maths: Score
    evidence: Score
    recommendation: Score
    communication: Score
    efficiency: Score

    def scores(self) -> dict[str, int]:
        return {k: getattr(self, k).score for k in RUBRIC}

    def overall(self) -> float:
        return sum(self.scores().values()) / len(RUBRIC)


class Submission(BaseModel):
    transcript: list[dict] = Field(default_factory=list)  # [{"role", "content"}]
    final_answer: str
    questions_asked: int = 0
    searches: int = 0


def reference(case: Case) -> str:
    data = "\n".join(f"- {r.data}" for r in case.data_reveals)
    return (f"PROBLEM: {case.problem_statement}\nNUMERICAL CASE: {case.numerical}\n"
            f"REFERENCE STRUCTURE:\n{case.structure}\nDATA AVAILABLE:\n{data}\n"
            f"KEY INSIGHT: {case.key_insight}\nREFERENCE RECOMMENDATION: {case.recommendation}")


def judge(llm: LLM, case: Case, sub: Submission, role: str = "judge") -> Verdict:
    convo = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in sub.transcript)
    user = (f"REFERENCE SOLUTION\n{clip(reference(case), 1800)}\n\n"
            f"CANDIDATE TRANSCRIPT ({sub.questions_asked} questions, {sub.searches} searches)\n"
            f"{clip(convo, 2200)}\n\nCANDIDATE FINAL ANSWER\n{clip(sub.final_answer, 900)}")
    return llm.structured(role, [{"role": "system", "content": JUDGE_SYS},
                                 {"role": "user", "content": user}], Verdict)
