"""Phase 6 gate (offline): number traceability, live spreadsheet formulas, action titles."""

import json

from openpyxl import load_workbook

from consultant.agents.storyliner import Argument, Draft, Slide
from consultant.deliverables import model_xlsx, render
from consultant.deliverables.validate import unsupported_numbers, validate_draft
from consultant.research.fact_store import Assumption, Fact
from consultant.workspace import Workspace


def workspace(tmp_path):
    ws = Workspace.create("d1", root=tmp_path, problem_statement="Profits fell 20%; why?",
                          scope={"restated_question": "Why did profits fall 20%?"})
    base = dict(engagement_id="d1", source_type="client", credibility=5,
                source_url="case://x/interviewer")
    ws.facts.add(Fact(claim="Base costs", value=19, unit="crore", quote="costs were Rs 19 crore",
                      **base))                                              # F1
    ws.facts.add(Fact(claim="Current costs", value=22, unit="crore",
                      quote="costs rose to Rs 22 crore", **base))           # F2
    ws.facts.add_assumption(Assumption(engagement_id="d1", name="price_rise", value=0.1,
                                       rationale="match rivals' 12% partly"))  # S1
    ws.run_analysis("cost increase", "size the cost rise",
                    "RESULT = {'increase_cr': 22 - 19, 'increase_pct': (22 - 19) / 19 * 100, "
                    "'irr': 0.1306}",
                    formulas=[{"name": "increase_cr", "expression": "F2 - F1", "unit": "crore"},
                              {"name": "increase_pct", "expression": "increase_cr / F1 * 100",
                               "unit": "%"}])                               # A1
    return ws


def good_draft():
    return Draft(answer="Costs rose 15.79% while revenue was flat; raise prices about 10%.",
                 arguments=[Argument(title="Costs rose by Rs 3 crore, from 19 to 22",
                                     text="That is 15.79%.", refs=["F1", "F2", "A1"]),
                            Argument(title="A 10% price rise closes most of the gap",
                                     text="Estimate.", refs=["S1"])],
                 risks=["Price-sensitive buyers may leave"], next_steps=["Test prices"],
                 open_questions=[], confidence="medium",
                 slides=[Slide(action_title="Input costs, not demand, explain the 20% fall",
                               points=["costs from 19 to 22 crore"], refs=["F1", "F2"])])


def test_formulas_recalculate_to_sandbox_values_and_are_live(tmp_path):
    ws = workspace(tmp_path)
    assert ws.state.analyses[0].formula_issues == []
    path = tmp_path / "model.xlsx"
    stats = model_xlsx.build(ws, path)
    assert stats == {"formulas": 2, "inputs": 3}
    assert model_xlsx.check(path) == []
    model = load_workbook(path)["Model"]
    assert str(model["C2"].value).startswith("=Inputs!")             # a real formula
    live = model_xlsx.recalculate(path)
    assert abs(live[("Model", "C3")] - 300 / 19) < 1e-9
    wb = load_workbook(path)                                           # change an input...
    wb["Inputs"]["C3"] = 25                                            # F2: 22 -> 25
    wb.save(path)
    assert abs(model_xlsx.recalculate(path)[("Model", "C2")] - 6) < 1e-9  # ...model follows
    assert any("increase_cr" in i for i in model_xlsx.check(path))     # and differs from sandbox


def test_formula_mismatch_is_caught_at_analysis_time(tmp_path):
    ws = workspace(tmp_path)
    rec = ws.run_analysis("wrong", "x", "RESULT = {'gap': 4}",
                          formulas=[{"name": "gap", "expression": "F2 - F1"}])
    assert rec.formula_issues and "sandbox gave 4" in rec.formula_issues[0]


def test_validator_traces_numbers_to_cited_evidence(tmp_path):
    ws = workspace(tmp_path)
    assert validate_draft(ws, good_draft()) == []
    bad = good_draft()
    bad.arguments[0].text = "Market share fell 12% and costs rose 15.79%."
    bad.slides.append(Slide(action_title="Overview", points=[], refs=[]))
    bad.arguments[1].refs = ["S1", "F99"]
    kinds = {(i["kind"], i["where"]) for i in validate_draft(ws, bad)}
    assert ("unsupported_number", "argument 1") in kinds        # 12 isn't in F1/F2/A1
    assert ("no_action_title", "slide 2") in kinds
    assert any(k == "unknown_ref" for k, _ in kinds)


def test_number_matching_rules():
    pool = [0.1579, 1536700.0, 22.0]
    assert unsupported_numbers("rose 15.79% (or 15.8%)", pool) == []     # fraction <-> percent
    assert unsupported_numbers("in 2024 and FY2023 it hit 22", pool) == []  # years ignored
    assert unsupported_numbers("1. first point 2. second", [9.0]) == []     # list numbers
    assert unsupported_numbers("about 23", pool) == ["23"]


def test_render_writes_every_deliverable(tmp_path):
    ws = workspace(tmp_path)
    out = render(ws, good_draft())
    for key in ("memo", "storyline", "storyline_md", "model", "sources", "assumptions",
                "validation"):
        assert (ws.dir / out[key]).exists(), key
    report = json.loads((ws.dir / out["validation"]).read_text(encoding="utf-8"))
    assert report["passed"], report
    memo = (ws.dir / out["memo"]).read_text(encoding="utf-8")
    assert memo.index("## Answer") < memo.index("## Why") and "Validation flags" not in memo
    sources = (ws.dir / out["sources"]).read_text(encoding="utf-8")
    assert "| F1 |" in sources and "5/5" in sources
    assert "ASSUMPTION" not in sources and "price_rise" in \
        (ws.dir / out["assumptions"]).read_text(encoding="utf-8")


def test_render_flags_untraceable_numbers_in_the_memo(tmp_path):
    ws = workspace(tmp_path)
    d = good_draft()
    d.answer = "Profits fell because market share dropped 12%."
    render(ws, d)
    memo = (ws.dir / "deliverables/memo.md").read_text(encoding="utf-8")
    assert "Validation flags" in memo and "12" in memo.split("Validation flags")[1]
    assert not json.loads((ws.dir / "deliverables/validation.json").read_text())["passed"]


def test_engagement_validator_forces_revision_when_partner_approves(tmp_path):
    from tests.test_engagement import ScriptedLLM, engagement

    class Untraceable(ScriptedLLM):
        drafts = 0

        def Draft(self, user):
            Untraceable.drafts += 1
            d = super().Draft(user)
            if Untraceable.drafts == 1:
                d["answer"] = "Costs rose 47% (a number nobody gave us)."
            return d

        def Review(self, user):                 # partner always approves
            self.reviews += 1
            return {"issues": [], "strongest_counterargument": "", "ceo_questions": [],
                    "agrees_with_client_prior": False, "prior_agreement_justified": True,
                    "verdict": "approve"}
    llm = Untraceable()
    eng, ws, _ = engagement(tmp_path, llm, eid="val")
    out = eng.run()
    assert Untraceable.drafts == 2 and "story1" in ws.state.steps_done
    assert "47" not in out["draft"].answer
    r0 = ws.state.notes["review0"]
    assert r0["verdict"] == "revise" and "[validator]" in r0["issues"][0]["detail"]


def test_enumeration_only_skips_counting_sequences():
    assert unsupported_numbers("Revenue grew by 2. Then it fell.", [9.0]) == ["2"]
    # smoke-asianpaints-2 false positives: "(1) ... (2) ..." lists and fiscal-year labels
    assert unsupported_numbers("prioritize (1) pricing, (2) mix, (3) costs", [9.0]) == []
    assert unsupported_numbers("FY22‑23 vs FY24-25, FY'24 and FY2023-24", [9.0]) == []
    assert unsupported_numbers("FY24 margin was 23", [9.0]) == ["23"]  # data still checked


def test_formula_that_hardcodes_an_input_is_rejected(tmp_path):
    ws = workspace(tmp_path)
    rec = ws.run_analysis("typed", "x", "RESULT = {'gap': 3, 'pct': 15.79}",
                          formulas=[{"name": "gap", "expression": "22.0 - 19.0"},
                                    {"name": "pct", "expression": "gap / F1 * 100"}])
    assert any("hard-codes 22.0; reference F2" in i for i in rec.formula_issues)
    assert not any(i.startswith("pct: hard-codes") for i in rec.formula_issues)  # 100 is fine


def test_validation_report_fails_on_non_live_formulas(tmp_path):
    ws = workspace(tmp_path)
    ws.run_analysis("typed", "x", "RESULT = {'gap': 3}",
                    formulas=[{"name": "gap", "expression": "22 - 19"}])
    render(ws, good_draft())
    report = json.loads((ws.dir / "deliverables/validation.json").read_text())
    assert not report["passed"] and report["formula_issues"]
