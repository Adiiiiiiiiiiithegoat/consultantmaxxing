# consultantmaxxing — rules for Claude Code

An AI strategy/management consultant: takes a business problem, scopes it, builds an
issue tree, researches the real world with sourced facts, analyses in code, red-teams
itself, and delivers a memo, storyline, model spreadsheet and sources appendix.

Full design and phase gates: **PLAN.md**. Current status: **PROGRESS.md**. Read both
at the start of every session.

## Non-negotiable rules

1. **Copyright.** `data/private/` holds two all-rights-reserved PDFs (IIMB casebook,
   Bridgeway guide). This repo is PUBLIC. Nothing from them, verbatim or extracted,
   may be committed. Extracted cases, chunks, indexes and eval cases go in
   `data/derived/` or `data/index/` (gitignored). Playbooks in `skills/` ARE committed,
   so write them in original words: distil patterns, never copy sentences or cases.
   Before every commit, run `git status` and confirm no derived data is staged.
2. **Model-agnostic.** All LLM calls go through `consultant/llm/`. No provider SDK
   imports anywhere else. Models are chosen per role in `config/models.yaml`.
   Today the only keys are Groq free tier + local Ollama; Claude/OpenAI must be
   swappable by config alone.
3. **Respect rate limits.** Groq free tier is roughly 30 req/min, 8K tokens/min,
   200K tokens/day *per model* (check console.groq.com/settings/limits). Every call
   goes through the rate limiter and the disk cache. Never hammer an API in a loop.
4. **No maths in prose.** Every number the agent computes comes from the Python
   sandbox and is saved with the engagement.
5. **No unsourced claims.** Every factual claim in a deliverable traces to a fact-store
   row (with URL + quote) or an assumptions-register entry labelled as an estimate.
6. **Never tune on the test split.** Held-out cases in `data/derived/cases/test/` are
   only used by the eval runner. Retrieval indexes must exclude them (there is a
   leakage test; keep it passing).
7. **Never weaken a test or gate to make it pass.** If a gate is wrong, say so in
   PROGRESS.md and explain why before changing it.

## Build-only mode (in force since 2026-10-13, until a paid model key is available)

Groq free-tier limits made live evaluation take ~20h per eval, so the build continues
without live gates:
- **Do not run live-LLM gate items**: no baseline runs, fact-finding eval, judge/partner
  discrimination tests, full-system evals, or retrieval evals that need LLM calls. Build
  all of that tooling so it runs later with one command; list each check in
  **DEFERRED_EVALS.md** (in run order).
- **Offline tests are the gate**: unit tests for deterministic code and tests with
  mocked or recorded LLM responses. Keep `uv run pytest` green. Record a real response
  once as a fixture if a test needs one.
- **Live calls only as wiring smoke tests**: at most 1–2 small calls per phase, on the
  smallest suitable model or local Ollama. Never gpt-oss-120b (the main `.env` blocks it
  via `CONSULTANT_BLOCK_MODELS`).
- Each phase ends: offline tests green → PROGRESS.md (built, untested live, risks) →
  commit → next phase. Stop only for real blockers or design decisions.
- Don't start long background runs (the machine is memory-constrained and Claude Code
  kills background jobs under memory pressure).

The working loop below applies again once live evals resume.

## Working loop

Build phase by phase as PLAN.md defines. For each phase:
1. Implement.
2. Run that phase's gate (tests + checks listed in PLAN.md).
3. Gate passes → update PROGRESS.md (what was built, gate results, known issues),
   commit, and move to the next phase.
4. Gate fails → debug and retry, up to 3 attempts. Still failing → stop, write the
   blocker and what you tried in PROGRESS.md, and ask the user.

Keep PROGRESS.md current enough that a fresh session can resume from it.

## Conventions

- Python 3.11+, `uv` for env/deps, `pytest`, `ruff`, type hints, pydantic models for
  every structured LLM output (validate and retry on parse failure).
- Small, testable modules. Unit tests for deterministic code; for LLM behaviour, use
  recorded/cached responses so tests are cheap and repeatable.
- Tracing: every LLM call, tool call, token count and duration logged as JSONL under
  `traces/<run_id>/`.
- Windows host: use pathlib, no shell-specific assumptions.
