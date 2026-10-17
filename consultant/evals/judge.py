"""Rubric judge: scores a submission against the case's reference solution, 1-5 per dimension.

What each dimension is judged on (the same for the system and the baseline):
- PROCESS dimensions (scoping, structure, hypotheses, maths, evidence) are judged by the LLM
  on everything the candidate visibly did: the interview transcript, its work record (for
  the system: the workspace's issue tree, hypotheses with status and evidence, analyses with
  results, and a fact summary; the baseline keeps no workspace) and, for both, the final
  answer, so maths done in the answer counts (the baseline's p033 expected-value maths was
  correct but scored 1 when the process call never saw the answer).
- OUTCOME dimensions (key_insight, recommendation, communication) are judged on the final
  answer alone, so a long or well-documented process can't lift the answer's score. The
  whole answer is shown (up to ANSWER_CLIP tokens, the same for both): at 900 tokens the
  system's 7-9K-char answers lost their middle, i.e. every supporting argument.
- MEASURED: efficiency is computed from the transcript, not judged (see efficiency()): both
  system and baseline used to score 1 with only a raw question count to go on.
Two judge calls per submission, one per LLM group; the Verdict combines them."""

from pydantic import BaseModel, Field

from consultant.knowledge.schema import Case
from consultant.llm import LLM
from consultant.llm.context import clip

ANSWER_CLIP = 6000  # tokens; above any answer seen so far (8.6K chars, ~2.9K tokens)

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
    "efficiency": "Measured, not judged. Wasted questions = unanswered ('I don't have that "
                  "information') + no new information (a reply already given). 5 = waste "
                  "<= 20% and no more questions than the reference interview's candidate "
                  "turns; 4 = waste <= 35% and <= 1.5x reference; 3 = waste <= 50% and <= 2x "
                  "reference; 2 = waste <= 75%; 1 = more. No questions at all = 3 (nothing "
                  "wasted, nothing gathered). Tokens and searches are reported, not scored.",
}
PROCESS = ("scoping", "structure", "hypotheses", "maths", "evidence")
OUTCOME = ("key_insight", "recommendation", "communication")
MEASURED = ("efficiency",)

_BASE = ("You are a strict consulting-interview assessor. Score the candidate on each rubric "
         "dimension below from 1 to 5 using the reference solution as ground truth. Do not "
         "reward length or confident tone. Justify each score in at most two short sentences."
         "\n\n")
PROCESS_SYS = (_BASE + "You see the candidate's process: the interview transcript, its work "
               "record (issue tree, hypotheses, analyses, facts gathered) if it kept one, and "
               "its final answer. Judge how the candidate worked; for maths, judge the "
               "analyses and every number used, including calculations shown in the final "
               "answer.\n\nRUBRIC:\n" + "\n".join(f"- {k}: {RUBRIC[k]}" for k in PROCESS))
OUTCOME_SYS = (_BASE + "You see only the candidate's final answer. An answer that misses the "
               "key insight cannot score above 2 on key_insight; one whose recommendation "
               "contradicts the evidence cannot score above 2 on recommendation.\n\nRUBRIC:\n"
               + "\n".join(f"- {k}: {RUBRIC[k]}" for k in OUTCOME))


class Score(BaseModel):
    justification: str
    score: int = Field(ge=1, le=5)


class ProcessVerdict(BaseModel):
    scoping: Score
    structure: Score
    hypotheses: Score
    maths: Score
    evidence: Score


class OutcomeVerdict(BaseModel):
    key_insight: Score
    recommendation: Score
    communication: Score


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
    work: str = ""  # work record: issue tree, hypotheses, analyses, facts ("" = none kept)
    tokens: int = 0  # LLM tokens the run used (reported with efficiency, not scored)


def reference(case: Case) -> str:
    data = "\n".join(f"- {r.data}" for r in case.data_reveals)
    return (f"PROBLEM: {case.problem_statement}\nNUMERICAL CASE: {case.numerical}\n"
            f"REFERENCE STRUCTURE:\n{case.structure}\nDATA AVAILABLE:\n{data}\n"
            f"KEY INSIGHT: {case.key_insight}\nREFERENCE RECOMMENDATION: {case.recommendation}")


def efficiency(case: Case, sub: Submission) -> Score:
    """Deterministic efficiency score from the transcript (rubric anchors above)."""
    from consultant.evals.simulator import NOT_AVAILABLE
    replies = [t["content"] for t in sub.transcript[1:] if t["role"] == "interviewer"]
    asked = sum(t["role"] == "candidate" for t in sub.transcript)
    unanswered = sum(r == NOT_AVAILABLE for r in replies)
    seen, stale = set(), 0
    for r in replies:
        if r != NOT_AVAILABLE:
            stale += r in seen
            seen.add(r)
    ref = max(1, sum(t["role"] == "candidate" for t in case.transcript))
    facts = (f"{asked} questions (reference interview: {ref} candidate turns), {unanswered} "
             f"unanswered, {stale} with no new information, {sub.searches} searches, "
             f"{sub.tokens:,} tokens.")
    if not asked:
        return Score(score=3, justification=f"{facts} Nothing wasted, nothing gathered.")
    waste = (unanswered + stale) / asked
    ratio = asked / ref
    score = (5 if waste <= .2 and ratio <= 1 else 4 if waste <= .35 and ratio <= 1.5
             else 3 if waste <= .5 and ratio <= 2 else 2 if waste <= .75 else 1)
    return Score(score=score, justification=f"{facts} {waste:.0%} of questions wasted.")


def judge(llm: LLM, case: Case, sub: Submission, role: str = "judge") -> Verdict:
    ref = f"REFERENCE SOLUTION\n{clip(reference(case), 1800)}\n\n"
    convo = "\n".join(f"{t['role'].upper()}: {t['content']}" for t in sub.transcript)
    answer = clip(sub.final_answer, ANSWER_CLIP)
    process = (f"{ref}CANDIDATE TRANSCRIPT ({sub.questions_asked} questions, {sub.searches} "
               f"searches)\n{clip(convo, 2200)}\n\nCANDIDATE WORK RECORD\n"
               f"{clip(sub.work, 1200) if sub.work else '(none kept)'}\n\n"
               f"CANDIDATE FINAL ANSWER\n{answer}")
    p = llm.structured(role, [{"role": "system", "content": PROCESS_SYS},
                              {"role": "user", "content": process}], ProcessVerdict)
    o = llm.structured(role, [{"role": "system", "content": OUTCOME_SYS},
                              {"role": "user", "content": f"{ref}CANDIDATE FINAL ANSWER\n"
                                                          f"{answer}"}],
                       OutcomeVerdict)
    return Verdict(**p.model_dump(), **o.model_dump(), efficiency=efficiency(case, sub))
