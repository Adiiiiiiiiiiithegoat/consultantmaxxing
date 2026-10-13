"""Partner: independent red-team with a fresh context. Sees only the question, the client's
initial belief, the draft and the evidence (facts + analysis results) — never the team's
reasoning — so it can't inherit the team's blind spots."""

from typing import Literal

from pydantic import BaseModel, Field


class Issue(BaseModel):
    kind: Literal["unsupported_claim", "maths", "non_sequitur", "missing_branch",
                  "client_pleasing", "other"]
    severity: Literal["minor", "major", "critical"]
    where: str = Field(description="which part of the draft")
    detail: str


class Review(BaseModel):
    issues: list[Issue]
    strongest_counterargument: str
    ceo_questions: list[str] = Field(description="3 questions a sceptical CEO would ask")
    agrees_with_client_prior: bool
    prior_agreement_justified: bool = Field(description="if it agrees with the client's "
                                            "initial belief, is that justified by evidence?")
    verdict: Literal["approve", "revise"]

    def blocking(self) -> list[Issue]:
        out = [i for i in self.issues if i.severity in ("major", "critical")]
        if self.agrees_with_client_prior and not self.prior_agreement_justified:
            out.append(Issue(kind="client_pleasing", severity="major", where="answer",
                             detail="confirms the client's prior without evidence"))
        return out


PARTNER_SYS = (
    "You are a senior partner reviewing a team's draft before it goes to the client. Be "
    "sceptical; your job is to find what is wrong, not to praise. Check: (1) every claim and "
    "number is supported by the listed evidence ids (a number not present in the cited "
    "evidence is an unsupported_claim; an arithmetic slip is maths); (2) the recommendation "
    "follows from the findings (else non_sequitur); (3) an obvious branch of the problem "
    "was not examined (missing_branch); (4) anti-agreeableness: if the answer simply "
    "confirms what the client already believed, it must be justified by evidence, otherwise "
    "flag client_pleasing. State the strongest counterargument and three CEO questions. "
    "Verdict 'revise' if any major or critical issue exists.")


def format_review(r: Review) -> str:
    issues = "\n".join(f"- [{i.severity}] {i.kind} @ {i.where}: {i.detail}" for i in r.issues)
    return (f"VERDICT: {r.verdict}\nISSUES:\n{issues or '- none'}\nCOUNTERARGUMENT: "
            f"{r.strongest_counterargument}\nCEO QUESTIONS: {'; '.join(r.ceo_questions)}")


def review(llm, question: str, client_prior: str, draft_text: str, evidence: str,
           role: str = "partner") -> Review:
    user = (f"CLIENT QUESTION: {question}\nCLIENT'S INITIAL BELIEF: {client_prior or 'none'}"
            f"\n\nDRAFT:\n{draft_text}\n\nEVIDENCE (the only admissible support):\n{evidence}")
    r = llm.structured(role, [{"role": "system", "content": PARTNER_SYS},
                              {"role": "user", "content": user}], Review)
    if r.blocking():
        r.verdict = "revise"  # deterministic: a blocking issue can't be approved
    return r
