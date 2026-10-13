"""Case-mode interviewer: holds one case, reveals an item only when a question asks for it.

The LLM only *matches* the question to case items; the reply is the case's own text, so the
interviewer can never invent or leak data it wasn't asked for.
"""

import unicodedata

from pydantic import BaseModel, Field

from consultant.knowledge.schema import Case
from consultant.llm import LLM

NOT_AVAILABLE = ("I don't have that information for this case. What would you need it for, "
                 "and how would it change your approach?")

MATCH_SYS = (
    "You are the interviewer in a consulting case interview. You hold a list of case items. "
    "Given the candidate's latest question, return the ids of the items that the question "
    "specifically asks for, or that the analysis the candidate proposes would directly need. "
    "Rules: a question must target the item's subject (e.g. asking about costs unlocks cost "
    "data, not customer data). Vague or generic questions ('any other information?', 'what "
    "else should I know?') unlock nothing. Questions about things absent from the list "
    "unlock nothing. When unsure, return no ids.")


class Match(BaseModel):
    reasoning: str = Field(description="one sentence")
    item_ids: list[int]


class Interviewer:
    def __init__(self, case: Case, llm: LLM, role: str = "interviewer"):
        self.case, self.llm, self.role = case, llm, role
        self.items = ([(qa.question, qa.answer) for qa in case.clarifying_qa]
                      + [(r.trigger, r.data) for r in case.data_reveals + case.other_replies])
        self.revealed: set[int] = set()
        self.log: list[dict] = []

    def opening(self) -> str:
        return self.case.problem_statement

    def _menu(self) -> str:
        return "\n".join(f"[{i}] subject: {topic[:160]} | content: {data[:160]}"
                         for i, (topic, data) in enumerate(self.items))

    def ask(self, question: str) -> str:
        question = unicodedata.normalize("NFKC", question).replace("‑", "-")
        ids: list[int] = []
        if self.items:
            msgs = [{"role": "system", "content": MATCH_SYS},
                    {"role": "user", "content": f"CASE: {self.case.problem_statement}\n\n"
                                                f"ITEMS:\n{self._menu()}\n\n"
                                                f"CANDIDATE: {question}"}]
            match = self.llm.structured(self.role, msgs, Match)
            ids = sorted({i for i in match.item_ids if 0 <= i < len(self.items)})
        reply = "\n".join(self.items[i][1] for i in ids) if ids else NOT_AVAILABLE
        self.revealed |= set(ids)
        self.log.append({"question": question, "item_ids": ids, "reply": reply})
        return reply
