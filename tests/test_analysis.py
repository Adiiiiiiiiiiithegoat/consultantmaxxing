"""Phase 3: analysis templates (known answers), sandbox guarantees, kill-and-resume."""

import subprocess
import sys
from pathlib import Path

import pytest

from consultant.analysis import sandbox
from consultant.analysis import templates as t
from consultant.workspace import Hypothesis, IssueNode, Workspace


# ---- templates: known answers ---------------------------------------------------------------
def test_market_sizing():
    assert t.top_down(1e9, 0.5, 0.2) == pytest.approx(1e8)
    assert t.bottom_up(1_000, 50, 12) == 600_000


def test_break_even():
    assert t.break_even_units(1000, 15, 5) == 100
    assert t.break_even_revenue(1000, 0.25) == 4000
    with pytest.raises(ValueError):
        t.break_even_units(1000, 5, 5)


def test_unit_economics():
    u = t.unit_economics(price=100, variable_cost=60, cac=200, orders_per_year=4, years=3,
                         churn_per_year=0.5)
    assert (u["annual_contribution"], u["lifetime_years"], u["ltv"]) == (160, 2, 320)
    assert u["ltv_to_cac"] == pytest.approx(1.6) and u["cac_payback_years"] == 1.25


def test_price_volume_mix_reconciles():
    r = t.price_volume_mix({"A": (100, 10), "B": (100, 20)}, {"A": (120, 11), "B": (100, 20)})
    assert (r["base_revenue"], r["current_revenue"]) == (3000, 3320)
    assert (r["volume"], r["mix"], r["price"]) == (300, -100, 120)
    assert r["check"] == pytest.approx(0)


def test_margin_bridge_reconciles():
    r = t.margin_bridge({"revenue": 1000, "cogs": -600, "sga": -200},
                        {"revenue": 1100, "cogs": -700, "sga": -190})
    assert r["steps"] == {"cogs": -100, "revenue": 100, "sga": 10}
    assert (r["base_profit"], r["current_profit"], r["check"]) == (200, 210, 0)


def test_scenarios_and_sensitivity():
    def f(price, units):
        return price * units
    base = {"price": 10, "units": 100}
    assert t.sensitivity(f, base, {"price": [9, 11]}) == {"price": [(9, 900), (11, 1100)]}
    assert t.scenario_table(f, {"bear": {"units": 80}, "bull": {"units": 120}}, base) == \
        {"bear": 800, "bull": 1200}
    assert t.grid(f, base, "price", [1, 2], "units", [10, 20]) == [[10, 20], [20, 40]]


def test_npv_irr_payback_cagr():
    assert t.npv(0.1, [-100, 60, 60]) == pytest.approx(4.1322314)
    assert t.irr([-100, 60, 60]) == pytest.approx(0.1306624, abs=1e-6)   # 60x+60x^2=100
    flows = [-1000, 300, 400, 500]
    assert t.npv(t.irr(flows), flows) == pytest.approx(0, abs=1e-6)
    assert t.payback_period([-100, 30, 40, 50]) == pytest.approx(2.6)
    assert t.payback_period([-100, 10, 10]) is None
    assert t.cagr(100, 121, 2) == pytest.approx(0.1)
    assert t.present_value_from_cagr(50, 0.15, 10) == pytest.approx(12.3592, abs=1e-3)
    with pytest.raises(ValueError):
        t.irr([100, 50])


# ---- sandbox guarantees ---------------------------------------------------------------------
def test_sandbox_runs_code_and_saves_everything(tmp_path):
    code = ("from consultant.analysis.templates import npv\n"
            "print('hello')\nRESULT = {'npv': npv(0.1, [-100, 60, 60])}")
    r = sandbox.run(code, tmp_path, "npv check")
    assert r.ok and r.result["npv"] == pytest.approx(4.1322314) and "hello" in r.stdout
    for f in ("code.py", "result.json", "stdout.txt", "stderr.txt", "run.json"):
        assert (r.workdir / f).exists()


def test_sandbox_blocks_network(tmp_path):
    r = sandbox.run("import urllib.request\nurllib.request.urlopen('http://example.com')",
                    tmp_path, "net")
    assert not r.ok and "network access is disabled" in r.error
    r = sandbox.run("import socket\nsocket.create_connection(('1.1.1.1', 53))", tmp_path, "sock")
    assert not r.ok and "network access is disabled" in r.error


def test_sandbox_times_out_runaway_code(tmp_path):
    r = sandbox.run("while True:\n    pass", tmp_path, "loop", timeout=1.5)
    assert not r.ok and "timed out" in r.error and r.seconds < 10


def test_sandbox_memory_cap(tmp_path):
    r = sandbox.run("x = bytearray(400 * 1024 * 1024)\nimport time\ntime.sleep(5)", tmp_path,
                    "hog", mem_mb=100, timeout=20)
    assert not r.ok and "memory cap" in r.error


def test_sandbox_reports_exceptions(tmp_path):
    r = sandbox.run("1/0", tmp_path, "boom")
    assert not r.ok and "ZeroDivisionError" in r.error and r.result is None


# ---- workspace: persistence and kill-and-resume ------------------------------------------
def test_workspace_roundtrip_and_analysis_index(tmp_path):
    ws = Workspace.create("e1", root=tmp_path, problem_statement="Why are margins down?")
    tree = IssueNode(id="T", text="Profit", children=[IssueNode(id="T1", text="Revenue")])
    ws.set_tree(tree, [Hypothesis(id="H1", statement="Price fell", branch_id="T1")])
    rec = ws.run_analysis("bridge", "test H1", "RESULT = {'x': 2 * 21}", hypothesis_ids=["H1"])
    ws.update_hypothesis("H1", status="supported", evidence_fact_ids=[3, 1])
    again = Workspace.load("e1", root=tmp_path)
    assert again.state.analyses[0].result == {"x": 42} and rec.id == "A1"
    h = again.hypothesis("H1")
    assert (h.status, h.evidence_fact_ids, h.analysis_ids) == ("supported", [1, 3], ["A1"])
    assert [n.id for n in again.state.issue_tree.walk()] == ["T", "T1"]


RUNNER = r'''
import os, sys
from pathlib import Path
from consultant.workspace import Workspace
root, log = Path(sys.argv[1]), Path(sys.argv[2])
die_after = int(sys.argv[3])
ws = Workspace.open("eng", root=root, problem_statement="p")
for i in range(1, 5):
    def work(i=i):
        with log.open("a") as f:
            f.write(f"step{i}\n")
        return {"out": i}
    ws.step(f"step{i}", work)
    if i == die_after:
        os._exit(1)   # hard kill: no cleanup, no finally blocks
'''


def test_kill_and_resume_does_not_redo_completed_steps(tmp_path):
    log = tmp_path / "work.log"
    script = tmp_path / "runner.py"
    script.write_text(RUNNER)
    killed = subprocess.run([sys.executable, str(script), str(tmp_path), str(log), "2"])
    assert killed.returncode == 1
    assert log.read_text().split() == ["step1", "step2"]
    resumed = subprocess.run([sys.executable, str(script), str(tmp_path), str(log), "0"])
    assert resumed.returncode == 0
    assert log.read_text().split() == ["step1", "step2", "step3", "step4"]  # 1-2 not redone
    ws = Workspace.load("eng", root=tmp_path)
    assert ws.state.steps_done == ["step1", "step2", "step3", "step4"]
    assert ws.state.notes["step2"] == {"out": 2}


def test_sandbox_kill_leaves_no_orphan_process(tmp_path):
    import time

    import psutil
    r = sandbox.run("import time\nwhile True: time.sleep(0.1)", tmp_path, "sleeper", timeout=1)
    assert not r.ok
    time.sleep(0.5)

    def in_workdir(p):
        try:
            return Path(p.cwd()) == r.workdir
        except (psutil.Error, OSError):
            return False
    assert [p.pid for p in psutil.process_iter() if in_workdir(p)] == []
