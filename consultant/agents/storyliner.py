"""Storyliner: pyramid-principle draft (a committed recommendation first, confidence with its
reason, the evidence it rests on, what would change it, then arguments, risks and next
steps) and an action-titled slide storyline. Every claim cites F<fact id>, A<analysis id> or
S<assumption id>; numbers may only come from those."""

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
    answer: str = Field(description="the committed recommendation, stated first, 2-4 "
                                    "sentences; never 'collect more data before deciding'")
    arguments: list[Argument] = Field(min_length=1, max_length=5)
    risks: list[str]
    next_steps: list[str]
    open_questions: list[str]
    confidence: Literal["low", "medium", "high"]
    slides: list[Slide]
    # defaults keep drafts saved before these fields existed loadable; StoryDraft requires them
    confidence_reason: str = ""
    key_evidence: list[str] = Field(default_factory=list)
    would_change: list[str] = Field(default_factory=list)


class StoryDraft(Draft):
    """What the storyliner must fill in: the answer-first fields are required."""
    confidence_reason: str = Field(description="why this confidence level: how strong the "
                                               "evidence is and what is missing")
    key_evidence: list[str] = Field(min_length=2, max_length=3, description="the 2-3 evidence "
                                    "ids (F#, A#, S#) the recommendation rests on most")
    would_change: list[str] = Field(min_length=1, max_length=4, description="findings that "
                                    "would change the recommendation")


STORY_SYS = (
    "You are the storyliner. Always give the client a best answer: lead with a committed "
    "recommendation, even when the evidence is thin; then lower the confidence and say why. "
    "Structure: the recommendation; confidence (high/medium/low) with the reason; the 2-3 "
    "pieces of evidence it rests on; what would change the answer; then 3-5 supporting "
    "arguments with action titles, risks (include material risks even without a "
    "mitigation), next steps and open questions. 'Collect more data before deciding' is "
    "never the recommendation: data still to collect goes in the next steps, after the "
    "recommendation. Then a slide storyline whose action titles read as a story on their "
    "own. Use only the evidence given; committing means reasoning from it, never inventing "
    "figures: every number must appear in a cited fact (F#), analysis result (A#) or "
    "labelled assumption (S#). Round it for reading (2-3 significant figures: 0.36666 -> "
    "0.37 or 37%, 42525.0 -> 42,525) but never change its value otherwise. Do not do "
    "arithmetic in text: if a number isn't in the evidence, don't state it. If there is a "
    "RECONCILED DECISION MODEL, its numbers are the ones to use; never cite a superseded "
    "analysis. Evidence ids go only in refs and key_evidence, never in the prose the client "
    "reads.\nCONFIDENCE: high when the evidence settles the question; medium when it supports "
    "the answer but a named, plausible finding could change it; low only when a specific "
    "missing fact would likely flip the recommendation (name it). In a case interview the "
    "client's data is the evidence base: what the client doesn't have lowers confidence only "
    "if it would change the answer. Data worth collecting that wouldn't change the answer "
    "goes in next steps, not in the confidence or the caveats. One list of open questions, "
    "no repeats.\n\nHOUSE STYLE:\n")


REVISE = (
    "Revise your previous draft; don't start over. Keep its recommendation unless a BLOCKING "
    "issue shows the evidence contradicts it (then change it and say why in the confidence "
    "reason). Fix every blocking issue. Comments, the counterargument and the CEO questions "
    "are input to weigh, not orders: a counterargument alone never flips the answer.")


def write(llm, brief: str, review: str = "", role: str = "storyliner",
          previous: Draft | None = None) -> Draft:
    """A first draft from the brief, or (with `review`) a revision of `previous`: written from
    scratch each round, drafts flipped back and forth with the partner's counterarguments."""
    system = STORY_SYS + playbooks.load("deliverables")
    user = brief
    if review:
        prev = f"YOUR PREVIOUS DRAFT:\n{render_text(previous)}\n\n" if previous else ""
        user = f"{brief}\n\n{prev}PARTNER REVIEW OF YOUR PREVIOUS DRAFT:\n{review}\n\n{REVISE}"
    return llm.structured(role, [{"role": "system", "content": system},
                                 {"role": "user", "content": user}], StoryDraft)


def render_text(d: Draft) -> str:
    args = "\n".join(f"{i}. {a.title}: {a.text} [{', '.join(a.refs)}]"
                     for i, a in enumerate(d.arguments, 1))
    slides = "\n".join(f"- {s.action_title} [{', '.join(s.refs)}]" for s in d.slides)
    return (f"RECOMMENDATION: {d.answer}\nCONFIDENCE: {d.confidence}"
            + (f" ({d.confidence_reason})" if d.confidence_reason else "")
            + (f"\nRESTS ON: {', '.join(d.key_evidence)}" if d.key_evidence else "")
            + (f"\nWOULD CHANGE THE ANSWER: {'; '.join(d.would_change)}"
               if d.would_change else "")
            + f"\nARGUMENTS:\n{args}\nRISKS: {'; '.join(d.risks)}\n"
            f"NEXT STEPS: {'; '.join(d.next_steps)}\nOPEN QUESTIONS: "
            f"{'; '.join(d.open_questions)}\nSLIDES:\n{slides}")
