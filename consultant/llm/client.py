"""The only module that talks to model providers. Everything else asks for a *role*.

All providers are reached through the OpenAI-compatible chat API (Groq, Ollama, OpenAI,
Anthropic's compatibility endpoint), so swapping models is a config change.
Every call: context fit -> disk cache -> budget -> rate limiter -> retry/backoff -> trace.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TypeVar

import httpx
import openai
from pydantic import BaseModel, ValidationError

from consultant.config import ROOT, models_config
from consultant.llm.cache import DiskCache
from consultant.llm.context import estimate_tokens, fit
from consultant.llm.limiter import QuotaExhausted, RateLimiter, RequestTooLarge
from consultant.tracing import Budget, Tracer

T = TypeVar("T", bound=BaseModel)
_THINK = re.compile(r"<think>(.*?)</think>", re.S)


class LLMError(RuntimeError):
    pass


class StructuredOutputError(LLMError):
    pass


class ModelBlocked(LLMError):
    """Model listed in CONSULTANT_BLOCK_MODELS (e.g. reserved for a running eval)."""


@dataclass
class Completion:
    content: str
    model: str
    tool_calls: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    reasoning: str | None = None
    finish_reason: str | None = None
    cached: bool = False


def extract_json(text: str) -> str:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object found in reply")
    return text[start : end + 1]


def _retry_after(exc: openai.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


class LLM:
    def __init__(self, config: dict | None = None, *, tracer: Tracer | None = None,
                 budget: Budget | None = None, cache_dir: Path = ROOT / ".llm_cache",
                 http_client: httpx.Client | None = None, sleep=time.sleep,
                 max_retries: int = 5, clock=time.time):
        self.cfg = config or models_config()
        self.tracer, self.budget = tracer, budget
        self.cache = DiskCache(cache_dir / "responses")
        self.state_dir = cache_dir / "limits"
        self.http_client, self.sleep, self.clock = http_client, sleep, clock
        self.max_retries = max_retries
        self._limiters: dict[str, RateLimiter] = {}
        self._clients: dict[str, openai.OpenAI] = {}

    # ---- plumbing -------------------------------------------------------------------
    def _trace(self, kind: str, **data) -> None:
        if self.tracer:
            self.tracer.log(kind, **data)

    def limiter(self, model: str) -> RateLimiter:
        if model not in self._limiters:
            spec = self.cfg["models"].get(model, {})
            safe = re.sub(r"[^\w.-]", "_", model)
            self._limiters[model] = RateLimiter(
                *(spec.get(k) for k in ("rpm", "tpm", "rpd", "tpd", "otpm")),
                state_path=self.state_dir / f"{safe}.json", sleep=self.sleep, clock=self.clock)
        return self._limiters[model]

    def _client(self, provider: str) -> openai.OpenAI:
        if provider not in self._clients:
            p = self.cfg["providers"][provider]
            key = p.get("api_key") or os.environ.get(p.get("api_key_env", ""))
            if not key:
                raise LLMError(f"{p.get('api_key_env')} is not set (provider {provider})")
            self._clients[provider] = openai.OpenAI(
                base_url=p["base_url"], api_key=key, max_retries=0, timeout=180,
                http_client=self.http_client)
        return self._clients[provider]

    # ---- public API ------------------------------------------------------------------
    def complete(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                 cache: bool = True, json_mode: bool = False, **params) -> Completion:
        r = self.cfg["roles"][role]
        chain = [(r["model"], r.get("params", {}))]
        if fb := r.get("fallback"):
            chain.append((fb["model"], fb.get("params", {})))
        for i, (model, base) in enumerate(chain):
            try:
                return self._call(role, model, messages, tools, cache, json_mode,
                                  {**base, **params})
            except (QuotaExhausted, ModelBlocked, openai.APIConnectionError) as e:
                if i == len(chain) - 1:
                    raise
                self._trace("fallback", role=role, model=model, error=repr(e))
        raise AssertionError("unreachable")

    def structured(self, role: str, messages: list[dict], schema: type[T], *,
                   retries: int = 2, **params) -> T:
        """Pydantic schema -> JSON reply -> validate -> on failure show the error and retry."""
        hint = ("Reply with only a JSON object (no prose, no code fences) that validates "
                f"against this JSON schema:\n{json.dumps(schema.model_json_schema())}")
        msgs = [dict(m) for m in messages]
        if msgs and msgs[0]["role"] == "system":
            msgs[0]["content"] = f"{msgs[0]['content']}\n\n{hint}"
        else:
            msgs.insert(0, {"role": "system", "content": hint})
        for attempt in range(retries + 1):
            try:
                c = self.complete(role, msgs, json_mode=True, **params)
                return schema.model_validate_json(extract_json(c.content))
            except openai.BadRequestError as e:
                # Groq's JSON mode rejects invalid/empty output server-side with a 400
                if "json_validate_failed" not in str(e):
                    raise
                reply, err = "(invalid or empty JSON)", "the reply was not a valid JSON object"
            except (ValidationError, ValueError) as e:
                reply, err = c.content or "(empty reply)", str(e)[:1500]
            self._trace("schema_retry", role=role, attempt=attempt, error=err)
            msgs += [{"role": "assistant", "content": reply},
                     {"role": "user", "content": f"That reply failed validation:\n{err}\n"
                      "Return the corrected JSON object only, keeping it brief."}]
        raise StructuredOutputError(f"{role}: no valid {schema.__name__} after {retries + 1} tries")

    # ---- one model call ----------------------------------------------------------------
    def _call(self, role, model, messages, tools, use_cache, json_mode, params) -> Completion:
        if model in os.environ.get("CONSULTANT_BLOCK_MODELS", "").split(","):
            raise ModelBlocked(f"{model} is blocked by CONSULTANT_BLOCK_MODELS ({role})")
        provider, name = model.split("/", 1)
        spec = self.cfg["models"].get(model, {})
        if spec.get("max_output") and params.get("max_tokens", 0) > spec["max_output"]:
            params = {**params, "max_tokens": spec["max_output"]}
        max_out = params.get("max_tokens", 1024)
        ceiling = min(spec.get("tpm") or 10**9, spec.get("context") or 10**9)
        fixed = estimate_tokens(tools or []) + max_out
        if estimate_tokens(messages) + fixed > ceiling:
            before = estimate_tokens(messages)
            messages = fit(messages, ceiling - fixed)
            self._trace("context_trimmed", role=role, model=model, before=before,
                        after=estimate_tokens(messages))

        payload = {"model": model, "messages": messages, "tools": tools,
                   "json_mode": json_mode, "params": params}
        key = DiskCache.key(payload)
        if use_cache and (hit := self.cache.get(key)):
            self._trace("llm", role=role, model=model, cached=True, key=key)
            return Completion(**{**hit["completion"], "cached": True})

        est = estimate_tokens(messages) + fixed
        if self.budget:
            self.budget.check("calls")
            self.budget.check("tokens", est)
        limiter = self.limiter(model)
        ev = limiter.acquire(est, max_out)

        kw = {k: v for k, v in params.items() if v is not None}  # null = unset this param
        if tools:
            kw["tools"] = tools
        if json_mode and self.cfg["providers"][provider].get("json_mode", True):
            kw["response_format"] = {"type": "json_object"}
        t0 = time.time()
        resp = self._create(provider, name, model, messages, kw, limiter)
        secs = time.time() - t0

        choice = resp.choices[0]
        msg = choice.message
        content = msg.content or ""
        reasoning = getattr(msg, "reasoning", None) or (msg.model_extra or {}).get("reasoning")
        if think := _THINK.findall(content):
            reasoning, content = "\n".join(think), _THINK.sub("", content)
        usage = resp.usage.model_dump(include={"prompt_tokens", "completion_tokens",
                                               "total_tokens"}) if resp.usage else {}
        total = usage.get("total_tokens") or est
        limiter.settle(ev, total, usage.get("completion_tokens"))
        if self.budget:
            self.budget.charge("calls")
            self.budget.charge("tokens", total)

        out = Completion(content=content.strip(), model=model,
                         tool_calls=[tc.model_dump() for tc in msg.tool_calls or []],
                         usage=usage, reasoning=reasoning, finish_reason=choice.finish_reason)
        cost = (usage.get("prompt_tokens", 0) * spec.get("price_in", 0)
                + usage.get("completion_tokens", 0) * spec.get("price_out", 0)) / 1e6
        self._trace("llm", role=role, model=model, cached=False, key=key, seconds=round(secs, 3),
                    cost_usd=cost, finish_reason=choice.finish_reason, **usage)
        if use_cache:
            self.cache.set(key, {"request": payload, "completion": asdict(out)})
        return out

    def _create(self, provider, name, model, messages, kw, limiter):
        client = self._client(provider)
        for attempt in range(self.max_retries + 1):
            try:
                return client.chat.completions.create(model=name, messages=messages, **kw)
            except openai.RateLimitError as e:
                if "Request too large" in str(e):  # TPM or OTPM admission check: never clears
                    raise RequestTooLarge(f"{model}: {str(e)[:300]}") from e
                wait = _retry_after(e) or 2.0 ** attempt
                if wait > limiter.max_wait:
                    raise QuotaExhausted(f"{model}: server says retry in {wait:.0f}s") from e
                err = e
            except (openai.APITimeoutError, openai.InternalServerError) as e:
                wait, err = 2.0 ** attempt, e
            if attempt == self.max_retries:
                raise err
            self._trace("retry", model=model, attempt=attempt, wait=wait, error=repr(err)[:300])
            self.sleep(wait)
