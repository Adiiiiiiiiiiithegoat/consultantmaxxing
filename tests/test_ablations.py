"""Offline wiring of the three ablation variants (A: Opus brain, B: single-brain synthesis,
C: baseline+) and the side-by-side report. Fictional bakery case, scripted LLM."""

from consultant.agents.engagement_manager import Engagement
from consultant.config import load_yaml, models_config
from consultant.evals import baseline, report
from consultant.evals.judge import RUBRIC
from consultant.evals.simulator import Interviewer
from consultant.knowledge.retrieval import Retriever
from consultant.workspace import Workspace
from tests.test_engagement import ScriptedLLM
from tests.test_simulator import BAKERY


def test_a_opus_brain_changes_only_the_manager_and_storyliner(monkeypatch):
    monkeypatch.setenv("CONSULTANT_MODELS_FILE", "models.claude-opus.yaml")
    a = models_config()
    base = load_yaml("models.claude.yaml")
    assert "extends" not in a
    assert a["roles"]["engagement_manager"]["model"] == "claude_cli/opus"
    assert a["roles"]["storyliner"]["model"] == "claude_cli/opus"
    assert {k: v for k, v in a["roles"].items() if k not in ("engagement_manager", "storyliner")
            } == {k: v for k, v in base["roles"].items()
                  if k not in ("engagement_manager", "storyliner")}
    assert a["providers"] == base["providers"] and a["models"] == base["models"]
    assert base["roles"]["engagement_manager"]["model"] == "claude_cli/sonnet"  # default kept


class SingleBrain(ScriptedLLM):
    """Every review blocks; the second finds more, so the first draft should ship."""

    def __init__(self):
        super().__init__()
        self.story_inputs: list[str] = []

    def StoryDraft(self, user):
        self.story_inputs.append(user)
        return super().StoryDraft(user)

    def Review(self, user):
        self.reviews += 1
        issue = {"kind": "unsupported_claim", "severity": "major", "where": "arg 1",
                 "detail": "no number"}
        return {"issues": [issue] * self.reviews, "strongest_counterargument": "footfall",
                "ceo_questions": ["a", "b", "c"], "agrees_with_client_prior": False,
                "prior_agreement_justified": False, "verdict": "revise"}


def test_b_single_brain_writes_from_the_full_context_with_one_revision(tmp_path):
    llm = SingleBrain()
    ws = Workspace.open("b", root=tmp_path, mode="case", profile="claude-single")
    iv = Interviewer(BAKERY, llm)
    Engagement(llm, ws, interviewer=iv, retriever=Retriever([])).run()
    st = ws.state
    assert st.steps_done[-6:] == ["story0", "review0", "story1", "review1", "repair", "deliver"]
    assert llm.reviews == 2 and len(llm.story_inputs) == 2   # one review, one revision
    assert st.notes["shipped_draft"] == "story0"             # best reviewed draft ships
    first, revision = llm.story_inputs
    assert BAKERY.problem_statement in first and "FULL INTERVIEW TRANSCRIPT" in first
    assert all(e["question"] in first and e["reply"] in first for e in iv.log)
    assert "fact count" in first and "n_numeric_facts" in first     # the model + its inputs
    assert all(f"F{f.id}: {f.claim}" in first for f in ws.facts.facts("b"))
    assert "HYPOTHESES:" not in first                           # not the brief
    assert revision.startswith(first) and "PARTNER REVIEW" in revision
    assert "YOUR PREVIOUS DRAFT" in revision


def test_default_profile_still_writes_from_the_brief(tmp_path):
    llm = SingleBrain()
    ws = Workspace.open("d", root=tmp_path, mode="case", profile="claude")
    Engagement(llm, ws, interviewer=Interviewer(BAKERY, llm), retriever=Retriever([])).run()
    assert "HYPOTHESES:" in llm.story_inputs[0]
    assert "FULL INTERVIEW TRANSCRIPT" not in llm.story_inputs[0]
    assert len(llm.story_inputs) == 3 and llm.reviews == 3     # MAX_REVIEW_ROUNDS unchanged


class PlusLLM(ScriptedLLM):
    def __init__(self, turns):
        super().__init__()
        self.turns, self.prompts, self.matched = list(turns), [], []

    def structured(self, role, msgs, schema):
        if schema.__name__ == "PlusTurn":
            self.prompts.append(msgs)
            return schema(**self.turns.pop(0))
        self.matched.append(msgs[-1]["content"])
        return super().structured(role, msgs, schema)


def test_c_baseline_plus_has_playbooks_and_the_sandbox(tmp_path):
    llm = PlusLLM([{"action": "ask", "content": "How did costs change?"},
                   {"action": "analyse", "content": "RESULT = {'margin': round((10 - 6) / 10, 2)}"},
                   {"action": "analyse", "content": "RESULT = 1 / 0"},
                   {"action": "final_answer", "content": "Raise prices: margin is 0.4."}])
    sub = baseline.run_case(llm, BAKERY, sandbox_dir=tmp_path)
    system = llm.prompts[0][0]["content"]
    assert "Playbook: profitability" in system and "break_even_units" in system
    assert "'analyse'" in system and baseline.BASELINE_SYS not in system
    assert '{"margin": 0.4}' in llm.prompts[2][1]["content"]     # the candidate sees RESULT
    assert "FAILED" in llm.prompts[3][1]["content"] and "ZeroDivisionError" in \
        llm.prompts[3][1]["content"]
    assert not any("RESULT" in m for m in llm.matched)           # interviewer never sees code
    assert [t["role"] for t in sub.transcript] == ["interviewer", "candidate", "interviewer"]
    assert sub.questions_asked == 1 and sub.final_answer.startswith("Raise prices")
    assert "A1:" in sub.work and '{"margin": 0.4}' in sub.work and "A2:" in sub.work
    assert len(list(tmp_path.iterdir())) == 2                    # both runs kept on disk


def test_c_runs_out_of_budget_and_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(baseline, "MAX_QUESTIONS", 0)
    monkeypatch.setattr(baseline, "MAX_ANALYSES", 1)
    llm = PlusLLM([{"action": "analyse", "content": "RESULT = {'x': 1}"},
                   {"action": "analyse", "content": "Final: do X."}])
    sub = baseline.run_case(llm, BAKERY, sandbox_dir=tmp_path)
    assert sub.final_answer == "Final: do X." and "final answer now" in \
        llm.prompts[1][1]["content"]


def test_compare_report_puts_runs_side_by_side_on_common_cases():
    def row(cid, s):
        return {"case_id": cid, "overall": s, "scores": {k: s for k in RUBRIC}}
    studied, new = report.STUDIED[0], "iimb-p999-untouched"
    runs = {"system-v2": [row(studied, 3), row(new, 2)], "baseline": [row(studied, 4),
                                                                        row(new, 3)],
            "A": [row(studied, 5)]}                       # unfinished: only common cases count
    out = report.compare(runs)
    s, u, a = out.split("### ")[1:]
    assert "n = 1" in s and "| system-v2 | baseline | A |" in s
    assert "| scoping | 3.00 | 4.00 | 5.00 |" in s and "**5.00**" in s
    assert "n = 0" in u and "|" not in u
    assert "n = 1" in a
