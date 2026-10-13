"""LLM-extract raw cases into `Case` JSON and industry reports into tagged chunks.

Resumable: existing outputs are skipped. Writes only under data/derived/ (gitignored).
Usage: uv run python -m consultant.knowledge.extract [cases|reports|split|all]
"""

import json
import random
import sys
from collections import Counter, defaultdict

from consultant.config import ROOT
from consultant.knowledge.casebook import RawCase, RawReport, segment
from consultant.knowledge.schema import (
    QA,
    Case,
    CaseAnnotation,
    DataReveal,
    IndustryChunks,
    TurnLabel,
)
from consultant.llm import LLM, StructuredOutputError
from consultant.tracing import Tracer

DERIVED = ROOT / "data/derived"
CASES = DERIVED / "cases"
CHUNKS = DERIVED / "industry_chunks.jsonl"
SEED, N_TEST = 20241013, 15

CASE_SYS = (
    "You annotate a consulting case-interview transcript. Turns are numbered and labelled "
    "INTERVIEWER or CANDIDATE. Label exactly the interviewer turns listed by the user, one "
    "label each: 'data' if the turn gives ANY case information: facts, numbers, findings "
    "('the problem is in X'), causes, constraints or what was observed; 'scoping' if it "
    "answers a clarifying question about the client, objective, geography, business model "
    "or timeline; 'none' ONLY if it contains no information at all (pure acknowledgement, "
    "'proceed', or a question back to the candidate with no facts). The trigger is a short "
    "phrase for what the candidate asked or proposed that drew the reply. Also give: the "
    "candidate's framework as a compact indented outline (max 15 lines); the key insight the "
    "case turns on; the recommendation as the candidate stated it (usually in the last "
    "candidate turns; empty only if the candidate never proposed actions); and as "
    "interviewer_feedback the 'Key Takeaways' bullets from the slide notes. Recommendation "
    "and feedback must contain only what the source says: add nothing, do not evaluate. "
    "Be concise.")

REPORT_SYS = (
    "You turn a one-page industry primer into compact notes split by aspect. Rewrite in your "
    "own words as terse notes; keep numbers with their units and years. One chunk per aspect "
    "present; skip aspects that are absent.")


def build_case(raw: RawCase, ann: CaseAnnotation) -> Case:
    """Answers and data are the interviewer's verbatim words; the LLM only labels turns."""
    labels = {lab.turn: lab for lab in ann.labels}
    qa, reveals, other = [], [], []
    for i, (who, text) in enumerate(raw.turns):
        if i == 0 or who != "interviewer":
            continue
        prev = raw.turns[i - 1][1] if raw.turns[i - 1][0] == "candidate" else ""
        # an unlabelled interviewer turn is kept as data: a missed reveal costs more than noise
        lab = labels.get(i) or TurnLabel(turn=i, kind="data", trigger=prev[:150])
        if lab.kind == "none" and any(ch.isdigit() for ch in text):
            lab = TurnLabel(turn=i, kind="data", trigger=lab.trigger)  # numbers are data
        if lab.kind == "scoping":
            qa.append(QA(question=lab.trigger, answer=text))
        elif lab.kind == "data":
            reveals.append(DataReveal(trigger=lab.trigger, data=text))
        else:  # steers like "focus on pre-purchase" still carry information
            other.append(DataReveal(trigger=lab.trigger, data=text))
    return Case(id=raw.id, title=raw.title, firm=raw.firm, case_type=raw.case_type,
                difficulty=raw.difficulty, source_pages=raw.pages, industry=ann.industry,
                numerical=ann.numerical, problem_statement=raw.turns[0][1], clarifying_qa=qa,
                structure=ann.structure, data_reveals=reveals, key_insight=ann.key_insight,
                recommendation=ann.recommendation,
                interviewer_feedback=ann.interviewer_feedback, other_replies=other,
                transcript=[{"role": w, "content": t} for w, t in raw.turns])


def extract_case(llm: LLM, raw: RawCase, role: str) -> Case:
    todo = [i for i, (who, _) in enumerate(raw.turns) if who == "interviewer" and i > 0]
    msgs = [{"role": "system", "content": CASE_SYS},
            {"role": "user", "content": f"Case: {raw.title} ({raw.case_type}, {raw.difficulty}, "
                                        f"{raw.firm})\n\nTRANSCRIPT:\n{raw.text}\n\n"
                                        f"Interviewer turns to label: {todo}"}]
    return build_case(raw, llm.structured(role, msgs, CaseAnnotation))


def extract_cases(llm: LLM, raws: list[RawCase]) -> None:
    out = CASES / "all"
    out.mkdir(parents=True, exist_ok=True)
    roles = ("case_extractor", "case_extractor_alt")  # spread the load over two daily quotas
    for i, raw in enumerate(raws):
        path = out / f"{raw.id}.json"
        if path.exists():
            continue
        try:
            case = extract_case(llm, raw, roles[i % 2])
        except StructuredOutputError:  # e.g. qwen's 1K output cap truncates long cases
            case = extract_case(llm, raw, roles[(i + 1) % 2])
        path.write_text(case.model_dump_json(indent=1), encoding="utf-8")
        print(f"[{i + 1}/{len(raws)}] {raw.id}: {len(case.data_reveals)} reveals", flush=True)


def extract_reports(llm: LLM, reports: list[RawReport]) -> None:
    done = {json.loads(x)["source_page"] for x in CHUNKS.read_text(encoding="utf-8").splitlines()
            } if CHUNKS.exists() else set()
    for r in reports:
        if r.page in done:
            continue
        msgs = [{"role": "system", "content": REPORT_SYS},
                {"role": "user", "content": f"Industry: {r.industry}\n\n{r.text}"}]
        chunks = llm.structured("extractor", msgs, IndustryChunks).chunks
        with CHUNKS.open("a", encoding="utf-8") as f:
            for c in chunks:
                c.industry, c.source_page = r.industry, r.page
                f.write(c.model_dump_json() + "\n")
        print(f"{r.industry}: {len(chunks)} chunks", flush=True)


def load_cases(split: str = "all") -> list[Case]:
    return [Case.model_validate_json(p.read_text(encoding="utf-8"))
            for p in sorted((CASES / split).glob("*.json"))]


def stratified_split(cases: list[Case], n_test: int = N_TEST, seed: int = SEED):
    """Allocate test slots per case type by largest remainder (>=1 per type with >=2 cases),
    then within a type pick across difficulties."""
    rng = random.Random(seed)
    by_type = defaultdict(list)
    for c in sorted(cases, key=lambda c: c.id):
        by_type[c.case_type].append(c)
    quota = {t: n_test * len(v) / len(cases) for t, v in by_type.items()}
    alloc = {t: max(int(q), 1 if len(by_type[t]) >= 2 else 0) for t, q in quota.items()}
    for t in sorted(quota, key=lambda t: quota[t] - int(quota[t]), reverse=True):
        if sum(alloc.values()) >= n_test:
            break
        alloc[t] += 1
    test = []
    for t, group in by_type.items():
        rng.shuffle(group)
        group.sort(key=lambda c: c.difficulty)  # stable: shuffled within difficulty
        step = len(group) / alloc[t] if alloc[t] else 0
        test += [group[int(i * step)] for i in range(alloc[t])]
    test_ids = {c.id for c in test}
    return [c for c in cases if c.id not in test_ids], [c for c in cases if c.id in test_ids]


def write_split() -> None:
    train, test = stratified_split(load_cases("all"))
    for name, group in (("train", train), ("test", test)):
        d = CASES / name
        d.mkdir(parents=True, exist_ok=True)
        for old in d.glob("*.json"):
            old.unlink()
        for c in group:
            (d / f"{c.id}.json").write_text(c.model_dump_json(indent=1), encoding="utf-8")
    (CASES / "split.json").write_text(json.dumps(
        {"seed": SEED, "train": [c.id for c in train], "test": [c.id for c in test]}, indent=1))
    print("train", len(train), "test", len(test), Counter(c.case_type for c in test),
          Counter(c.difficulty for c in test))


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    raws, reports = segment()
    llm = LLM(tracer=Tracer(run_id=f"extract-{what}"))
    if what in ("cases", "all"):
        extract_cases(llm, raws)
    if what in ("reports", "all"):
        extract_reports(llm, reports)
    if what in ("split", "all"):
        write_split()
