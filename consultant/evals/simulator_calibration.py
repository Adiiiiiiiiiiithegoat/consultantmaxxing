"""Calibrate the case interviewer against the casebook's own interviews (train split only).

Positives: in each sampled case's real transcript, a candidate turn whose real interviewer
reply hands over a case item (the reply contains most of that item's text). The simulator
must answer that question with that item.
Negatives: fishing, vague and off-topic questions (written for this repo) asked against every
sampled case. The simulator must refuse them.

The sets are built at run time from data/derived/ (private; never committed). The 6 cases of
the planned system-vs-baseline re-run are excluded so the harness isn't tuned on its own eval
cases; the sample is split into a fit half (used to set thresholds) and a check half.
Usage: uv run python -m consultant.evals.simulator_calibration
"""

import json
import random

from consultant.knowledge.retrieval import tokenize
from consultant.knowledge.schema import Case

RERUN_CASES = {"iimb-p012-semiconductor-chip-manufacturer", "iimb-p017-indian-domestic-airline",
               "iimb-p025-defence-sector", "iimb-p031-sustainability-strategy-for-fmcg",
               "iimb-p033-packaging-manufacturer", "iimb-p039-port-operator"}
CONTAINS = 0.6  # share of an item's words found in the real reply for it to count as given

FISHING = ["Can you give me all the data you have?", "What other information do you have?",
           "Is there anything else I should know?", "Please share everything about the client.",
           "Tell me all the numbers.", "Do you have any more data?",
           "What should I ask you next?", "Can you just tell me the answer?",
           "What is the key insight of this case?", "Any other details?"]
OFF_TOPIC = ["What is the CEO's favourite sport?", "What will the weather be like next week?",
             "How many moons does Jupiter have?", "What is the client's stock price going to "
             "be next year?", "Which football club does the CFO support?",
             "What is the capital of Australia?"]
# Check-half negatives are worded differently, so a filter tuned on the fit half is tested on
# phrasings it hasn't seen.
FISHING_CHECK = ["Could you share whatever data you've got?", "Anything more you can tell me?",
                 "What other numbers are available?", "Give me every detail of the case.",
                 "Is there more information I'm missing?", "What's the right answer here?",
                 "Which question should I be asking?", "Dump all the figures, please.",
                 "Do you have further details?", "What is the main takeaway of the case?"]
OFF_TOPIC_CHECK = ["Where did the founder go to school?", "What's a good restaurant nearby?",
                   "How tall is Mount Everest?", "Which political party will win the next "
                   "election?", "What car does the CEO drive?", "What time is it in Tokyo?"]
NEGATIVES = {"fit": FISHING + OFF_TOPIC, "check": FISHING_CHECK + OFF_TOPIC_CHECK}


def items_of(case: Case) -> list[tuple[str, str]]:
    """(subject, text) per item, in the simulator's order."""
    return ([(qa.question, qa.answer) for qa in case.clarifying_qa]
            + [(r.trigger, r.data) for r in case.data_reveals + case.other_replies])


def sample(cases: list[Case], n: int = 20, seed: int = 0) -> tuple[list[Case], list[Case]]:
    pool = sorted((c for c in cases if c.id not in RERUN_CASES), key=lambda c: c.id)
    picked = random.Random(seed).sample(pool, min(n, len(pool)))
    return picked[: len(picked) // 2], picked[len(picked) // 2:]


def positives(case: Case) -> list[tuple[str, set[int]]]:
    items = [set(tokenize(text)) for _, text in items_of(case)]
    t = case.transcript
    out = []
    for prev, nxt in zip(t, t[1:], strict=False):
        if prev["role"] != "candidate" or nxt["role"] != "interviewer":
            continue
        reply = set(tokenize(nxt["content"]))
        given = {i for i, words in enumerate(items)
                 if words and len(words & reply) / len(words) >= CONTAINS}
        if given:
            out.append((prev["content"], given))
    return out


def evaluate(match, cases: list[Case], negatives: list[str] = FISHING + OFF_TOPIC) -> dict:
    """match(case, question) -> item ids. Rates: positives answered with an expected item,
    negatives refused."""
    pos = [(c, q, want) for c in cases for q, want in positives(c)]
    neg = [(c, q) for c in cases for q in negatives]
    got = [(set(match(c, q)), want) for c, q, want in pos]
    hit = sum(bool(g & want) for g, want in got)
    given = sum(len(g) for g, _ in got)
    right = sum(len(g & want) for g, want in got)
    refused = sum(not match(c, q) for c, q in neg)
    return {"cases": len(cases), "positives": len(pos),
            "positive_answered_right": round(hit / len(pos), 3),
            "positive_answered_any": round(sum(bool(g) for g, _ in got) / len(pos), 3),
            # share of items handed over that the question actually got in the real interview
            "positive_precision": round(right / given, 3) if given else None,
            "negatives": len(neg), "negative_refused": round(refused / len(neg), 3)}


# The matcher before calibration, kept verbatim so "before" can be re-measured on any model.
LEGACY_SYS = (
    "You are the interviewer in a consulting case interview. You hold a list of case items. "
    "Given the candidate's latest question, return the ids of the items that the question "
    "specifically asks for, or that the analysis the candidate proposes would directly need. "
    "Rules: a question must target the item's subject (e.g. asking about costs unlocks cost "
    "data, not customer data). Vague or generic questions ('any other information?', 'what "
    "else should I know?') unlock nothing. Questions about things absent from the list "
    "unlock nothing. When unsure, return no ids.")


def matcher(llm, kind: str):
    from consultant.evals.simulator import Interviewer, Match

    def current(case, q):
        iv = Interviewer(case, llm)
        iv.ask(q)
        return iv.log[-1]["item_ids"]

    def legacy(case, q):
        iv = Interviewer(case, llm)
        msgs = [{"role": "system", "content": LEGACY_SYS},
                {"role": "user", "content": f"CASE: {case.problem_statement}\n\n"
                                            f"ITEMS:\n{iv._menu()}\n\nCANDIDATE: {q}"}]
        m = llm.structured(iv.role, msgs, Match)
        return sorted({i for i in m.item_ids if 0 <= i < len(iv.items)})
    return {"current": current, "legacy": legacy}[kind]


def main(argv=None) -> None:
    import argparse

    from consultant.config import ROOT, models_config
    from consultant.knowledge.extract import load_cases
    from consultant.llm import LLM
    ap = argparse.ArgumentParser()
    ap.add_argument("--matcher", choices=["current", "legacy"], default="current")
    ap.add_argument("--model", default=None, help="override the interviewer model, e.g. "
                    "ollama/qwen3:4b-instruct (default: the interviewer role's model)")
    a = ap.parse_args(argv)
    cfg = models_config()
    if a.model:
        cfg["roles"]["interviewer"] = {"model": a.model, "params": {"temperature": 0.0,
                                                                    "max_tokens": 300}}
    match = matcher(LLM(cfg), a.matcher)
    fit, check = sample(load_cases("train"))
    label = f"{a.matcher}@{cfg['roles']['interviewer']['model']}"
    out = {"fit": evaluate(match, fit, NEGATIVES["fit"]),
           "check": evaluate(match, check, NEGATIVES["check"])}
    print(json.dumps({label: out}, indent=1), flush=True)
    path = ROOT / "eval_results" / "simulator_calibration.json"
    path.parent.mkdir(exist_ok=True)
    saved = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    saved[label] = out
    path.write_text(json.dumps(saved, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
