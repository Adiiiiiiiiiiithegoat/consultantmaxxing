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

> **Build-only mode (since 2026-10-13):** live-LLM gate items are deferred until a paid key
> (see CLAUDE.md and DEFERRED_EVALS.md). Phases advance on offline tests (deterministic code
> + mocked/recorded LLM responses); live gate tooling is built but not run.

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
- [x] `SearchProvider` (ddgs default), fetcher with caching, robots.txt, PDF support
- [x] Source-type classification and credibility ranking (filings/regulators/government
  > established research & business press > trade press > blogs/forums)
- [x] **Fact store** (SQLite): claim, value, unit, entity, period, source_url, source_title,
  source_date, quote, source_type, credibility, retrieved_at, hypothesis_ids,
  engagement_id
- [x] Triangulation: key numbers need ≥2 independent sources; conflicts stored as ranges
- [x] **Citation verifier:** re-fetch the source and check the quote supports the claim
- [x] Estimation fallback: top-down / bottom-up guesstimate with an assumptions register
- [x] **Research Associate** agent: question → search plan → read → extract facts →
  gap check → brief with confidence; hard caps on searches/pages/tokens

**Gate:** fact-finding eval of 20 questions with stable, verifiable answers (e.g.
reported annual figures of listed Indian companies, official statistics): ≥70%
correct within tolerance; citation verifier confirms ≥90% of stored facts; zero
deliverable-eligible facts without a source; budget caps respected in tests.

### Phase 3 — Analysis and engagement state
- [x] Sandbox: subprocess with timeout, memory cap, no network, working dir per
  engagement; all code and outputs saved
- [x] Analysis templates with unit tests: market sizing (top-down/bottom-up),
  break-even, unit economics, price-volume-mix, margin bridge, scenario/sensitivity
  tables, NPV/IRR/payback
- [x] Engagement workspace: problem statement, issue tree, hypothesis log
  (open/supported/killed + evidence ids), assumptions register, decision log,
  analysis index. Serialised to disk after every step.

**Gate:** all templates pass known-answer tests; sandbox blocks network and times
out runaway code; kill-and-resume test: a run interrupted mid-way resumes from the
workspace without redoing completed work.

### Phase 4 — Playbooks and retrieval
- [x] `skills/` playbooks, one per case type: what to clarify first, structures and when
  they don't fit, common root causes ranked by frequency in the train cases, traps
  (mined from interviewer feedback), definition of done
- [x] Cross-cutting skills: hypothesis-driven problem solving, research method,
  deliverables (answer-first, pyramid principle, one message per slide,
  storyboarding), strategy concepts (moats, positioning, implementation)
- [x] Retrieval: case library indexed by *problem pattern* (train split only) + industry
  primers; primers auto-extend when the agent researches a new industry

**Gate:** leakage test passes (no test-case content in any index); retrieval eval: for
20 hand-written queries the relevant case/primer is in top-5 for ≥80%; playbook
originality check (no long verbatim overlaps with source text, e.g. no shared
12-word n-grams).

### Phase 5 — Agents and the engagement loop
- [x] Engagement Manager loop: scope → classify → load playbook → issue tree +
  hypotheses → dispatch research in parallel → analyst → update hypotheses → repeat
  → storyliner → partner → revise (max 3 rounds) → deliver
- [x] Stop rules: all priority hypotheses resolved, or budget/iteration cap hit (then
  deliver with explicit open questions)
- [x] User checkpoints after scoping and after initial hypotheses (interactive mode);
  auto mode for evals
- [x] Partner: fresh context, sees only draft + fact store; checks sourcing, logic,
  strongest counterargument, CEO questions, and an anti-agreeableness check (if the
  answer just confirms the client's initial belief, it must be justified)

- [x] After the first end-to-end runs: measure tokens per full engagement (per model),
  estimate Phase 7 duration on the free tier, propose options (spread roles across models,
  a 5-case dev eval set for iteration, the full 15 only for the final run); note in PROGRESS.md

**Gate:** 3 train cases run end-to-end in case mode within budget and produce all
artefacts; partner catches the planted flaw in ≥4 of 5 seeded bad drafts
(unsupported claim, maths error, non-sequitur recommendation, missing obvious
branch, client-pleasing conclusion); one real-mode run completes on a simple
public-company question.

### Phase 6 — Deliverables
- [x] Executive memo (markdown → also .docx/.pdf optional): answer first, 3–5 supporting
  arguments, risks, next steps
- [x] Slide storyline: action titles that read as a story alone + supporting data per
  slide (JSON + markdown; .pptx rendering optional)
- [x] Model spreadsheet via openpyxl with live formulas and an inputs/assumptions tab
- [x] Sources appendix generated from the fact store with credibility labels
- [x] Assumptions & confidence page: estimates, uncertainty, what data would change the
  answer

**Gate:** validator confirms every number in memo/storyline maps to a fact id,
analysis output or assumption id; .xlsx opens and formulas recalculate to the
sandbox values; every slide has an action title.

### Phase 7 — Full evaluation and debugging
- [x] Runner: held-out cases through the full system and the baseline (case mode)
- [x] Research backtests tooling: real-mode problems answered as of a past date (sources
  restricted to on/before it), scored against the outcome
- [x] `EVAL_REPORT.md` generator: per-dimension scores, system vs baseline, tokens per run,
  failure analysis by layer, gate verdict
- [ ] (deferred, paid key) Verify the 3 backtest outcomes, then run backtests
- [ ] (deferred, paid key) Iterate on a 5-case train subset; final run on the 15 test cases
  + baseline; write EVAL_REPORT.md

**Gate:** system beats baseline on aggregate judge score and on at least 6 of 9
rubric dimensions; report written. If not, iterate on the weakest layer (max 3
iterations), re-running the eval each time, before escalating to the user.

### Phase 8 — Interface and hardening
- [x] CLI (`typer`): `consult new`, `consult resume`, `consult eval`, `consult report`
- [x] Streamlit UI: live issue tree, hypothesis status, facts as they arrive, budget
  usage, deliverable downloads
- [x] Guardrails: budgets, no-network sandbox, citation requirement enforced at
  deliverable time, confidence labels, graceful stop on quota exhaustion
- [x] README: setup, keys, profiles, how to run, how to swap models

**Gate:** fresh clone → setup → `consult new` completes a dev-profile engagement
following only the README; UI renders a live run; full test suite green.

### Live gate runs on the free tier (overnight 2026-10-14, branch overnight-fixes)
- [x] Resume smoke-asianpaints-2 through to complete deliverables
- [x] `consult eval system --split train --n 3`: 3/3 within budget, all artefacts
- [x] `consult eval facts`: accuracy 75%, verifier 0.99, unsourced 0
- [x] `consult eval partner`: 5/5 seeded flaws caught
- [x] Partner fixture made arithmetically consistent, with a consistency test
- [x] Targeted repair pass: no flagged number ships as fact (fix, else `[unverified figure]`)
- [x] Judge: process dimensions on transcript + workspace, outcome dimensions on the answer only
- [x] Baseline on the same 3 train cases, same model; system vs baseline per dimension
- [x] Fact-finding gate split 10 dev / 10 held out; held-out accuracy is the headline
- [x] Storyliner always commits: recommendation, confidence + reason, 2-3 key evidence, what would change it, next steps
- [x] Partner findings split into blocking vs comments; approve with comments
- [x] Validator checks the cited fact's period matches the claim's period
- [x] Efficiency measured from the transcript (unanswered / no-new-info vs reference)
- [x] Propose 10 fresh fact questions with sources (`fact_questions_fresh.yaml`)
- [x] User spot-checks the 10 fresh fact questions (all 10 verified)
- [x] Simulator calibrated on the casebook's real interviews (fishing filter, new prompt, model + lexical pick)
- [x] Claude via the Pro subscription: claude_cli provider, models.claude.yaml, claude budget profile
- [x] `consult compare <problem.md> <human_answer.md>`: blinded A/B, judged separately
- [x] Live: smoke call per model; one case-mode engagement measured (5m45s, ~140K tokens, ~4% of a 5-hour window)
- [x] Re-run on Claude: 6 train cases (original 3 + p031/p033/p039), system and baseline, partner eval (command in DEFERRED_EVALS.md)
- [x] Diagnose the 6-case result: context loss, format bias, over-asking; partner fixture cost lines
- [x] Judge fix: whole answer to the outcome call, final answer to the process call; re-judge stored runs
- [x] Full client replies to every agent (no truncation, merged duplicates, problem statement in the brief)
- [x] Case interview as a conversation: answer prompts, client questions only, no repeats, stop after 2 stale replies
- [x] Review loop keeps the previous draft's answer; ship the best-reviewed draft
- [x] One reconciled decision model before synthesis; older analyses superseded
- [x] Calibrated confidence, rounded numbers, one open-questions list, no ids in client text
- [x] History rewrite for a copyright leak; originality pre-commit hook (8-word runs, data/ and PDF paths)
- [x] Re-run on Claude: system on 12 train cases, baseline on the new 6; report studied vs untouched (system 3.06 vs baseline 3.34)
- [x] Ablation variants behind config flags (branch `ablations`): A Opus brain, B single-brain synthesis, C baseline+
- [x] Side-by-side report: `report --compare` (studied 6 / untouched 6 / all 12)
- [x] Run A, B and C on the same 12 train cases and compare with system-v2 and baseline (C 3.51, baseline 3.34, A 3.23, B 2.98)
- [x] Confirm C on 6 new train cases: C 3.28, baseline 3.15, C with Opus 3.52; pooled 18 C 3.43 vs baseline 3.28
- [ ] Wire the 10 verified fresh fact questions in as the held-out fact-finding set

## 5b. Rebuild around the single agent (C)
Start only if the confirmation holds. One tool-using agent replaces the multi-agent
pipeline: C's loop (playbooks in the prompt, sandbox) plus research, citations and
deliverables. The eval harness (simulator, judge, report, baseline, leakage and
originality tests) stays as it is; `consult eval system` runs the new agent.
- [ ] Pick the agent model (Sonnet or Opus) from the confirmation run
- [ ] `consultant/agent.py`: one loop from baseline+ with ask / analyse / research / finish actions, budget and stop rules from budgets.yaml
- [ ] Research action: the research associate + extractor behind one call; facts go to the fact store with URL + quote and come back with ids
- [ ] Citations: the answer cites fact ids and assumption entries; the Phase 6 validator checks every claim and number
- [ ] Deliverables: memo, storyline, model .xlsx and sources appendix built from the answer, sandbox results and fact store
- [ ] Partner review behind a profile flag (off by default): one review, one revision, ship the better-reviewed draft
- [ ] Real mode: same loop without the interviewer (ask off, research on)
- [ ] `consult new`, `consult ui` and `consult eval system` run the new agent; old pipeline kept as `--legacy` until the new one beats it, then deleted
- [ ] Offline tests with recorded responses for the loop, each action and the review flag
- [ ] Deferred eval: new agent vs C vs baseline on the 18 train cases, partner flag on and off; then the test split once

## 6. Later (not in this build)
- Switch roles to Claude via config and re-run evals to measure the gain
- Use the consultant as a simulated expert persona for product testing
- Additional knowledge sources and more case types
