# Progress

Updated by Claude Code at the end of every phase. A fresh session resumes from here.

| Phase | Status | Gate result | Notes |
|---|---|---|---|
| 0 Foundations | done | pass: 15 unit + 2 live (2 skipped: no keys) | see log 2026-10-13 |
| 1 Knowledge + eval foundation | not started | | |
| 2 Research layer | not started | | |
| 3 Analysis + engagement state | not started | | |
| 4 Playbooks + retrieval | not started | | |
| 5 Agents + engagement loop | not started | | |
| 6 Deliverables | not started | | |
| 7 Full evaluation | not started | | |
| 8 Interface + hardening | not started | | |

## Blockers

None.

## Log

### 2026-10-13 — Phase 0 Foundations (gate passed)

**Built**
- `uv` project (`pyproject.toml`), ruff, pytest. `uv run pytest` = unit tests;
  `uv run pytest -m live` = live smoke tests (real API calls, kept tiny).
- `consultant/config.py`: YAML config with `${VAR:-default}` expansion; loads `.env`.
- `consultant/llm/`: the only module importing a provider SDK (`openai` client against
  OpenAI-compatible endpoints: Groq, Ollama, OpenAI, Anthropic compat).
  - `client.LLM.complete(role, messages, tools=..., json_mode=...)`: context fit → disk
    cache → budget check → rate limiter → call with retry/backoff (429 uses
    `retry-after`, else exponential; 5xx/timeouts retried) → trace. Falls back to the
    role's `fallback` model on daily-quota exhaustion or unreachable server.
  - `client.LLM.structured(role, messages, Schema)`: pydantic schema in system prompt,
    JSON mode, validate, feed the validation error back, retry (default 2).
  - `limiter.py`: sliding-window RPM/TPM/RPD/TPD per model; day window persisted under
    `.llm_cache/limits/`; raises `QuotaExhausted` instead of sleeping for hours and
    `RequestTooLarge` if one request can never fit TPM.
  - `cache.py`: sha256 of (model, messages, tools, params) → JSON file holding request
    + completion under `.llm_cache/responses/`.
  - `context.py`: token estimate (chars/3, conservative), `clip`, `fit` (drop oldest
    middle turns, then clip longest). The client auto-fits when prompt + max_tokens
    exceeds the model's per-request ceiling (8K on Groq free) and traces it.
- `consultant/tracing/`: `Tracer` (JSONL `traces/<run_id>/events.jsonl`; llm, retry,
  fallback, schema_retry, context_trimmed events), `usage()` ledger per model,
  `Budget` (generic capped counters: tokens, calls, searches, pages) from
  `config/budgets.yaml` profiles `dev` / `full`.

**Model choice (config/models.yaml)**. Checked the live `/models` list and rate-limit
headers on this account on 2026-10-13. Only three usable chat LLMs on Groq: gpt-oss-120b,
gpt-oss-20b, qwen3.8-27b. Each has 30 RPM / 1K RPD / 8K TPM / 200K TPD, and quotas are
per model, so roles are spread across them:
- engagement_manager, storyliner → gpt-oss-120b (strongest reasoner; reasoning_effort medium)
- research_associate → gpt-oss-20b (own quota for parallel volume; fallback local)
- analyst, partner, judge → qwen3.8-27b (different family from the writer, which keeps
  the red-team and judge independent; good at code and tool calls)
- extractor → local Ollama `qwen3:4b-instruct` (free bulk extraction; fallback gpt-oss-20b)

**Gate results**
- Unit (15 passed): limiter on simulated clock (RPM, TPM + settle, daily exhaustion,
  oversized request, persistence across restart); cache hit/miss incl. params in key;
  schema retry success and give-up; forced 429 → slept retry-after 7s then 2s backoff;
  429 with hour-long retry-after → fallback without sleeping; unreachable provider →
  fallback; budget halts run (cached calls free); ledger; context trimming.
- Live (2 passed, 2 skipped): Groq + Ollama each returned a schema-valid answer.
  Anthropic/OpenAI skipped (no keys; adapters are config entries, untested by design).
- Forced 429 is tested against a mock OpenAI-compatible server, not live Groq, so we
  never hammer the real API (rule 3).

**Known issues / notes**
- Groq qwen3.8-27b emits no reasoning regardless of `reasoning_effort`; set to `none`.
- Plain `qwen3:4b` on Ollama always thinks (~500 tokens, ~10s per answer) even with
  think=false; use `qwen3:4b-instruct`. Plain `qwen3:4b` is still pulled locally (2.5GB);
  safe to `ollama rm qwen3:4b`.
- gpt-oss reasoning tokens count toward TPM and max_tokens. A tiny max_tokens can give
  empty content (reasoning ate it); keep role max_tokens generous.
- Windows console is cp1252: scripts printing model output need `PYTHONIOENCODING=utf-8`
  (the CLI in Phase 8 should reconfigure stdout).
- The limiter state is per process. Two processes running at once can exceed Groq's
  quota between them; the 429 backoff covers it.
- Couldn't open console.groq.com/settings/limits (Chrome extension unreachable). Limits
  come from the response headers (RPD, TPM) plus Groq's rate-limit docs (RPM, TPD).
