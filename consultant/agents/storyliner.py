"""Storyliner: pyramid-principle draft (answer first, 3-5 arguments, risks, next steps) and an
action-titled slide storyline. Every claim cites F<fact id>, A<analysis id> or S<assumption
id>; numbers may only come from those."""

from typing import Literal

from pydantic import BaseModel, Field

from consultant.knowledge import playbooks


class Argument(BaseModel):
    title: str = Field(description="action title: states the insight, not the topic")
    text: str
    refs: list[str] = Field(description="evidence ids, e.g. F3, A1, S2")


class Slide(BaseModel):
    action_title: str
    points: list[str]
    refs: list[str]


class Draft(BaseModel):
    answer: str = Field(description="the answer to the client's question, 2-4 sentences")
    arguments: list[Argument] = Field(min_length=1, max_length=5)
    risks: list[str]
    next_steps: list[str]
    open_questions: list[str]
    confidence: Literal["low", "medium", "high"]
    slides: list[Slide]


STORY_SYS = (
    "You are the storyliner. Write the engagement's answer using the pyramid principle: the "
    "answer first, then 3-5 supporting arguments with action titles, then risks (include "
    "material risks even without a mitigation), next steps and open questions. Then a slide "
    "storyline whose action titles read as a story on their own. Use only the evidence "
    "given; every number must appear in a cited fact (F#), analysis result (A#) or labelled "
    "assumption (S#), copied exactly. Do not do arithmetic in text: if a number isn't in the "
    "evidence, don't state it.\n\nHOUSE STYLE:\n")


def write(llm, brief: str, review: str = "", role: str = "storyliner") -> Draft:
    system = STORY_SYS + playbooks.load("deliverables")
    user = brief if not review else (f"{brief}\n\nPARTNER REVIEW OF YOUR PREVIOUS DRAFT — "
                                     f"fix every major/critical issue:\n{review}")
    return llm.structured(role, [{"role": "system", "content": system},
                                 {"role": "user", "content": user}], Draft)


def render_text(d: Draft) -> str:
    args = "\n".join(f"{i}. {a.title}: {a.text} [{', '.join(a.refs)}]"
                     for i, a in enumerate(d.arguments, 1))
    slides = "\n".join(f"- {s.action_title} [{', '.join(s.refs)}]" for s in d.slides)
    return (f"ANSWER: {d.answer}\nARGUMENTS:\n{args}\nRISKS: {'; '.join(d.risks)}\n"
            f"NEXT STEPS: {'; '.join(d.next_steps)}\nOPEN QUESTIONS: "
            f"{'; '.join(d.open_questions)}\nCONFIDENCE: {d.confidence}\nSLIDES:\n{slides}")
