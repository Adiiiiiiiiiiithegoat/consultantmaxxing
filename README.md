# consultantmaxxing

An AI strategy/management consultant. Give it a business problem; it scopes it, builds an
issue tree and ranked hypotheses, researches the open web with sourced facts, runs every
number in a sandbox, red-teams its own draft, and delivers:

- `memo.md`: answer first, 3–5 supporting arguments with evidence ids, risks, next steps
- `storyline.json` / `storyline.md`: slides with action titles that read as a story
- `model.xlsx`: inputs (every fact with its source, every assumption labelled) and a model
  sheet with live formulas
- `sources.md`: sources appendix with credibility labels and verification status
- `assumptions.md`: estimates, hypothesis status, what would change the answer
- `validation.json`: every number traced to a fact, analysis or assumption

Design and phases: [PLAN.md](PLAN.md). Build status: [PROGRESS.md](PROGRESS.md).

> **Status: build-only mode.** All components are built and covered by an offline test
> suite. The live evaluation gates (system vs baseline etc.) are deferred until a paid model
> key is available: see [DEFERRED_EVALS.md](DEFERRED_EVALS.md). Quality on free-tier
> open models is unmeasured.

## Setup

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), git. Optional:
[Ollama](https://ollama.com) for free local models.

```bash
git clone <this repo> consultantmaxxing
cd consultantmaxxing
uv sync
cp .env.example .env          # then edit .env
uv run pytest                 # offline suite, no API calls
```

### Keys (`.env`)
| Variable | Needed for |
|---|---|
| `GROQ_API_KEY` | the default model plan (Groq free tier works, slowly). Comma-separate several keys to rotate between them; limits are per Groq org, so only keys from different orgs add capacity |
| `OLLAMA_BASE_URL` | local models (default `http://localhost:11434`) |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` | if you point roles at those providers |
| `CONSULTANT_BLOCK_MODELS` | optional: comma-separated models the client must refuse |
| `CONSULTANT_MODELS_FILE` | optional: use another file in `config/` instead of `models.yaml` |

Local extractor model (optional, ~2.5GB; falls back to Groq if absent):
`ollama pull qwen3:4b-instruct`

## Run an engagement

```bash
uv run consult new "Why did Asian Paints' EBITDA margin change in FY24, and what should it do?"
uv run consult new "..." --profile agent-partner   # with a partner review of the draft
uv run consult new "..." --legacy                   # the old multi-agent pipeline
uv run consult list                             # engagements and their progress
uv run consult resume <engagement-id>           # continue after a stop
uv run consult ui                               # dashboard: the agent's work, facts, deliverables
```

One agent does the work (`consultant/agent.py`): each turn it researches the web (every
fact stored with its URL and quote), runs an analysis in the sandbox, or finishes. Its
answer is written up citing those facts, analyses and labelled estimates; a validator checks
every number and any it can't trace is removed and listed. With Claude
(`CONSULTANT_MODELS_FILE=models.claude.yaml`) the agent runs on Opus;
`models.claude-sonnet.yaml` is the cheaper Sonnet mode. The legacy pipeline (`--legacy`)
also has interactive checkpoints after scoping and after the hypotheses. Output goes to
`engagements/<id>/deliverables/`. Every step is saved, so a crash, Ctrl-C or exhausted
quota loses nothing: `consult resume` picks up where it stopped.

### Profiles (`config/budgets.yaml`)
| | dev | full |
|---|---|---|
| tokens per engagement | 60K | 400K |
| LLM calls / searches / pages | 120 / 20 / 30 | 800 / 120 / 200 |
| evidence loops / client questions / research questions per loop | 2 / 10 / 3 | 4 / 25 / 6 |

70% of the token budget goes to investigation, 30% is reserved so the run always delivers.
A run that hits a cap still delivers, with the unresolved points listed as open questions.

## Models: swapping providers

Code never names a model; it asks for a **role**. `config/models.yaml` maps roles
(engagement_manager, research_associate, analyst, storyliner, partner, extractor, judge,
interviewer, baseline) to `provider/model` plus parameters, and lists per-model rate limits.
All providers are reached through the OpenAI-compatible API, so switching is config only:

```yaml
providers:
  anthropic: {base_url: https://api.anthropic.com/v1, api_key_env: ANTHROPIC_API_KEY}
models:
  anthropic/claude-sonnet-5-5: {rpm: 50, tpm: 400000}
roles:
  engagement_manager:
    model: anthropic/claude-sonnet-5-5
    params: {temperature: 0.3, max_tokens: 4000}
```

Or keep several plans side by side and pick one per run:
`CONSULTANT_MODELS_FILE=models.small.yaml uv run consult new "..."` (every role on
gpt-oss-20b or local Ollama). Keep the partner and judge on a different model family from
the writer where possible.

The free-tier defaults (gpt-oss-120b, gpt-oss-20b, qwen3.8-27b on Groq, qwen3:4b-instruct
locally) and their quirks are documented in `config/models.yaml` and PROGRESS.md.

## Guardrails
- **Rate limits:** per-model RPM/TPM/daily limiter, disk cache, backoff on 429; a dead daily
  quota stops the run cleanly (resumable) instead of hammering the API.
- **Sourcing:** a fact cannot be stored without a URL and a verbatim quote; quotes and values
  are checked against the page; key numbers need two independent sources; citations are
  re-fetched and verified before delivery.
- **No maths in prose:** numbers come from sandboxed code (no network, timeout, memory cap);
  the deliverable validator flags any number not traceable to cited evidence.
- **Honesty:** confidence labels, explicit open questions, risks disclosed even without a
  mitigation, a red-team partner that flags answers that merely please the client.
- **Polite research:** robots.txt respected, per-host delays, identified user-agent; no
  bot-protection evasion.

## Private knowledge base (optional)

The case library and playbook mining used two copyrighted PDFs that are **not** in this repo.
If you have them, put them in `data/private/` and run `uv run consult knowledge build`
(LLM calls). Everything derived stays in gitignored `data/derived/` and `data/index/`.
Without them the system still runs in real mode; it just has no similar-case retrieval, and
case-mode evals need the cases. The playbooks in `skills/` are committed and original.

## Evaluation (deferred)
Runners exist for every gate: `consult eval system|baseline|facts|partner|backtests|
retrieval|tokens` and `consult report` (writes EVAL_REPORT.md). The order, commands and pass
criteria are in [DEFERRED_EVALS.md](DEFERRED_EVALS.md). Iterate on train cases only; the 15
held-out test cases are for the final run.

## Development
```bash
uv run pytest            # offline suite (mocked/recorded LLM responses)
uv run pytest -m live    # live smoke tests: real API calls, keep rare
uv run ruff check .
```
Layout: `consultant/llm` (provider layer, limiter, cache, structured output),
`consultant/research` (search, fetch, extraction, fact store, triangulation, verification),
`consultant/analysis` (sandbox, templates), `consultant/agents` (engagement manager,
research associate, analyst, storyliner, partner), `consultant/workspace`,
`consultant/deliverables`, `consultant/knowledge`, `consultant/evals`, `consultant/ui`,
`consultant/cli.py`, `skills/`, `config/`.

## Note on source material
Case and framework material used to build and evaluate the agent is copyrighted and is not
distributed with this repository.
