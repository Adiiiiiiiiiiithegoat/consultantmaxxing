"""Case-mode interviewer: holds one case, reveals an item only when a question asks for it.

The LLM only *matches* the question to case items; the reply is the case's own text, so the
interviewer can never invent or leak data it wasn't asked for.

Calibrated on the casebook's real interviews (evals/simulator_calibration.py, train split):
real interviewers hand data over when the candidate asks for it or proposes an analysis that
needs it, about one item per answered question. Fishing requests ("give me all the data",
"what's the answer") are refused before any model call, so that holds whatever model plays the
interviewer. Each answer is the model's top item plus the best lexical (BM25) match when it
clearly matches (score >= LEX_MIN): the two miss different questions. At most MAX_ITEMS go out.
"""

import re
import unicodedata

from pydantic import BaseModel, Field

from consultant.knowledge.schema import Case
from consultant.llm import LLM

NOT_AVAILABLE = ("I don't have that information for this case. What would you need it for, "
                 "and how would it change your approach?")

MAX_ITEMS = 2  # the real interviews hand over ~1 item per answered question
LEX_MIN = 6.0  # BM25 score for the lexical pick (set on the calibration fit half)

# Fishing: asking for everything, for more without saying what, or for the answer itself.
_FISHING = re.compile(
    r"\b(?:all|every|everything|whatever)\b[^.?!]{0,40}\b(?:data|information|info|numbers?|"
    r"figures?|details?|have|got|case|know)\b|\b(?:dump|give me)\b[^.?!]{0,20}\b(?:all|every)"
    r"|\banything\s+(?:else|more)\b|\bwhat\s+else\b"
    r"|\b(?:any|some|what|which)\s+(?:other|more|further|additional)\s+(?:data|information|"
    r"info|numbers?|figures?|details?)\b|\b(?:more|further|other|additional)\s+(?:data|"
    r"information|info|numbers?|figures?|details?)\s+(?:are|is)?\s*(?:available|missing)\b"
    r"|\b(?:the|right|correct|final)\s+answer\b|\b(?:key|main)\s+(?:insight|takeaway)\b"
    r"|\bwh(?:at|ich)\s+(?:question\s+)?should\s+i\s+(?:be\s+)?ask(?:ing)?\b"
    r"|\bmore\s+(?:information|info|data)\s+i'?m\s+missing\b", re.I)


def fishing(question: str) -> bool:
    """A short request that names no specific subject: refused without asking the model.
    Long turns are analysis that happens to say 'all the details', not fishing."""
    return len(question.split()) <= 30 and bool(_FISHING.search(question))


MATCH_SYS = (
    "You are the interviewer in a consulting case interview. You hold a list of case items; "
    "each has a subject (what it answers) and its content. Real interviewers hand an item over "
    "when the candidate's latest message asks about its subject, asks something its content "
    "answers, or proposes an analysis or next step that needs that data (e.g. 'I'd like to "
    "look at the cost structure' gets the cost data). Return the ids of the items the message "
    "asks for or directly needs, most relevant first, usually one and at most two. Return no "
    "ids when the message is about something the items don't cover, or doesn't say what it "
    "wants (e.g. 'any other information?'). Never hand over an item the message doesn't touch.")


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
        from consultant.knowledge.retrieval import BM25, Doc
        self._bm25 = BM25([Doc(str(i), "item", "", f"{s} {s} {t}", {})  # subject weighs 2x
                           for i, (s, t) in enumerate(self.items)]) if self.items else None

    def opening(self) -> str:
        return self.case.problem_statement

    def _menu(self) -> str:
        return "\n".join(f"[{i}] subject: {topic[:160]} | content: {data[:160]}"
                         for i, (topic, data) in enumerate(self.items))

    def ask(self, question: str) -> str:
        question = unicodedata.normalize("NFKC", question).replace("‑", "-")
        ids: list[int] = []
        if self.items and not fishing(question):
            msgs = [{"role": "system", "content": MATCH_SYS},
                    {"role": "user", "content": f"CASE: {self.case.problem_statement}\n\n"
                                                f"ITEMS:\n{self._menu()}\n\n"
                                                f"CANDIDATE: {question}"}]
            match = self.llm.structured(self.role, msgs, Match)
            model = [i for i in match.item_ids if 0 <= i < len(self.items)][:1]
            lexical = [int(h.doc.id) for h in self._bm25.search(question, k=1)
                       if h.score >= LEX_MIN]
            ids = list(dict.fromkeys(model + lexical))[:MAX_ITEMS]
        reply = "\n".join(self.items[i][1] for i in ids) if ids else NOT_AVAILABLE
        self.revealed |= set(ids)
        self.log.append({"question": question, "item_ids": ids, "reply": reply})
        return reply
