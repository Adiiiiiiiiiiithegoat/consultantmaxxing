"""LLM client against a fake OpenAI-compatible server (httpx.MockTransport): no network."""

import json

import httpx
import pytest
from pydantic import BaseModel

from consultant.llm import LLM, StructuredOutputError
from consultant.llm.context import estimate_tokens, fit
from consultant.tracing import Budget, BudgetExceeded, Tracer
from tests.test_limiter import Clock

CFG = {
    "providers": {"fake": {"base_url": "http://fake/v1", "api_key": "x"},
                  "local": {"base_url": "http://fake/v1", "api_key": "x", "local": True},
                  "down": {"base_url": "http://down/v1", "api_key": "x"}},
    "models": {"fake/m": {"rpm": 100, "tpm": 8000, "rpd": 1000}, "fake/backup": {},
               "local/m": {},
               "down/m": {}},
    "roles": {
        "r": {"model": "fake/m", "params": {"max_tokens": 100}},
        "fb": {"model": "down/m", "params": {"max_tokens": 100},
               "fallback": {"model": "fake/backup", "params": {"temperature": 0}}},
        "fb2": {"model": "fake/m", "fallback": {"model": "fake/backup"}},
        "loc": {"model": "local/m"},
    },
}


def chat(content, prompt=10, completion=5):
    return httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "created": 0, "model": "m",
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "total_tokens": prompt + completion}})


class Server:
    """Replies from a queue; records request bodies."""

    def __init__(self, *replies):
        self.replies, self.requests = list(replies), []

    def __call__(self, request: httpx.Request):
        if request.url.host == "down":
            raise httpx.ConnectError("refused", request=request)
        self.requests.append(json.loads(request.content))
        return self.replies.pop(0)


@pytest.fixture
def make_llm(tmp_path):
    def _make(server, **kw):
        clock = Clock()
        llm = LLM(CFG, cache_dir=tmp_path / "cache", sleep=clock.sleep, clock=clock,
                  http_client=httpx.Client(transport=httpx.MockTransport(server)), **kw)
        return llm, clock
    return _make


MSG = [{"role": "user", "content": "hi"}]


def test_cache_hit_and_miss(make_llm):
    srv = Server(chat("one"), chat("two"), chat("three"))
    llm, _ = make_llm(srv)
    a = llm.complete("r", MSG)
    b = llm.complete("r", MSG)
    assert (a.content, a.cached, b.content, b.cached) == ("one", False, "one", True)
    assert len(srv.requests) == 1
    c = llm.complete("r", [{"role": "user", "content": "different"}])
    assert c.content == "two" and len(srv.requests) == 2
    d = llm.complete("r", MSG, temperature=0.9)  # params are part of the key
    assert (d.content, d.cached) == ("three", False)


class Answer(BaseModel):
    city: str
    population_m: float


def test_schema_retry_feeds_back_validation_error(make_llm):
    fenced = '```json\n{"city": "Pune", "population_m": 7.4}```'
    srv = Server(chat("sure! here you go"), chat(fenced))
    llm, _ = make_llm(srv)
    out = llm.structured("r", MSG, Answer)
    assert out == Answer(city="Pune", population_m=7.4)
    assert srv.requests[0]["response_format"] == {"type": "json_object"}
    assert "failed validation" in srv.requests[1]["messages"][-1]["content"]


def test_schema_retry_gives_up(make_llm):
    srv = Server(*[chat('{"city": "Pune"}') for _ in range(3)])
    llm, _ = make_llm(srv)
    with pytest.raises(StructuredOutputError):
        llm.structured("r", MSG, Answer, retries=2)
    assert len(srv.requests) == 3


def test_forced_429_backs_off_using_retry_after(make_llm, tmp_path):
    srv = Server(httpx.Response(429, headers={"retry-after": "7"}, json={"error": {}}),
                 httpx.Response(429, json={"error": {}}),
                 chat("ok"))
    tracer = Tracer(root=tmp_path / "traces")
    llm, clock = make_llm(srv, tracer=tracer)
    assert llm.complete("r", MSG).content == "ok"
    assert clock.slept == [7.0, 2.0]  # server's retry-after, then exponential backoff
    assert len(tracer.events("retry")) == 2


def test_429_with_long_retry_after_falls_back(make_llm):
    srv = Server(httpx.Response(429, headers={"retry-after": "3600"}, json={"error": {}}),
                 chat("from backup"))
    llm, clock = make_llm(srv)
    out = llm.complete("fb2", MSG)  # daily quota gone: don't sleep an hour, use fallback
    assert (out.model, out.content, clock.slept) == ("fake/backup", "from backup", [])


def test_unreachable_provider_falls_back(make_llm):
    srv = Server(chat("from backup"))
    llm, _ = make_llm(srv)
    out = llm.complete("fb", MSG)
    assert out.model == "fake/backup" and out.content == "from backup"
    assert srv.requests[0]["temperature"] == 0


def test_budget_halts_run(make_llm):
    srv = Server(chat("a"), chat("b"))
    llm, _ = make_llm(srv, budget=Budget({"calls": 1}))
    llm.complete("r", MSG)
    llm.complete("r", MSG)  # cached: free
    with pytest.raises(BudgetExceeded):
        llm.complete("r", [{"role": "user", "content": "new"}])


def test_tracer_ledger(make_llm, tmp_path):
    tracer = Tracer(root=tmp_path / "traces")
    llm, _ = make_llm(Server(chat("a", 100, 20)), tracer=tracer)
    llm.complete("r", MSG)
    llm.complete("r", MSG)
    u = tracer.usage()["fake/m"]
    assert (u["calls"], u["cached"], u["prompt_tokens"], u["completion_tokens"]) == (2, 1, 100, 20)


def test_oversized_context_is_trimmed_to_fit_tpm(make_llm):
    srv = Server(chat("ok"))
    llm, _ = make_llm(srv)
    big = [{"role": "system", "content": "be brief"}] + [
        {"role": "user", "content": "x" * 6000} for _ in range(10)]
    llm.complete("r", big)
    sent = srv.requests[0]["messages"]
    assert estimate_tokens(sent) + 100 <= 8000
    assert sent[0]["content"] == "be brief" and "omitted" in sent[1]["content"]


def test_fit_clips_single_huge_message():
    out = fit([{"role": "user", "content": "y" * 100_000}], 1000)
    assert estimate_tokens(out) <= 1000 and "omitted" in out[0]["content"]


def test_request_too_large_429_fails_fast(make_llm):
    from consultant.llm import RequestTooLarge
    err = {"error": {"message": "Request too large for model `m` on tokens per minute (TPM)"}}
    srv = Server(httpx.Response(429, json=err))
    llm, clock = make_llm(srv)
    with pytest.raises(RequestTooLarge):
        llm.complete("r", MSG)
    assert clock.slept == [] and len(srv.requests) == 1


def test_otpm_429_fails_fast_and_max_output_is_clamped(make_llm):
    # Groq estimates a request's output from recent replies; once that estimate exceeds the
    # OTPM cap, waiting never helps. Only a max_tokens <= cap is admitted.
    err = {"error": {"message": "Request too large for model `m` on output tokens per minute "
                                "(OTPM): Limit 1000, Requested 1371."}}
    from consultant.llm import RequestTooLarge
    llm, clock = make_llm(Server(httpx.Response(429, json=err)))
    with pytest.raises(RequestTooLarge):
        llm.complete("r", MSG)
    assert clock.slept == []
    srv = Server(chat("ok"))
    llm, _ = make_llm(srv)
    llm.cfg = {**CFG, "models": {**CFG["models"], "fake/m": {"max_output": 50}}}
    llm.complete("r", MSG, max_tokens=4000)
    assert srv.requests[0]["max_tokens"] == 50


def test_groq_json_validate_failed_400_is_retried(make_llm):
    err = {"error": {"message": "Failed to validate JSON.", "type": "invalid_request_error",
                     "code": "json_validate_failed", "failed_generation": ""}}
    srv = Server(httpx.Response(400, json=err), chat('{"city": "Pune", "population_m": 7.4}'))
    llm, _ = make_llm(srv)
    assert llm.structured("r", MSG, Answer).city == "Pune"
    assert "failed validation" in srv.requests[1]["messages"][-1]["content"]


def test_blocked_model_is_refused_or_falls_back(make_llm, monkeypatch):
    from consultant.llm import ModelBlocked
    monkeypatch.setenv("CONSULTANT_BLOCK_MODELS", "fake/m")
    srv = Server(chat("from backup"))
    llm, _ = make_llm(srv)
    with pytest.raises(ModelBlocked):
        llm.complete("r", MSG)          # no fallback: hard failure, nothing sent
    assert srv.requests == []
    assert llm.complete("fb2", MSG).model == "fake/backup"


def test_scoped_budget_caps_one_thread_of_work(make_llm):
    srv = Server(chat("a"), chat("b"))
    llm, _ = make_llm(srv)
    with llm.scoped(Budget({"calls": 1})):
        llm.complete("r", MSG)
        with pytest.raises(BudgetExceeded):
            llm.complete("r", [{"role": "user", "content": "second"}])
    llm.complete("r", [{"role": "user", "content": "second"}])  # outside the scope: fine


def test_local_provider_not_charged_to_budget(make_llm):
    srv = Server(chat("a"), chat("b"))
    llm, _ = make_llm(srv, budget=Budget({"calls": 0, "tokens": 0}))
    llm.complete("loc", MSG)                       # free local model: allowed
    with pytest.raises(BudgetExceeded):
        llm.complete("r", MSG)                     # remote: capped


def test_disk_cache_concurrent_writes_to_same_key(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from consultant.llm.cache import DiskCache
    c = DiskCache(tmp_path)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda i: c.set("k" * 64, {"v": 1}), range(64)))
    assert c.get("k" * 64) == {"v": 1}
    assert not list(tmp_path.rglob("*.tmp"))
