"""Live smoke tests: one real call per configured provider. Run: uv run pytest -m live"""

import os

import httpx
import pytest
from pydantic import BaseModel

from consultant.config import models_config
from consultant.llm import LLM
from consultant.tracing import Tracer

pytestmark = pytest.mark.live
CFG = models_config()


def provider_ready(name: str) -> bool:
    p = CFG["providers"][name]
    if name == "ollama":
        try:
            return httpx.get(p["base_url"].removesuffix("/v1") + "/api/tags", timeout=3).is_success
        except httpx.HTTPError:
            return False
    return bool(os.environ.get(p["api_key_env"]))


def first_model(provider: str) -> str:
    return next(m for m in CFG["models"] if m.startswith(provider + "/"))


class Sum(BaseModel):
    answer: int


@pytest.mark.parametrize("provider", list(CFG["providers"]))
def test_one_call_per_provider(provider, tmp_path):
    if not provider_ready(provider):
        pytest.skip(f"{provider}: no key / not running")
    if not any(m.startswith(provider + "/") for m in CFG["models"]):
        pytest.skip(f"{provider}: no model configured")
    model = first_model(provider)
    params = {"max_tokens": 400, "temperature": 0}
    if "gpt-oss" in model:
        params["reasoning_effort"] = "low"
    cfg = {**CFG, "roles": {"smoke": {"model": model, "params": params}}}
    tracer = Tracer(root=tmp_path)
    llm = LLM(cfg, tracer=tracer, cache_dir=tmp_path / "cache")
    out = llm.structured("smoke", [{"role": "user", "content": "What is 17 + 25?"}], Sum)
    assert out.answer == 42
    assert tracer.usage()[model]["completion_tokens"] > 0
