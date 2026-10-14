"""Claude Code CLI provider against recorded CLI outputs (tests/fixtures/claude_cli/): no live
calls. ok_*.jsonl and json_reply.jsonl are real `claude -p --output-format stream-json`
streams (sanitized; json_reply only swaps the reply text); usage_limit_SYNTHETIC.jsonl is
built in the same shape, since a real one can only be recorded by hitting the limit."""

import json
import threading
import time
from pathlib import Path

import pytest
from pydantic import BaseModel

from consultant.config import load_yaml
from consultant.llm import LLM, QuotaExhausted
from consultant.llm import claude_cli as cc
from consultant.tracing import Budget, Tracer

FIX = Path(__file__).parent / "fixtures" / "claude_cli"
CFG = {"providers": {"claude_cli": {"type": "claude_cli", "limit_wait_s": 1200,
                                    "max_limit_wait_h": 1}},
       "models": {"claude_cli/haiku": {"context": 1000000}},
       "roles": {"r": {"model": "claude_cli/haiku"}}}
MSG = [{"role": "system", "content": "Be terse."}, {"role": "user", "content": "hi"}]


def stream(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class Runner:
    """Replays recorded streams in order; records what each call was given."""

    def __init__(self, *names: str):
        self.queue, self.calls = list(names), []

    def __call__(self, cmd, prompt, env, cwd):
        system = Path(cmd[cmd.index("--system-prompt-file") + 1]).read_text(encoding="utf-8")
        self.calls.append({"cmd": cmd, "prompt": prompt, "env": env, "cwd": cwd,
                           "system": system})
        name = self.queue.pop(0)
        return (1, "", "claude crashed") if name == "CRASH" else (0, stream(name), "")


def make(tmp_path, runner, sleep=lambda s: None):
    return LLM(CFG, cache_dir=tmp_path / "cache", tracer=Tracer("t", root=tmp_path),
               cli_runner=runner, sleep=sleep)


def test_command_is_a_plain_model_call_on_the_subscription():
    cmd = cc.command("sonnet", "sys.txt", binary="claude")
    assert cmd[:2] == ["claude", "-p"] and cmd[cmd.index("--model") + 1] == "sonnet"
    for flag, value in [("--output-format", "stream-json"), ("--tools", ""),
                        ("--setting-sources", ""), ("--system-prompt-file", "sys.txt")]:
        assert cmd[cmd.index(flag) + 1] == value             # no tools, no settings/hooks
    for flag in ("--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
        assert flag in cmd                                   # no MCP, skills or sessions
    assert "--bare" not in cmd and "--append-system-prompt" not in cmd  # bare = API key only


def test_recorded_call_logs_tokens_cost_and_window_without_the_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-reach-the-cli")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "also-not")
    run = Runner("ok_haiku.jsonl")
    llm = make(tmp_path, run)
    budget = Budget({"tokens": 10_000, "calls": 5})
    with llm.scoped(budget):
        c = llm.complete("r", MSG)
    assert c.content == "OK" and c.model == "claude_cli/haiku"
    assert c.usage == {"prompt_tokens": 760, "completion_tokens": 4, "total_tokens": 764}
    assert budget.spent["tokens"] == 764 and budget.spent["calls"] == 1
    call = run.calls[0]
    assert "ANTHROPIC_API_KEY" not in call["env"] and "ANTHROPIC_AUTH_TOKEN" not in call["env"]
    assert call["system"] == "Be terse." and call["prompt"] == "hi"
    assert Path(call["cwd"]).name.startswith("consult-claude-")   # empty dir: no CLAUDE.md
    [ev] = llm.tracer.events("llm")
    assert ev["cost_usd"] == pytest.approx(0.0001538) and ev["windows"]["five_hour"] == 0.7
    assert ev["served_by"] == "claude-haiku-5-5" and ev["cache_write_tokens"] == 758
    assert llm.tracer.usage()["claude_cli/haiku"]["cost_usd"] == pytest.approx(0.0001538)
    assert llm.complete("r", MSG).cached and len(run.calls) == 1   # disk cache still applies


def test_structured_output_and_repair_go_through_the_cli(tmp_path):
    class Answer(BaseModel):
        city: str
        population_m: float
    run = Runner("ok_haiku.jsonl", "json_reply.jsonl")   # "OK" fails validation, then JSON
    a = make(tmp_path, run).structured("r", MSG, Answer)
    assert a.city == "Pune" and a.population_m == 7.4
    first, retry = run.calls
    assert "validates against this JSON schema" in first["system"]   # schema hint in system
    assert "[ASSISTANT]\nOK" in retry["prompt"] and "failed validation" in retry["prompt"]
    assert retry["prompt"].endswith("Reply to the last USER message only.")


def test_usage_limit_waits_every_20_minutes_then_resumes_or_stops_cleanly(tmp_path):
    slept = []
    run = Runner("usage_limit_SYNTHETIC.jsonl", "usage_limit_SYNTHETIC.jsonl", "ok_haiku.jsonl")
    llm = make(tmp_path, run, sleep=slept.append)
    assert llm.complete("r", MSG).content == "OK"
    assert slept == [1200, 1200]
    assert len(llm.tracer.events("usage_limit_wait")) == 2
    # past max_limit_wait (1 h here): stop cleanly with QuotaExhausted -> `consult resume`
    run = Runner(*["usage_limit_SYNTHETIC.jsonl"] * 5)
    slept.clear()
    with pytest.raises(QuotaExhausted):
        make(tmp_path / "b", run, sleep=slept.append).complete("r", MSG)
    assert slept == [1200, 1200, 1200]


def test_other_failures_are_retried_then_raised(tmp_path):
    from consultant.llm import LLMError
    run = Runner("CRASH", "ok_haiku.jsonl")
    assert make(tmp_path, run).complete("r", MSG).content == "OK"
    with pytest.raises(LLMError):
        make(tmp_path / "b", Runner(*["CRASH"] * 5)).complete("r", MSG, cache=False)


def test_concurrency_is_one(tmp_path):
    active, peak = [0], [0]

    def slow(cmd, prompt, env, cwd):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        time.sleep(0.05)
        active[0] -= 1
        return 0, stream("ok_haiku.jsonl"), ""
    llm = make(tmp_path, slow)
    threads = [threading.Thread(target=llm.complete, args=("r", [{"role": "user",
                                                                   "content": f"q{i}"}]))
               for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_claude_models_config_maps_roles_as_planned():
    roles = {r: v["model"] for r, v in load_yaml("models.claude.yaml")["roles"].items()}
    sonnet = ("engagement_manager", "research_associate", "analyst", "storyliner", "partner",
              "baseline")
    assert all(roles[r] == "claude_cli/sonnet" for r in sonnet)
    assert roles["judge"] == "claude_cli/opus"
    assert all(roles[r] == "claude_cli/haiku" for r in ("interviewer", "extractor"))
    profile = load_yaml("budgets.yaml")["profiles"]["claude"]
    assert profile["max_tokens"] > load_yaml("budgets.yaml")["profiles"]["dev"]["max_tokens"]
    assert json.dumps(load_yaml("models.claude.yaml")).count("api_key_env") == 0


def test_compare_blinds_both_answers_identically_and_judges_each_fresh(tmp_path, monkeypatch):
    import random

    from consultant.evals import compare as cmp
    monkeypatch.setattr(cmp, "ROOT", tmp_path)
    problem = tmp_path / "problem.md"
    problem.write_text("Should the bakery raise cake prices?", encoding="utf-8")
    human = tmp_path / "human.md"
    human.write_text("# My view\n\n**Raise prices 8%** and contract butter.\n\n* test first",
                     encoding="utf-8")
    system = ("# Should the bakery raise prices?\n\n## Answer\n**Recommendation:** Raise cake "
              "prices toward rivals [F4, A1].\n\n**Rests on:** F4, A1\n\n## Why\n### 1. Costs\n"
              "Costs rose (F3).  \n*Evidence: F3, A1*\n\n*Evidence ids: F = sourced fact.*"
              "\n\n## ⚠ Validation flags\n- slide 2: '25' not found")
    calls = []

    class Judge:
        def structured(self, role, msgs, schema, **kw):
            calls.append((role, msgs[-1]["content"], kw))
            return schema(**{k: {"justification": "j", "score": 4} for k in cmp.RUBRIC})
    res = cmp.compare(problem, human, "cmp1", llm=Judge(), system_answer=system,
                      rng=random.Random(1))
    out = tmp_path / "engagements" / "cmp1"
    mapping = json.loads((out / "key" / "mapping.json").read_text(encoding="utf-8"))
    assert sorted(mapping.values()) == ["human", "system"] and not (out / "mapping.json").exists()
    a, b = ((out / f"{x}.md").read_text(encoding="utf-8") for x in "AB")
    for text, lab in ((a, "A"), (b, "B")):
        assert text.startswith("CLIENT QUESTION\nShould the bakery") and f"\nANSWER {lab}\n" in text
        for tell in ("[F", "(F3)", "F4, A1", "Evidence", "#", "**", "Validation", "⚠", "* test"):
            assert tell not in text, (lab, tell)
    assert "Recommendation: Raise cake prices toward rivals." in (a + b)
    assert len(calls) == 2 and all(kw.get("cache") is False and role == "judge"
                                   for role, _, kw in calls)
    assert sum("ANSWER A" in c for _, c, _ in calls) == 1   # each call sees one answer only
    assert res[mapping["A"]]["label"] == "A" and res["system"]["total"] == 4


def test_a_failed_call_says_why():
    ok = stream("ok_haiku.jsonl")
    with pytest.raises(cc.CLIError, match=r"subtype=success is_error=False exit=1.*stderr='boom'"):
        cc.parse(ok, "boom", 1)          # a reply but a non-zero exit: report both
    bad = ok.replace('"is_error": false', '"is_error": true')
    with pytest.raises(cc.CLIError, match=r"is_error=True exit=0.*reply='OK'"):
        cc.parse(bad, "", 0)
    with pytest.raises(cc.CLIError, match="no result event"):
        cc.parse("", "", 0)
