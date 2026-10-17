"""Phase 8 offline: the Streamlit dashboard renders finished and in-progress engagements;
the CLI wires commands and stops gracefully on quota exhaustion."""

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from typer.testing import CliRunner

import consultant.workspace as wsmod
from consultant.cli import app
from tests.test_engagement import ScriptedLLM, engagement

APP = str(Path(wsmod.__file__).parents[1] / "ui" / "app.py")


@pytest.fixture
def engagements(tmp_path, monkeypatch):
    monkeypatch.setattr(wsmod, "ENGAGEMENTS", tmp_path)
    return tmp_path


def text_of(at) -> str:
    parts = [m.value for m in at.markdown] + [t.value for t in at.title] + \
        [c.value for c in at.caption]
    return "\n".join(str(p) for p in parts)


def test_dashboard_renders_a_finished_engagement(engagements):
    eng, ws, _ = engagement(engagements, ScriptedLLM(), eid="done-one")
    eng.run()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    body = text_of(at)
    assert "Why have the bakery" in body and "H1" in body and "✅" in body
    assert len(at.metric) == 4 and at.metric[3].value == str(len(ws.facts.facts("done-one")))
    assert "## Answer" in body                                   # memo rendered inline
    assert len(at.get("download_button")) >= 6                   # memo, model, sources, ...


def test_dashboard_renders_a_run_in_progress(engagements):
    from consultant.workspace import Workspace
    ws = Workspace.create("live-one", root=engagements, mode="real",
                          problem_statement="Why did Acme's margins fall?")
    ws.state.steps_done = ["scope"]
    ws.state.scope = {"restated_question": "Why did Acme's margins fall in FY24?"}
    ws.save()
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception
    body = text_of(at)
    assert "Acme's margins" in body and "not built yet" in body
    assert at.metric[0].value == "1"


def test_dashboard_with_no_engagements(engagements):
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception and "Start an engagement" in text_of(at)


@pytest.mark.parametrize("engine", [[], ["--legacy"]])
def test_cli_list_and_graceful_quota_stop(engagements, monkeypatch, engine):
    from consultant import agent
    from consultant.agents import engagement_manager
    from consultant.llm import QuotaExhausted

    def boom(self):
        raise QuotaExhausted("daily quota exhausted; frees up in 40 min")
    monkeypatch.setattr(engagement_manager.Engagement, "run", boom)
    monkeypatch.setattr(agent.Agent, "run", boom)
    r = CliRunner().invoke(app, ["new", "Why did profits fall?", "--auto", "--name", "q1",
                                 *engine])
    assert r.exit_code == 2 and "Stopped cleanly" in r.output and "consult resume q1" in r.output
    r = CliRunner().invoke(app, ["list"])
    assert r.exit_code == 0 and "q1" in r.output
