"""Partner: independent red-team with a fresh context. Sees only the question, the client's
initial belief, the draft and the evidence (facts + analysis results) — never the team's
reasoning — so it can't inherit the team's blind spots.

Findings are blocking (the draft would mislead the client) or non-blocking comments (wording,
emphasis, framing). The verdict is set in code: approve, with comments, whenever nothing
blocks."""

from typing import Literal

from pydantic import BaseModel, Field

# "other" is kept so reviews saved before the blocking split still load; it never blocks.
BLOCKING_KINDS = ("unsupported_claim", "maths", "non_sequitur", "missing_branch",
                  "client_pleasing")
COMMENT_KINDS = ("wording", "emphasis", "framing", "other")


class Issue(BaseModel):
    kind: Literal["unsupported_claim", "maths", "non_sequitur", "missing_branch",
                  "client_pleasing", "wording", "emphasis", "framing", "other"]
    severity: Literal["minor", "major", "critical"]
    where: str = Field(description="which part of the draft")
    detail: str

    @property
    def blocking(self) -> bool:
        return self.kind in BLOCKING_KINDS and self.severity in ("major", "critical")


class Review(BaseModel):
    issues: list[Issue]
    strongest_counterargument: str
    ceo_questions: list[str] = Field(description="3 questions a sceptical CEO would ask")
    agrees_with_client_prior: bool
    prior_agreement_justified: bool = Field(description="if it agrees with the client's "
                                            "initial belief, is that justified by evidence?")
    verdict: Literal["approve", "revise"]

    def blocking(self) -> list[Issue]:
        out = [i for i in self.issues if i.blocking]
        if self.agrees_with_client_prior and not self.prior_agreement_justified:
            out.append(Issue(kind="client_pleasing", severity="major", where="answer",
                             detail="confirms the client's prior without evidence"))
        return out

    def comments(self) -> list[Issue]:
        """Non-blocking points: the draft is approved with these as comments."""
        return [i for i in self.issues if not i.blocking]


PARTNER_SYS = (
    "You are a senior partner reviewing a team's draft before it goes to the client. Be "
    "sceptical; your job is to find what is wrong, not to praise. Check: (1) every claim and "
    "number is supported by the listed evidence ids (a number not present in the cited "
    "evidence is an unsupported_claim; an arithmetic slip is maths); (2) the recommendation "
    "follows from the findings (else non_sequitur); (3) an obvious branch of the problem "
    "was not examined (missing_branch); (4) anti-agreeableness: if the answer simply "
    "confirms what the client already believed, it must be justified by evidence, otherwise "
    "flag client_pleasing. Those five kinds are BLOCKING when major or critical; use major "
    "only if the draft would mislead the client or is factually or logically wrong. Before "
    "flagging maths, recompute: if every number checks out it is not a maths issue. Points "
    "about how something is said, what is stressed, or a fairer way to put it are "
    "NON-BLOCKING: use kind wording, emphasis or framing; they never stop approval. State "
    "the strongest counterargument and three CEO questions. Verdict 'revise' only if a "
    "blocking issue exists; otherwise 'approve' (non-blocking points go as comments).")


def format_review(r: Review) -> str:
    def lines(items: list[Issue]) -> str:
        return "\n".join(f"- [{i.severity}] {i.kind} @ {i.where}: {i.detail}"
                         for i in items) or "- none"
    return (f"VERDICT: {r.verdict}\nBLOCKING ISSUES (fix these):\n{lines(r.blocking())}\n"
            f"COMMENTS (optional):\n{lines(r.comments())}\nCOUNTERARGUMENT: "
            f"{r.strongest_counterargument}\nCEO QUESTIONS: {'; '.join(r.ceo_questions)}")


def review(llm, question: str, client_prior: str, draft_text: str, evidence: str,
           role: str = "partner") -> Review:
    user = (f"CLIENT QUESTION: {question}\nCLIENT'S INITIAL BELIEF: {client_prior or 'none'}"
            f"\n\nDRAFT:\n{draft_text}\n\nEVIDENCE (the only admissible support):\n{evidence}")
    r = llm.structured(role, [{"role": "system", "content": PARTNER_SYS},
                              {"role": "user", "content": user}], Review)
    # deterministic: a blocking issue can't be approved, and comments alone can't block
    r.verdict = "revise" if r.blocking() else "approve"
    return r
