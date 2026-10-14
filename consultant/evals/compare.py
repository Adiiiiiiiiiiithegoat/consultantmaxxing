"""`consult compare <problem.md> <human_answer.md>`: the system against a human answer, blind.

1. Run the system on the problem (real mode, research on the open web).
2. Put both answers through the same blinding: plain text, no markdown, no evidence ids, no
   system-only sections (validation flags, review notes, removed-figure notes), identical
   layout. Assign A/B at random; the mapping goes to key/mapping.json, apart from the blinded
   files.
3. Judge each blinded answer in its own fresh call (no cache, no shared context) on a
   reference-free rubric, since a real problem has no answer key.
Output in engagements/<name>/: A.md, B.md, scores.json (by label), key/mapping.json,
result.json (unblinded, written last).
"""

import json
import re
import secrets
from pathlib import Path

from pydantic import BaseModel, Field

from consultant.config import ROOT

RUBRIC = {
    "recommendation": "A clear, committed recommendation that answers the question asked. "
                      "1 = none or evasive; 5 = specific, actionable, prioritised.",
    "reasoning": "The recommendation follows from the analysis; trade-offs are weighed. "
                 "1 = assertion; 5 = tight logic from evidence to conclusion.",
    "evidence": "Claims rest on specific, checkable facts and numbers, not generalities. "
                "1 = unsupported; 5 = every key claim backed.",
    "insight": "Finds the non-obvious driver or implication rather than restating the "
               "problem. 1 = generic; 5 = sharp and specific to this situation.",
    "communication": "Answer first, structured, concise. 1 = rambling; 5 = crisp.",
}
BLIND_SYS = ("You are a senior partner assessing one answer to a client's business question. "
             "You do not know who wrote it. Score each dimension 1-5 with a one-sentence "
             "justification. Do not reward length or confident tone.\n\nRUBRIC:\n"
             + "\n".join(f"- {k}: {v}" for k, v in RUBRIC.items()))


class Dim(BaseModel):
    justification: str
    score: int = Field(ge=1, le=5)


class BlindScore(BaseModel):
    recommendation: Dim
    reasoning: Dim
    evidence: Dim
    insight: Dim
    communication: Dim

    def total(self) -> float:
        return sum(getattr(self, k).score for k in RUBRIC) / len(RUBRIC)


_SYSTEM_ONLY = re.compile(r"\n#+\s*(?:⚠|Figures removed as unverified|Validation flags|"
                          r"Unresolved review points).*", re.S)


def blind(text: str) -> str:
    """Same normalisation for both answers, so format can't give the author away."""
    text = _SYSTEM_ONLY.sub("\n", text.replace("\r\n", "\n"))
    text = re.sub(r"(?m)^\s*[-*•]\s+", "- ", text)                     # one bullet style
    text = re.sub(r"(?m)^#+\s*", "", text)                             # headings -> lines
    text = re.sub(r"\*\*|__|(?<![\w-])[*_](?=\S)|(?<=\S)[*_](?!\w)", "", text)  # emphasis
    text = re.sub(r"(?mi)^(?:evidence(?: ids)?|rests on):.*$", "", text)  # system-only lines
    text = re.sub(r"\s*\[(?:[FAS]\d+(?:,\s*)?)+\]|\s*\((?:[FAS]\d+(?:,\s*)?)+\)", "", text)
    text = text.replace("[unverified figure]", "an unconfirmed figure")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def layout(problem: str, label: str, answer: str) -> str:
    return f"CLIENT QUESTION\n{problem.strip()}\n\nANSWER {label}\n{blind(answer)}\n"


def judge_blind(llm, problem: str, text: str, role: str = "judge") -> BlindScore:
    """One fresh call per answer: no cache, nothing shared with the other answer's call."""
    return llm.structured(role, [{"role": "system", "content": BLIND_SYS},
                                 {"role": "user", "content": text}], BlindScore, cache=False)


def run_system(problem: str, name: str, profile: str) -> str:
    from consultant.agents.engagement_manager import Engagement
    from consultant.llm import LLM
    from consultant.research.web import Web
    from consultant.tracing import Tracer
    from consultant.workspace import Workspace
    tracer = Tracer(run_id=f"compare-{name}")
    ws = Workspace.open(name, root=ROOT / "engagements", mode="real", profile=profile,
                        problem_statement=problem)
    Engagement(LLM(tracer=tracer), ws, web=Web(tracer=tracer)).run()
    return (ws.dir / "deliverables" / "memo.md").read_text(encoding="utf-8")


def compare(problem_md: Path, human_md: Path, name: str, profile: str = "claude", llm=None,
            system_answer: str | None = None, rng=None) -> dict:
    problem = problem_md.read_text(encoding="utf-8")
    human = human_md.read_text(encoding="utf-8")
    system = system_answer if system_answer is not None else run_system(problem, name, profile)
    out = ROOT / "engagements" / name
    (out / "key").mkdir(parents=True, exist_ok=True)
    labels = ["A", "B"]
    rng = rng or secrets.SystemRandom()
    rng.shuffle(labels)
    mapping = {labels[0]: "system", labels[1]: "human"}
    (out / "key" / "mapping.json").write_text(json.dumps(mapping, indent=1), encoding="utf-8")
    texts = {lab: layout(problem, lab, system if who == "system" else human)
             for lab, who in mapping.items()}
    for lab, t in texts.items():
        (out / f"{lab}.md").write_text(t, encoding="utf-8")
    if llm is None:
        from consultant.llm import LLM
        from consultant.tracing import Tracer
        llm = LLM(tracer=Tracer(run_id=f"compare-{name}-judge"))
    scores = {lab: judge_blind(llm, problem, texts[lab]) for lab in sorted(texts)}
    (out / "scores.json").write_text(json.dumps({k: v.model_dump() for k, v in scores.items()},
                                                indent=1), encoding="utf-8")
    result = {"dir": str(out), **{who: {"label": lab, "total": round(scores[lab].total(), 2),
                                        "scores": {k: getattr(scores[lab], k).score
                                                   for k in RUBRIC}}
                                  for lab, who in mapping.items()}}
    (out / "result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    return result
