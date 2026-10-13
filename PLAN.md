# consultantmaxxing — build plan

## 1. What "finished" means

Give it a business problem, e.g. *"A mid-size Indian paint company's EBITDA margin fell
4 points in two years. What should they do?"* It will:

1. Ask scoping questions (or accept a brief in non-interactive mode)
2. Classify the problem (profitability, market entry, pricing, growth, M&A/PE,
   unconventional, guesstimate) and load the matching playbook
3. Build a MECE issue tree and ranked hypotheses
4. Research the real world in parallel, storing every fact with its source
5. Run all quantitative analysis in a Python sandbox
6. Update hypotheses (support / kill / dig deeper) until resolved or out of budget
7. Have an independent "partner" red-team the storyline, then revise
8. Deliver: executive memo, slide storyline, model spreadsheet (.xlsx), sources
   appendix, assumptions & confidence page

Two operating modes:
- **Case mode:** an interviewer simulator holds the case data and reveals it only when
  asked a relevant question (used for evaluation).
- **Real mode:** no interviewer; the agent must research everything itself.

## 2. Architecture

```
            user / CLI / UI
                  │
        ┌─────────▼──────────┐
        │ Engagement Manager │  orchestrator: scope, issue tree, hypotheses,
        └─┬──────┬──────┬────┘  dispatch, stop rules
          │      │      │
 ┌────────▼─┐ ┌──▼────┐ ┌▼────────────┐
 │ Research │ │Analyst│ │ Storyliner  │
 │Associates│ │sandbox│ └──────┬──────┘
 │(parallel)│ └───────┘        │
 └────┬─────┘           ┌──────▼──────┐
      │                 │   Partner   │  independent red-team, fresh context
      ▼                 └─────────────┘
 Fact store (SQLite) · Engagement workspace · Retrieval (cases, industry primers)
```

| Role | Job | Default model (Groq free tier) |
|---|---|---|
| Engagement Manager | Scoping, tree, hypotheses, routing, stopping | strongest available (e.g. `openai/gpt-oss-120b`) |
| Research Associate | One research question → sourced facts | mid model; parallel, rate-limited |
| Analyst | Writes and runs analysis code | strongest / code-capable |
| Storyliner | Pyramid-principle memo + storyline | strongest |
| Partner | Red-team review | a *different* model from the writer where possible |
| Extractor | Parse docs/pages into structured facts | small/fast model or local Ollama |
| Judge (eval only) | Score runs against rubric | strongest, different from the system's writer if possible |

Model names live in `config/models.yaml`, never in code. Check Groq's current model
list before hard-coding defaults.

## 3. Constraints and the design decisions they force

**Only Groq free tier + local Ollama for now.** Consequences:
- No Claude Agent SDK (Claude-only). Write a small, transparent agent loop of our own
  on an OpenAI-compatible client (Groq, Ollama, OpenAI and Anthropic's
  OpenAI-compatible endpoint all fit), or use LiteLLM as the provider layer.
  Tool calling via JSON-schema function calls.
- **Rate limits are the main engineering problem.** ~8K tokens/min and ~200K
  tokens/day per model. Therefore:
  - token-bucket rate limiter per model (RPM + TPM + daily budget), with backoff on 429
  - spread roles across different models (each has its own quota)
  - disk cache keyed on (model, messages, params) so reruns during debugging are free
  - aggressive context management: agents get summaries + IDs, not raw pages; the
    fact store holds the detail
  - route bulk extraction to local Ollama where quality allows
  - a `dev` profile (fewer cases, smaller budgets) and a `full` profile
  - when the daily budget is near exhaustion, checkpoint and stop cleanly; runs are
    resumable
- **Quality ceiling.** Open models are weaker orchestrators than frontier models.
  The eval harness measures this; switching to Claude later is a config change, and
  the same evals show the gain.

**Search without paid keys.** Default `ddgs` (DuckDuckGo, keyless) behind a
`SearchProvider` interface; Tavily/Brave/Claude web search pluggable later. Fetch with
`httpx` + `trafilatura` (HTML) and `pymupdf` (PDF). Respect robots.txt, cache pages,
polite delays.

**Embeddings:** local (`sentence-transformers` bge-small or Ollama `nomic-embed-text`).
**Vector store:** LanceDB (local, file-based). **State:** SQLite.

## 4. Repo layout (target)

```
consultant/
  llm/            provider layer, rate limiter, cache, structured output + retry
  agents/         engagement_manager, research_associate, analyst, storyliner, partner
  research/       search providers, fetcher, extractor, fact_store, credibility,
                  triangulation, citation_verifier
  analysis/       sandbox runner, templates (sizing, break-even, unit econ,
                  price-volume-mix, margin bridge, scenarios, NPV/IRR)
  knowledge/      pdf extraction, case extraction, industry chunks, retrieval
  workspace/      engagement state: tree, hypotheses, assumptions, decisions
  deliverables/   memo, storyline, xlsx model, sources appendix, assumptions page
  evals/          interviewer simulator, judge, rubric, fact-finding set,
                  backtests, baseline, report
  tracing/        JSONL tracer, cost/usage accounting
  cli.py
  ui/             Streamlit app
skills/           playbooks in original words (committed)
config/           models.yaml, budgets.yaml, profiles (dev/full)
data/private/     source PDFs (gitignored)
data/derived/     extracted cases, chunks, eval sets (gitignored)
tests/
```

## 5. Phases and gates

Each phase ends with a gate. Proceed only when it passes (see CLAUDE.md working loop).

### Phase 0 — Foundations
- [x] Project scaffold (`uv`, ruff, pytest), config loading, `.env`
- [x] Provider layer: Groq + Ollama working; Anthropic/OpenAI adapters present but untested
- [x] Rate limiter (RPM/TPM/daily per model), retry/backoff, disk cache
- [x] Structured output helper: pydantic schema → JSON → validate → repair/retry
- [x] Tracer (JSONL per run) and usage/cost ledger; budget object that can halt a run

**Gate:** unit tests for limiter (simulated clock), cache hit/miss, schema
retry; live smoke test makes one call to each configured provider; a forced 429 is
handled with backoff.

### Phase 1 — Knowledge extraction and evaluation foundation
- [x] Extract text from the casebook; segment into individual cases and industry reports
- [x] LLM-extract each case into a pydantic `Case`: id, title, firm, type, difficulty,
  numerical flag, industry, problem statement, clarifying Q&A, structure used,
  data reveals (each: trigger question → data given), key insight, recommendation,
  interviewer feedback
- [x] Industry reports → tagged chunks (value chain, KPIs, cost/revenue drivers, trends)
- [x] Bridgeway → notes for the deliverables/research playbooks (original words only)
- [x] Stratified split with fixed seed: ~15 held-out test cases across types/difficulty;
  rest are train (case library + playbook mining)
- [x] **Interviewer simulator:** holds one case; answers clarifying questions; reveals a
  data item only when a question is relevant to its trigger; otherwise "not
  available / what would you need it for?"
- [x] **Judge** with rubric (each 1–5, with written justification): scoping,
  structure (MECE), hypothesis quality, key insight found, maths correctness,
  evidence/sourcing, recommendation follows from evidence, communication
  (answer-first, pyramid), efficiency (questions/searches used)
- [x] **Baseline runner:** plain single-model consultant prompt, no tools/playbooks,
  through the same simulator and judge

**Gate:** ≥55 cases extracted, 100% schema-valid; a written spot-check of 10 random
cases against source text with no material errors; simulator test: scripted
relevant questions get the data, irrelevant ones don't; judge discrimination test:
a strong reference answer scores clearly higher than a deliberately flawed one on
≥4 of 5 cases; baseline scores recorded in `eval_results/baseline.json`.

### Phase 2 — Research layer
- [ ] `SearchProvider` (ddgs default), fetcher with caching, robots.txt, PDF support
- [ ] Source-type classification and credibility ranking (filings/regulators/government
  > established research & business press > trade press > blogs/forums)
- [ ] **Fact store** (SQLite): claim, value, unit, entity, period, source_url, source_title,
  source_date, quote, source_type, credibility, retrieved_at, hypothesis_ids,
  engagement_id
- [ ] Triangulation: key numbers need ≥2 independent sources; conflicts stored as ranges
- [ ] **Citation verifier:** re-fetch the source and check the quote supports the claim
- [ ] Estimation fallback: top-down / bottom-up guesstimate with an assumptions register
- [ ] **Research Associate** agent: question → search plan → read → extract facts →
  gap check → brief with confidence; hard caps on searches/pages/tokens

**Gate:** fact-finding eval of 20 questions with stable, verifiable answers (e.g.
reported annual figures of listed Indian companies, official statistics): ≥70%
correct within tolerance; citation verifier confirms ≥90% of stored facts; zero
deliverable-eligible facts without a source; budget caps respected in tests.

### Phase 3 — Analysis and engagement state
- [ ] Sandbox: subprocess with timeout, memory cap, no network, working dir per
  engagement; all code and outputs saved
- [ ] Analysis templates with unit tests: market sizing (top-down/bottom-up),
  break-even, unit economics, price-volume-mix, margin bridge, scenario/sensitivity
  tables, NPV/IRR/payback
- [ ] Engagement workspace: problem statement, issue tree, hypothesis log
  (open/supported/killed + evidence ids), assumptions register, decision log,
  analysis index. Serialised to disk after every step.

**Gate:** all templates pass known-answer tests; sandbox blocks network and times
out runaway code; kill-and-resume test: a run interrupted mid-way resumes from the
workspace without redoing completed work.

### Phase 4 — Playbooks and retrieval
- [ ] `skills/` playbooks, one per case type: what to clarify first, structures and when
  they don't fit, common root causes ranked by frequency in the train cases, traps
  (mined from interviewer feedback), definition of done
- [ ] Cross-cutting skills: hypothesis-driven problem solving, research method,
  deliverables (answer-first, pyramid principle, one message per slide,
  storyboarding), strategy concepts (moats, positioning, implementation)
- [ ] Retrieval: case library indexed by *problem pattern* (train split only) + industry
  primers; primers auto-extend when the agent researches a new industry

**Gate:** leakage test passes (no test-case content in any index); retrieval eval: for
20 hand-written queries the relevant case/primer is in top-5 for ≥80%; playbook
originality check (no long verbatim overlaps with source text, e.g. no shared
12-word n-grams).

### Phase 5 — Agents and the engagement loop
- [ ] Engagement Manager loop: scope → classify → load playbook → issue tree +
  hypotheses → dispatch research in parallel → analyst → update hypotheses → repeat
  → storyliner → partner → revise (max 3 rounds) → deliver
- [ ] Stop rules: all priority hypotheses resolved, or budget/iteration cap hit (then
  deliver with explicit open questions)
- [ ] User checkpoints after scoping and after initial hypotheses (interactive mode);
  auto mode for evals
- [ ] Partner: fresh context, sees only draft + fact store; checks sourcing, logic,
  strongest counterargument, CEO questions, and an anti-agreeableness check (if the
  answer just confirms the client's initial belief, it must be justified)

- [ ] After the first end-to-end runs: measure tokens per full engagement (per model),
  estimate Phase 7 duration on the free tier, propose options (spread roles across models,
  a 5-case dev eval set for iteration, the full 15 only for the final run); note in PROGRESS.md

**Gate:** 3 train cases run end-to-end in case mode within budget and produce all
artefacts; partner catches the planted flaw in ≥4 of 5 seeded bad drafts
(unsupported claim, maths error, non-sequitur recommendation, missing obvious
branch, client-pleasing conclusion); one real-mode run completes on a simple
public-company question.

### Phase 6 — Deliverables
- [ ] Executive memo (markdown → also .docx/.pdf optional): answer first, 3–5 supporting
  arguments, risks, next steps
- [ ] Slide storyline: action titles that read as a story alone + supporting data per
  slide (JSON + markdown; .pptx rendering optional)
- [ ] Model spreadsheet via openpyxl with live formulas and an inputs/assumptions tab
- [ ] Sources appendix generated from the fact store with credibility labels
- [ ] Assumptions & confidence page: estimates, uncertainty, what data would change the
  answer

**Gate:** validator confirms every number in memo/storyline maps to a fact id,
analysis output or assumption id; .xlsx opens and formulas recalculate to the
sandbox values; every slide has an action title.

### Phase 7 — Full evaluation and debugging
- [ ] Run all held-out cases through the full system and the baseline (case mode)
- [ ] Research backtests: 3 real-mode problems using lesser-known companies or events
  after the model's knowledge cutoff, or restricting sources to dates before the
  outcome
- [ ] `EVAL_REPORT.md`: per-dimension scores, system vs baseline, cost/tokens per run,
  and failure analysis classified by layer (playbook gap / research / analysis /
  synthesis / orchestration)

**Gate:** system beats baseline on aggregate judge score and on at least 6 of 9
rubric dimensions; report written. If not, iterate on the weakest layer (max 3
iterations), re-running the eval each time, before escalating to the user.

### Phase 8 — Interface and hardening
- [ ] CLI (`typer`): `consult new`, `consult resume`, `consult eval`, `consult report`
- [ ] Streamlit UI: live issue tree, hypothesis status, facts as they arrive, budget
  usage, deliverable downloads
- [ ] Guardrails: budgets, no-network sandbox, citation requirement enforced at
  deliverable time, confidence labels, graceful stop on quota exhaustion
- [ ] README: setup, keys, profiles, how to run, how to swap models

**Gate:** fresh clone → setup → `consult new` completes a dev-profile engagement
following only the README; UI renders a live run; full test suite green.

## 6. Later (not in this build)
- Switch roles to Claude via config and re-run evals to measure the gain
- Use the consultant as a simulated expert persona for product testing
- Additional knowledge sources and more case types
