"""Phase 7 tooling, offline: backtest date restriction, report + gate logic, backtest runner."""

import json

import httpx

from consultant.agents.research_associate import ResearchAssociate, dated_ok
from consultant.evals import backtests, report
from consultant.evals.judge import RUBRIC
from consultant.research.fact_store import FactStore
from consultant.research.web import Web
from tests.test_research import Provider, make, site


def test_dated_ok():
    assert dated_ok(None, None) and dated_ok("2030-01-01", None)       # no restriction
    assert dated_ok("2024-06-30", "2024-12-31")
    assert not dated_ok("2025-01-02", "2024-12-31")                    # after the cut-off
    assert not dated_ok(None, "2024-12-31")                            # undated: excluded


def test_backtest_mode_reads_only_dated_sources(tmp_path):
    ra, _, store, web = make(tmp_path, answered=True)
    assert ra.research("What was Acme's revenue in FY2024?").facts    # normal: facts found
    site.hits = []
    bt = ResearchAssociate(ra.llm, Web(Provider(), cache_dir=tmp_path / "w2",
                                       sleep=lambda s: None,
                                       client=httpx.Client(transport=httpx.MockTransport(site))),
                           FactStore(), "bt", allow_estimate=False, as_of="2024-12-31")
    res = bt.research("What was Acme's revenue in FY2024?")
    assert res.facts == [] and res.brief.value is None                # fake pages are undated


def row(case, overall, score):
    return {"case_id": case, "overall": overall, "scores": {k: score for k in RUBRIC}}


def write(results, name, rows):
    d = results / name
    d.mkdir(parents=True)
    for r in rows:
        (d / f"{r['case_id']}.json").write_text(json.dumps(r))


def test_report_gate_and_triage(tmp_path):
    write(tmp_path, "system-test", [row("a", 4.0, 4), row("b", 3.5, 2), row("c", 9, 4)])
    write(tmp_path, "baseline", [row("a", 2.0, 2), row("b", 2.5, 2)])
    g = report.gate(report.load_rows(tmp_path, "system-test"),
                    report.load_rows(tmp_path, "baseline"))
    assert g["cases"] == 2 and g["pass"] and len(g["dimensions_won"]) == 9  # only a, b compared
    md = report.render("system-test", "baseline", tmp_path)
    assert "**Result: PASS**" in md and "| scoping |" in md
    assert "research: 2" in md.split("Failure analysis")[1]    # case b: key_insight, evidence


def test_report_fails_when_system_loses_dimensions(tmp_path):
    sys_rows = [{"case_id": "a", "overall": 3.1,
                 "scores": {k: (5 if i < 5 else 1) for i, k in enumerate(RUBRIC)}}]
    write(tmp_path, "s", sys_rows)
    write(tmp_path, "b", [row("a", 3.0, 3)])
    g = report.gate(report.load_rows(tmp_path, "s"), report.load_rows(tmp_path, "b"))
    assert not g["pass"] and len(g["dimensions_won"]) == 5     # aggregate wins, but only 5/9


def test_backtests_skip_unverified_and_score(tmp_path, monkeypatch):
    from tests.test_engagement import RealModeLLM

    class Scorer(RealModeLLM):
        def BacktestVerdict(self, user):
            assert "WHAT HAPPENED" in user and "AS OF" in user
            return {"call_correct": True, "key_points_found": [True, False, True],
                    "calibration": 4, "evidence_quality": 3, "justification": "ok"}
    monkeypatch.setattr(backtests, "OUT", tmp_path / "bt")
    monkeypatch.setattr(backtests, "ROOT", tmp_path)
    (tmp_path / "eval_results").mkdir()
    import consultant.agents.engagement_manager as em
    monkeypatch.setattr(em.Retriever, "load", classmethod(lambda cls, *a, **k: cls([])))
    monkeypatch.setattr(em, "add_primer", lambda *a, **k: None)
    web = Web(Provider(), cache_dir=tmp_path / "w", sleep=lambda s: None,
              client=httpx.Client(transport=httpx.MockTransport(site)))
    out = backtests.run(llm=Scorer(), web=web, root=tmp_path / "eng")
    assert out["done"] == 0 and len(out["skipped_unverified"]) == 3   # all unverified
    out = backtests.run(include_unverified=True, llm=Scorer(), web=web, root=tmp_path / "eng")
    assert out["done"] == 3 and out["calls_correct"] == 3
    assert abs(out["key_point_recall"] - 2 / 3) < 1e-9


def test_split_report_keeps_studied_and_untouched_cases_apart():
    def row(cid, s):
        return {"case_id": cid, "overall": s, "scores": {k: s for k in RUBRIC}}
    studied, new = report.STUDIED[0], "iimb-p999-untouched"
    out = report.split_report([row(studied, 4), row(new, 2)], [row(studied, 3), row(new, 3)])
    s, u, c = out.split("### ")[1:]
    assert "Studied" in s and "n = 1" in s and "+1.00" in s
    assert "Untouched" in u and "n = 1" in u and "-1.00" in u
    assert "Combined" in c and "n = 2" in c and "+0.00" in c
