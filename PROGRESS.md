# Progress

**Mode: build-only since 2026-10-13** (see CLAUDE.md). Live gate checks are listed in
DEFERRED_EVALS.md; phases advance on offline tests.

Updated by Claude Code at the end of every phase. A fresh session resumes from here.

| Phase | Status | Gate result | Notes |
|---|---|---|---|
| 0 Foundations | done | pass: 15 unit + 2 live (2 skipped: no keys) | see log 2026-10-13 |
| 1 Knowledge + eval foundation | built; baseline eval deferred | offline green; live: extraction, spot-check, simulator, judge passed; baseline 0/15 (deferred) | DEFERRED_EVALS.md §1 |
| 2 Research layer | built; live gate deferred | offline green (39 tests) | DEFERRED_EVALS.md §2 |
| 3 Analysis + engagement state | built | offline gate passed (15 tests) | no live component |
| 4 Playbooks + retrieval | built | offline gate passed: leakage, retrieval 95.5% top-5, originality | BM25, no embeddings |
| 5 Agents + engagement loop | built; live gate deferred | offline green (74 tests); 1 smoke call OK | DEFERRED_EVALS.md §5 |
| 6 Deliverables | built | offline gate passed (83 tests total); 2 smoke calls | live analyst retry path untested |
| 7 Full evaluation | tooling built; evals deferred | offline green (88 tests) | DEFERRED_EVALS.md §6 |
| 8 Interface + hardening | built | offline green (95 tests); fresh-clone setup OK; live smoke partial | see final summary |

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

### 2026-10-13: Phase 0 correction (found during Phase 1)
- **qwen3.8-27b has an undocumented output-tokens-per-minute (OTPM) cap of 1,000 on this
  account.** Groq checks it when a request is admitted, using an estimate based on recent
  reply lengths. Once one reply runs past 1K tokens, every later request is refused until
  `max_tokens` ≤ 1000. Waiting does not clear it.
- Fix: per-model `max_output` (the client clamps `max_tokens`), an OTPM window in the
  limiter, and every "Request too large" 429 now raises `RequestTooLarge` immediately.
- **Test changed (rule 7):** the Phase 0 test `test_otpm_429_waits_for_the_minute` encoded
  my wrong assumption that OTPM 429s clear after a minute. Live behaviour proved otherwise,
  so it was replaced by `test_otpm_429_fails_fast_and_max_output_is_clamped`. This corrects
  a wrong expectation; the gate was not loosened.

### 2026-10-13: Phase 1 Knowledge + eval foundation (in progress)

**Built**
- `knowledge/casebook.py`: deterministic segmentation. Case start pages come from the
  contents table; spans are cross-checked against each page's footer (all 58 consistent);
  the footer's difficulty/firm wins over the contents table (2 conflicts: Indie Band,
  Telecom Operator). **Speakers come from text colour: 12pt white = interviewer, 12pt
  black = candidate**, consistent in all 58 cases. Summary slides go to notes.
- `knowledge/extract.py`: the LLM only *annotates* (labels each interviewer turn as
  scoping / data / none, gives the trigger, and writes structure, key insight,
  recommendation and takeaways). Answers and data are the interviewer's verbatim text.
  "none" turns that contain numbers become data; unlabelled turns are kept as data;
  remaining "none" turns are kept as `other_replies`, which the simulator can still give.
  Models alternate gpt-oss-120b / qwen3.8-27b to split quota; if one model can't produce a
  valid annotation (qwen's 1K cap on long cases), the case falls to the other.
  Industry reports → 120 tagged chunks (local qwen3:4b-instruct).
  Stratified split (seed 20241013): 43 train / 15 test, every case type in test.
- `data/derived/bridgeway_notes.md`: my own notes on the guide, for the Phase 4 playbooks.
  Deliberate departure from the guide: material risks are always disclosed.
- `evals/simulator.py`: the LLM matches a question to case items; the reply is the
  case's verbatim text or "not available"; it never invents data.
- `evals/judge.py`: 9-dimension rubric, 1–5 with justifications, on qwen (a different
  family from the writer model).
- `evals/baseline.py`: one model (gpt-oss-120b, same as the engagement manager), plain
  prompt, at most 8 questions, through the same simulator and judge. Resumable per case;
  stops cleanly on quota exhaustion.

**What went wrong and was fixed**
- The first extraction attempt (unlabelled text) swapped speakers: e.g. Pump Manufacturer
  stored the interviewer's key data as a candidate question. That attempt was discarded in
  favour of colour-labelled turns.
- gpt-oss-20b mislabels turns and invents recommendations/feedback, so it is not used for
  extraction.
- A run was killed once by Claude Code under memory pressure; it resumed from cache.

**Gate results so far**
- Extraction: 58/58 cases, 100% schema-valid (`load_cases` validates every file).
- Spot-check, 10 random cases (seed 101): fitness-studio, intel-i7, tractor,
  new-medicine, atm-fee, insurance-product, second-hand-car, mama-earth, paint,
  data-co. **No material errors**: all stored data is verbatim; key insights and
  recommendations match the source; numbers checked (e.g. Paint INR 984/can).
  Weaknesses: steering turns ("focus on pre-purchase") were dropped (fixed via
  `other_replies`); some interviewer prompts are labelled data (harmless noise); the
  feedback field sometimes mixes in case-statement bullets because the slide's sections
  run together (cosmetic).
- Simulator: 7/7 relevant questions unlock the right item, 5/5 irrelevant unlock nothing
  (fictional bakery fixture).
- Judge discrimination: 5/5 train cases; strong (real interview) vs flawed:
  4.11/1.11, 5.00/1.22, 4.89/1.11, 4.56/1.11, 4.78/1.22 (gap required ≥ 1.0 on ≥ 4/5).
- Baseline: running on the 15 test cases, paced by quota (~12K gpt-oss-120b tokens/case).

**Baseline protocol fixes (made before any baseline result was kept)**
- The action was named `answer`. When the simulator asked "what would you need it for?",
  the model replied with action=answer, which ended the case (seen on Crane Accident: it
  "finalised" with a paragraph on insurance caps). Renamed to `ask` / `final_answer`, and
  the prompt says to reply to interviewer questions inside `ask`. This fixes an ambiguity in
  the run protocol; it is not tuning toward answers. The 4 results from v1 were moved to
  `eval_results/_discarded/baseline-v1` and rerun.
- Groq JSON mode returns HTTP 400 `json_validate_failed` when the output is empty/invalid
  (gpt-oss-120b's medium reasoning used up max_tokens 1500). `structured()` now retries
  that like any other validation failure (unit-tested), and baseline max_tokens is 2500.

**Limiter correction: daily quotas refill continuously**
- Groq's RPD reset header equals requests used × 86.4s (80 used → 1h55m12s), so daily
  quotas are buckets refilling cap/day, not a hard 24h window. The old window blocked for
  ~22h while Groq still accepted requests. The day check is now a bucket replayed from the
  same event log (`_bucket_wait`), unit-tested for partial refill. The per-minute windows
  are unchanged.

**Simulator stability fix (found via baseline run 3)**
- The interviewer matcher (gpt-oss-20b, reasoning low) flip-flopped: the same "budget or
  headcount constraints" question matched the case's constraints item when typed with ASCII
  hyphens but not with U+2011 hyphens. Now reasoning_effort medium, and questions are
  NFKC-normalised. The gate test gained this exact pattern as a 3rd relevant question
  (stricter, now 8 relevant / 5 irrelevant); it passes.
- Baseline results made with the unstable matcher were moved to
  `eval_results/_discarded/baseline-v2-unstable-matcher` and are being rerun; all kept
  baseline results use the same simulator the system will face in Phase 7.

**Baseline isolation (agreed with the user, 2026-10-13)**
- Phase 2 proceeds in parallel; Phase 1 stays "in progress" until all 15 baseline results
  are in.
- The baseline runs from a git worktree pinned to commit `b557bfe`
  (`D:/Projects/consultantmaxxing-baseline`, detached HEAD), so Phase 2 changes can't reach
  it. Gitignored inputs (`.env`, `data/derived`, `.llm_cache`) were copied in once. Each
  result stores `code_version` (commit hash, `-dirty` if tracked files differ); the summary
  lists all versions seen. The driver retries every 20 min when quota is out; its log is
  `baseline_driver.log` in the worktree. When it finishes, copy
  `eval_results/baseline*` back to the main checkout.
- gpt-oss-120b is reserved for the baseline: the main checkout's `.env` sets
  `CONSULTANT_BLOCK_MODELS=groq/openai/gpt-oss-120b`, and the client refuses blocked models
  before sending anything (fallback roles move on; others raise `ModelBlocked`; unit-tested).
  Remove that line once the baseline is done.
- Caveat: the worktree has its own limiter state, so the two processes don't see each
  other's use of gpt-oss-20b / qwen. The server's 429s (with backoff) cover the overlap.

### 2026-10-13: Phase 2 Research layer (in progress, parallel to the Phase 1 baseline)

**Built**
- `research/web.py`: `SearchProvider` interface with keyless DuckDuckGo (`ddgs`); fetcher
  with robots.txt, 2s per-host delay, disk cache, 15s timeout, 30MB cap, HTML via
  trafilatura, PDF via pymupdf. Searches/pages are charged to run and per-question budgets.
  Search errors (ddgs rate limits) return no results and are traced, never cached.
- `research/credibility.py`: source type by domain/path: filing/government (5) >
  research/press (4) > trade (3, the default for unknown domains) > aggregator (2) > blog (1).
- `research/fact_store.py` (SQLite): CHECK constraints make a fact without `source_url` or
  `quote` impossible; estimates live in `assumptions` (label must be 'estimate'); only
  verified facts are deliverable-eligible.
- `research/extract.py`: the local extractor proposes facts. A fact is kept only if its
  quote is verbatim on the page (normalised) and contains the value. Multi-year table rows
  are aligned to header years deterministically (right-aligned, skipping TTM columns); if
  the model omits the header, the nearest year-bearing line above the row is taken from
  the page. Chunk selection uses metric synonyms and period variants.
- `research/triangulate.py`: ≥2 independent domains within 2% = confirmed; disagreements
  are kept as low/high ranges; otherwise single_source.
- `research/verify.py`: re-fetches each source; checks the quote is still there and the
  value is in the quote (non-numeric claims: LLM entailment).
- `research/estimate.py`: top-down/bottom-up estimates; the LLM proposes factors and a
  formula, Python evaluates it with a whitelist-only AST evaluator; uncited factors become
  assumption-register entries.
- `agents/research_associate.py`: plan (2–3 plain keyword queries) → search → read the most
  credible results (up to 5 tries / 3 successful reads per query) → extract → gap check →
  next query; caps per question; a 4K-token reserve for the brief. Facts are flagged
  `period=MISMATCH` against the question's year. **The brief's value must equal a cited fact
  for the asked period**; otherwise one corrective retry, then the value is withheld.
- LLM client: `scoped(budget)` thread-local per-question budgets; `local: true` providers
  (Ollama) are not charged to budgets (still traced).

**Iteration discipline**: after one diagnostic run on the first gate question (Infosys),
all iteration used 3 separate dev questions (`fact_questions_dev.yaml`: HCLTech net
income, India power generation, Titan total income). Fixes made from what they exposed:
table-column mistakes, period mismatches, the brief ignoring flags, blocked/timeout
sources, over-quoted queries.

**Known limits / decisions for the user**
- Several government sites (pib.gov.in, powermin.gov.in) return 403 to any non-browser
  user-agent. I did **not** spoof a browser UA (that would be evading bot detection); those
  sources are treated as unavailable. A keyed search/extract API (Tavily, Brave) or an
  official data API would be the clean fix.
- Third-party aggregator tables can be wrong: smart-investing.in labels HCLTech's FY24 profit
  as "Mar2025". Faithful extraction of a wrong source shows why key numbers need ≥2
  independent sources; the brief should rate such answers low.
- The local extractor (qwen3:4b-instruct) is the quality bottleneck. A 7B local model would
  likely help, but the machine had only 0.7GB free RAM, so I didn't pull one.

### 2026-10-13: switch to build-only mode (user decision)
- Groq free-tier limits made live evals take ~20h each, and Claude Code killed background
  runs twice under memory pressure. The user switched the build to build-only mode
  (CLAUDE.md, PLAN.md, DEFERRED_EVALS.md updated).
- The baseline driver was stopped (killed under memory pressure) with **0/15** results in the
  pinned worktree; its cached calls replay free on resume. **Phase 1 = built; baseline eval
  deferred.**
- Phase 2 gate eval, attempt 1 (interrupted): 2/5 correct on the first five gate questions
  (Maruti, Asian Paints right; Infosys revenue null; Infosys PAT and TCS revenue wrong).
  Failure analysis led to general fixes, untested live: column layouts only for table
  expansion, ≤1 extra (TTM) cell, prose never expanded, header must be table-like and not
  the row itself.
- Main `.env` keeps `CONSULTANT_BLOCK_MODELS=groq/openai/gpt-oss-120b` as a guard: smoke
  tests must never touch 120b.

**Phase 2 = built; live gate deferred.** Untested live: accuracy, verifier rate on real pages
after the latest table fixes, estimation fallback. Risks: free web sources (blocked
government sites, wrong aggregator tables), a small local extractor, and DDG search
quality. The keyed-search and bigger-extractor options are in the notes above.

### 2026-10-13: Phase 3 Analysis + engagement state (built; gate is fully offline and passed)
- `analysis/sandbox.py`: runs code in `python -I` in its own working directory
  (`engagements/<id>/analyses/NNN_name/`), saving code.py, stdout, stderr, result.json and
  run.json. Analysis code assigns `RESULT = {...}`. The network is blocked by a socket patch;
  timeout and memory cap come from a psutil watchdog over the **whole process tree**. On
  Windows the venv python.exe is a launcher, so the real interpreter is a child: measuring
  only the launcher missed a 400MB allocation; fixed and covered by a test. Committed
  private bytes count, since untouched zero pages aren't in RSS. Not a security boundary:
  use a container for adversarial code.
- `analysis/templates.py`: top-down / bottom-up sizing, break-even (units, revenue), unit
  economics (LTV, LTV/CAC, payback), price-volume-mix and margin bridge (both reconcile to
  zero), one-way sensitivity, scenario table, two-way grid, NPV, IRR (bisection), payback,
  CAGR, PV from CAGR. Known-answer tests are hand-checked (e.g. IRR of [-100, 60, 60] =
  13.066% from the quadratic).
- `workspace/`: `WorkspaceState` (problem, scope, case type, issue tree, hypothesis log
  with status + evidence fact ids + analysis ids, decision log, analysis index, open
  questions, deliverables, steps_done, step outputs). Atomic JSON save after every
  mutation; `facts.sqlite` (fact store + assumptions register) lives alongside.
  `ws.step(name, fn)` runs once and returns the saved output on resume.
- **Gate (offline):** templates pass known-answer tests; the sandbox blocks network
  (urllib + raw socket), times out a runaway loop, enforces the memory cap and leaves no
  orphan process; kill-and-resume: a subprocess hard-kills itself (`os._exit`) after
  step 2, the resume runs only steps 3–4, and step outputs survive.

### 2026-10-13: Phase 4 Playbooks + retrieval (built; gate ran offline and passed)
- `skills/`: 7 case-type playbooks (clarify first, structure and when it doesn't fit,
  common root causes ranked by frequency in the **train split only** (43 cases, test
  split asserted absent), analyses, traps mined from interviewer feedback, definition of
  done) + 4 cross-cutting skills (hypothesis-driven, research method, deliverables,
  strategy concepts). Original wording. `knowledge/playbooks.py` loads them.
- `knowledge/retrieval.py`: **BM25 in pure Python** over (a) train cases indexed by problem
  pattern (type, industry, problem, crux) and (b) industry primers (casebook primer chunks +
  `primers_extra.jsonl` added from research via `add_primer`). Index in `data/index/`
  (gitignored). The build refuses if a test-split case appears. Deviation from PLAN: no
  embeddings or LanceDB. The corpus is ~160 docs and the machine is RAM-constrained; BM25
  met the gate. Add Ollama `nomic-embed-text` + LanceDB later if quality or scale needs it.
- **Gate (offline, deterministic):** leakage test passes (no test id; no test problem or
  insight text in the index); retrieval eval: 22 hand-written queries (18 case-pattern + 4
  primer; train only; kept in `data/derived/evals/` because they paraphrase casebook
  problems) → **21/22 = 95.5% in the top 5** (≥80% required). The single miss
  ("television" vs "TV") was left as-is: no tuning to the eval. Originality: no shared
  12-word run between any playbook and the casebook/Bridgeway text, the extracted cases, or
  even my own Bridgeway notes (two playbook lines that echoed my notes were reworded).
- Still to wire in Phase 5: auto-extend primers after researching an industry the index
  doesn't cover.

### 2026-10-13: Phase 5 Agents + engagement loop (built; live gate deferred)
**Built**
- `agents/engagement_manager.py` (`Engagement`): scope (+ classify) → plan (playbook +
  3 similar train cases + MECE issue tree + ranked falsifiable hypotheses, competing
  explanations required) → iterations of [gather evidence → assess hypotheses (evidence
  ids validated; unknown ids dropped) → run requested analyses] → (real mode) citation
  verification + primer auto-extension → storyline → partner review (≤3 rounds) → deliver.
  Every stage is a `ws.step` (resumable). Budget: 70% of profile tokens for investigation,
  30% reserved for synthesis, so a capped run still delivers with an explicit open
  question. Checkpoints after scoping and hypotheses via a callback (interactive mode;
  auto mode passes none).
  - Case mode: questions go to the interviewer simulator; answers are stored as facts
    sourced `case://<id>/interviewer`, and numbers in them go through the same
    verbatim-quote extractor as web pages. Interview log persisted after each question.
  - Real mode: questions go to Research Associates in parallel (2 threads; their spend is
    charged to the engagement budget).
- `agents/analyst.py`: writes sandbox code; fact values injected as `FACTS[id]`,
  assumptions as `ASSUMPTIONS['S<id>']` (no retyped numbers); retries with the error.
- `agents/storyliner.py`: pyramid draft (answer, 1–5 action-titled arguments with
  F#/A#/S# refs, risks incl. unmitigated ones, next steps, open questions, confidence,
  slides).
- `agents/partner.py`: fresh-context red team (question, client prior, draft, evidence
  only). Checks unsupported claims, maths, non-sequiturs, missing branches,
  client-pleasing; counterargument + 3 CEO questions. Deterministic: any major/critical
  issue, or unjustified agreement with the client's prior, forces 'revise'.
- `deliverables/` (placeholder until Phase 6): memo.md + storyline.json.
- Eval tooling (not run live): `evals/run_system.py` (case mode on a split + judge; real
  mode), `evals/partner_eval.py` + `partner_seeded.yaml` (5 seeded flaws + clean draft,
  strict scoring), `evals/token_estimate.py`, `evals/common.code_version`.
- `config/models.small.yaml` + `CONSULTANT_MODELS_FILE`: every role on gpt-oss-20b/local,
  for smoke runs (and the pattern for a paid-model file).
- Concurrency fixes found by the real-mode test: per-thread temp files + Windows rename
  retry in `atomic_write` (cache and workspace); fact-store reads serialised.

**Offline tests (74 total green):** full case-mode engagement with a scripted LLM + real
simulator, sandbox, fact store and workspace (steps, sourced client facts, verbatim
numbers, hypothesis updates, analysis, partner revise→approve, memo); resume after
completion makes zero LLM calls; budget stop still delivers; real-mode engagement with fake
web (research, verification, primer extension); partner client-pleasing rule; strict
partner-eval scoring; run_case wiring with judge; token estimator.
**Live smoke (1 call, gpt-oss-20b):** the `Scope` schema validated on a real model
(classified profitability, captured the client's prior). Untested live: everything else.
Risks: open models as orchestrators; long prompts near the 8K-per-request ceiling on
Groq; qwen's 1K output cap for analyst/partner/judge.

**Token estimate and Phase 7 duration (requested by the user; structural estimate,
`python -m consultant.evals.token_estimate`)**

| | dev profile | full profile |
|---|---|---|
| one engagement, case mode | ~56K tokens (120b 20K, qwen 17K, 20b 20K) | ~119K |
| one engagement, real mode | ~57K | ~154K |
| Phase 7, 5 cases, current 3-model plan | ≥1.1 days | ≥2.5 days |
| Phase 7, 15 cases, current 3-model plan | ≥2.7 days | ≥4.9 days |
| Phase 7, 15 cases, all on gpt-oss-20b (small plan) | ≥6.5 days | not computed |

These are lower bounds from daily token quotas alone (200K/day per Groq model); pacing,
retries and the 8K TPM ceiling add roughly 1.5–2× wall-clock. Each Phase 7 "fix the weakest
layer and re-run" iteration repeats the cost. Replace with actuals after the first live
runs: `python -m consultant.evals.token_estimate --traces <run_id>`.

Options:
1. **Paid model for the orchestrating roles** (EM, storyliner, partner, judge): removes
   the bottleneck; the free tier stays for research and the interviewer.
2. **Spread roles across quotas**: already 3 Groq models; move the interviewer to local
   Ollama (saves ~20% of gpt-oss-20b use) and keep the judge on a different family.
3. **5-case dev eval set for iteration, full 15 only for the final run.** Important: the
   dev set must come from the **train split**, not the 15 test cases, otherwise iterating
   on it is tuning on the test split (rule 6).
   `run_system cases --split train --n 5`; the final `--split test` run happens once.
4. Dev profile for iteration, full profile only for the final run.

### 2026-10-13: Phase 6 Deliverables (built; gate ran offline and passed)
- `deliverables/render`: memo.md (answer first, 1–5 action-titled arguments with evidence
  ids, risks, next steps, open questions, a visible ⚠ block for anything untraceable),
  storyline.json + storyline.md, **model.xlsx**, sources.md (from the fact store: claim,
  value, linked source, date, type, credibility label, verified), assumptions.md
  (estimates with rationale, hypothesis status, what would change the answer, risks),
  validation.json. .docx/.pptx (optional in PLAN) not built; the markdown converts.
- `deliverables/model_xlsx.py`: Inputs sheet (every numeric fact with source URL; every
  assumption labelled ESTIMATE) and Model sheet where each analysis output declared by the
  Analyst as an arithmetic formula over F#/S#/earlier outputs is written as a **live Excel
  formula** referencing Inputs, beside the sandbox value. Non-arithmetic outputs (e.g. IRR)
  are written as values labelled "computed in sandbox". `recalculate()` re-evaluates the
  formulas from the saved file (no Excel needed) and `check()` compares them to the sandbox.
- Analysis-time formula checks (`Workspace.check_formulas`): each formula must reproduce the
  sandbox RESULT, and must not hard-code an input value. The live smoke call showed the
  model typing `22.0-19.0` instead of `F2 - F1`; that rule now catches it and feeds the
  Analyst a correction.
- `deliverables/validate.py`: every number in the answer, arguments, slides, risks and next
  steps must appear in evidence cited by that same element (fact value/quote/claim/table
  header, analysis output, assumption) or in the client's question. Handles fraction ↔
  percent, displayed rounding and years, and skips list numbering only when it counts
  1, 2, 3. Slides need an action title (≥4 words, not a generic topic); unknown refs
  flagged. The engagement runs it on every draft: violations become major issues that force
  a revision even if the partner approved.
- **Gate (offline):** validator passes a traced draft and flags an untraceable "12%",
  topic titles and unknown refs; the workbook's formulas recalculate to the sandbox values
  and change when an input changes; every slide has an action title; render writes all
  artefacts; validation fails on non-live formulas; in a scripted engagement an
  untraceable number forces a second draft.
- **Live smoke (2 calls, gpt-oss-20b):** the Analyst produced a correct sandbox result and
  formula rows; the hard-coded-input finding came from this. Untested live: the Analyst's
  corrective retry after that finding (the rerun hit the cache).

### 2026-10-13: Phase 7 Full evaluation (tooling built; all runs deferred)
- Held-out evaluation: `evals/run_system.py cases --split test` (system + judge) and
  `evals/baseline.py` (same simulator and judge); both resumable and commit-stamped.
- `evals/backtests.py` + `backtests.yaml`: 3 real-mode problems answered as of a past date
  (India FY25 GDP growth as of Dec 2024; Ola Electric e-2W leadership as of Sep 2024;
  IndusInd Bank risks as of Jan 2025), scored by the judge model on call correctness,
  key-point recall, calibration and evidence. **The outcomes were written from memory and
  are marked `verified: false`; the runner skips unverified backtests by default.**
  Research in backtest mode (`as_of`) only reads sources dated on/before the cut-off and
  skips undated ones.
- **Leak fixed:** trafilatura guessed a page date of 2024-01-01 from the body text "FY2024".
  Dates now come only from page metadata/markup (`extensive_search=False`,
  `original_date=True`); covered by a test.
- `evals/report.py` writes EVAL_REPORT.md: gate (system beats baseline on aggregate and
  ≥6/9 dimensions, compared on the same cases only), per-dimension table, tokens per run,
  first-pass failure triage by layer (weak dimension → playbook gap / research / analysis /
  synthesis / orchestration), backtest summary.
- Offline tests: date rule; backtest-mode research reads nothing undated; report gate
  pass/fail including "aggregate wins but only 5/9 dimensions" → FAIL; triage counts;
  backtest runner skips unverified and scores when included.
- Untested live: everything in this phase. No smoke call needed (the judge already ran
  live in Phase 1).

### 2026-10-13: Phase 8 Interface + hardening (built)
- `consultant/cli.py` (`uv run consult ...`): `new` (real mode, interactive checkpoints after
  scoping and hypotheses, `--auto` to skip), `resume`, `list`, `eval
  system|baseline|facts|partner|backtests|retrieval|tokens`, `report`, `knowledge build`,
  `ui`. Quota exhaustion and blocked models stop cleanly with the exact resume command.
- `consultant/ui/app.py` (Streamlit, `consult ui`): live-refreshing view (5s) of steps,
  tokens/calls vs profile caps, issue tree, hypothesis status, facts as they arrive (with
  source, credibility, verified), analyses, open questions, decision log, deliverable
  downloads, memo inline; a sidebar form starts `consult new --auto` in the background.
- README rewritten: setup, keys, profiles, running, swapping models (role mapping,
  `CONSULTANT_MODELS_FILE`), guardrails, private knowledge base, deferred evals, development.
- Guardrails: budgets with a synthesis reserve; no-network sandbox; citations required and
  verified; number-traceability validator; confidence labels; graceful quota stop; 120b
  block via `CONSULTANT_BLOCK_MODELS`.
- Bug found by an offline test: `Workspace.create(root=ENGAGEMENTS)` bound its default at
  import time, so overrides were ignored and a test wrote to the real `engagements/`.
  Roots now resolve at call time; the stray folder was deleted.
- **Offline gate:** UI renders a finished engagement, an in-progress one and the empty
  state (Streamlit AppTest); CLI `list` + graceful quota stop; full suite green (95 passed).
- **Fresh clone** (git clone, uv sync, .env from .env.example, uv run pytest): no private
  data came across; 89 passed, 3 skipped (the 3 that need the private casebook).

### 2026-10-13: final end-to-end smoke (dev profile, small-model plan, fresh clone)
Command: `CONSULTANT_MODELS_FILE=models.small.yaml uv run consult new "Why did Asian Paints'
profit margin change in FY2023-24, and what should management focus on next?" --auto
--profile dev` (all roles gpt-oss-20b; extractor local qwen3:4b-instruct; 120b blocked).

**Run 1** went through scope → plan → research → verification → draft → partner review x2
(30 gpt-oss-20b calls, 33.5K tokens; 19 local extractor calls), then crashed at the
synthesis budget. Findings, all fixed with offline tests (commit 9f359c5):
1. **Research questions lost the client's name.** The plan wrote generic questions
   ("company name raw material price increase...") and the scope wasn't passed to the
   Research Associates, so 9 searches / 18 pages produced **zero facts**. Fixed: engagement
   context goes to every research question; plan questions must name entity, metric and
   period.
2. With no facts, two hypotheses were marked "supported" on estimates alone. Fixed in
   code: only facts/analyses can support or kill a hypothesis; otherwise needs_more.
3. It assumed the margin *fell* (the client only said "changed"). Fixed in the plan
   prompt: establish the direction/size of a change before explaining it.
4. The analyst returned an empty RESULT and it was accepted. Fixed: empty = failure.
5. **Synthesis BudgetExceeded crashed the run** (draft + review + revision > the 18K
   synthesis reserve on dev). Fixed: the latest draft is delivered, unresolved partner
   issues are listed in the memo, and there's a deterministic fallback draft if none fits.
What worked live: scoping and classification, tree and hypotheses, search/fetch (with
403/429/robots handling), local extraction, citation verification, the analyst sandbox,
the validator catching an untraceable "25" and a topic-only slide title, and partner
reviews.

**Run 2 (after fixes)** stopped at the first call: gpt-oss-20b's daily quota was exhausted
(earlier runs today). The graceful stop worked as designed. It can be finished with
`cd D:/Projects/consultantmaxxing-smoke && CONSULTANT_MODELS_FILE=models.small.yaml uv run
consult resume smoke-asianpaints-2` once the quota refills. A full engagement on the free
tier needs ~35-60K tokens on one model, i.e. most of a day's quota.

## Final summary (build-only mode)

**Works and is tested offline (95 tests):** provider layer (Groq/Ollama, Anthropic/OpenAI
by config), rate limiter (per-minute windows + refilling daily buckets, OTPM, model
blocklist), cache, structured output with repair, tracing and budgets; casebook
segmentation/extraction (58 cases, speaker-labelled), simulator, judge, baseline; research
layer (search, polite fetch, verbatim-quote facts, table alignment, triangulation,
verification, estimation, research associate with caps); sandbox and analysis templates;
resumable workspace; playbooks and BM25 retrieval (gate passed offline: leakage, 95.5%
retrieval, originality); full engagement loop in case and real mode; deliverables with a
live-formula spreadsheet and number-traceability validator; CLI, dashboard, README.

**Verified live (small calls, Groq free tier):** provider smoke, simulator gate, judge gate,
case extraction, Scope schema, analyst + formulas, and most of one real-mode engagement
(see the smoke above).

**Untested live:** fact-finding accuracy after the latest research fixes; partner
discrimination; any complete engagement with deliverables from a real model; system vs
baseline; backtests (outcomes unverified); token actuals.

**To run the deferred evals** (full order and pass criteria in DEFERRED_EVALS.md):
```
# 0. paid key in .env; point roles at it in config/models.yaml; remove CONSULTANT_BLOCK_MODELS
uv run pytest && uv run pytest -m live tests/test_live.py
uv run pytest -m live tests/test_simulator.py tests/test_judge.py       # 1.1, 1.2
uv run consult eval baseline                                            # 1.3
uv run consult eval facts --dev && uv run consult eval facts            # 2.x
uv run consult eval system --split train --n 3 --name system-train      # 5.1
uv run consult eval partner                                             # 5.2
uv run python -m consultant.evals.run_system real "Why did Asian Paints' margins change in FY24?"   # 5.3
uv run consult eval tokens --traces eval-system-train                   # 5.4
# 7.x: verify backtests.yaml outcomes; iterate on --split train --n 5; then once:
uv run consult eval system --split test --name system-test
uv run consult eval backtests && uv run consult report                  # EVAL_REPORT.md
```

### 2026-10-14: Groq key rotation
- `GROQ_API_KEY` accepts several comma-separated keys. The client keeps a separate limiter
  per (model, key), sends each call to the key with the shortest wait, and moves to the next
  key when one is out of daily quota (local or server-reported via a 429 with a long
  retry-after, after which that key is skipped until the reported time). Falls back to
  another model only when all keys are out. Traces record `key_index`, never the key.
- Checked live with one tiny call per key: the three keys have independent request counters,
  i.e. separate Groq organizations, so rotation triples free-tier capacity. (Keys from the
  same org would share one quota and add nothing.)
- `consult eval tokens` now divides by the number of keys: Phase 7 on the current plan is
  ≥0.4 days for 5 cases and ≥0.9 days for 15 (token-bound; previously 1.1 / 2.7).
- The pinned baseline worktree runs pre-rotation code: keep a single key in its `.env`.

### 2026-10-14: overnight live gate runs (branch `overnight-fixes`, free tier)
Ran the four live checks with `CONSULTANT_MODELS_FILE=models.overnight.yaml` (120b roles on
gpt-oss-20b, reasoning low; partner/judge qwen3.8-27b; extractor local qwen3:4b). Full
detail, per-fix diagnosis and open decisions: **MORNING_REPORT.md**.
- Smoke `smoke-asianpaints-2` resumed to complete deliverables (2 genuine validator flags
  remain: the dev synthesis budget ran out before a revision).
- 5.1 `eval system --split train --n 3`: 3/3 complete within budget, all artefacts. Judge
  overall 1.41/5: low; see report.
- 2.1-2.3 `eval facts`: accuracy 75%, verifier 0.993, unsourced 0. Passes.
- 5.2 `eval partner`: 5/5 caught. The clean draft was rejected because the fixture's numbers
  are inconsistent (logged, not changed).
- Fixes (all with offline tests): estimator formula validation; validator FY-label and
  "(1)" list false positives; structured output salvages Groq `failed_generation`, says when a
  reply was truncated, trims over-long lists; failed analysis non-fatal; synthesis gets the
  reserve plus unspent investigation tokens; extractor collapses empty table cells and
  survives a bad chunk; brief sees quotes and titles; table rows with duplicated year
  columns / no verified header; verifier retries transient re-fetch errors.
- Offline suite: 109 passed.

### 2026-10-14: eval fairness pass (branch `eval-fairness`, after merging overnight-fixes into main)
- **Partner fixture** (`partner_seeded.yaml`): the clean draft said profits fell 20% while its
  own evidence implied -60%. Accounts are now consistent (revenue 30 flat, costs 20 -> 22,
  profit 10 -> 8) and recorded in an `accounts:` block; a test checks every number in the
  evidence and all six drafts against them (and that the seeded maths flaw really is wrong).
  Re-run: 5/5 flaws caught; the clean draft still gets `revise`, now on soft points ("conflates
  cost % with profit impact", product mix): partner severity calibration is open.
- **Repair pass** (`consultant/deliverables/repair.py`, step `repair` before `deliver`): one
  storyliner call rewrites only the flagged parts (re-cite evidence for the same entity, metric
  and period, or drop the number); whatever is still flagged becomes `[unverified figure]` and
  is listed in the memo; refs that name no evidence are dropped first. Smoke proof
  (smoke-asianpaints-2): a live synthesis re-run (fresh cache, real budget) validates clean;
  the original flagged draft (43.6 made up; 42.39 miscited x2) goes to zero flags (the model
  removed 43.6, the two 42.39s were replaced and listed). Found on the way: given a looser
  prompt the model "fixed" a flag by citing a 2026-quarter figure for FY24; the prompt now
  requires the same period, and the validator itself still doesn't check periods.
- **Judge**: two calls. Process dims (scoping, structure, hypotheses, maths, evidence,
  efficiency) see transcript + work record (system: issue tree, hypotheses, analyses, facts;
  baseline: none, its transcript is its process). Outcome dims (key_insight, recommendation,
  communication) see the final answer only. Documented in `judge.py`. Live discrimination
  re-run with the new judge: 5/5 (strong 3.9-4.8 vs flawed 1.1-1.7).
- **System vs baseline, same 3 train cases, same model (gpt-oss-20b), new judge**:
  | Dimension | Judged on | System | Baseline | Δ |
  |---|---|---|---|---|
  | scoping | process | 2.00 | 1.67 | +0.33 |
  | structure | process | 2.67 | 1.00 | +1.67 |
  | hypotheses | process | 2.00 | 1.00 | +1.00 |
  | maths | process | 2.33 | 2.33 | 0.00 |
  | evidence | process | 1.67 | 1.00 | +0.67 |
  | efficiency | process | 1.00 | 1.00 | 0.00 |
  | key_insight | final answer | 1.00 | 2.33 | -1.33 |
  | recommendation | final answer | 1.33 | 3.00 | -1.67 |
  | communication | final answer | 3.67 | 4.33 | -0.67 |
  | **overall** | | **1.96** | **1.96** | **0.00** |
  Gate (beat baseline overall and >= 6/9 dims): not met (4/9, tie). n = 3: directional only.
  Diagnosis: the system's process is better but its answers hedge ("cannot conclusively
  recommend... collect the missing data first") where the baseline commits. Next fix is in
  the storyliner (best answer + confidence + what would change it), not the judge.
  Results: `eval_results/system-train-rejudged/`, `eval_results/baseline-train/`.
- **Fact-finding split**: dev = the 9 questions inspected while fixing + 1 seeded draw;
  held-out = the other 10 (their per-question detail is no longer printed). Run 3 rescored:
  held-out 10/10, dev 5/10, all 15/20. The held-out set skews easy (all passed on the first
  run, which is why they were never inspected), so 100% overstates; fresh questions needed.
- Offline suite: 113 passed.

### 2026-10-14: answer quality (branch `answer-quality`, after merging eval-fairness into main)
Offline only (no live Groq evals); all of it is to be re-run on Claude (DEFERRED_EVALS.md,
"Re-run plan": 6 train cases = the original 3 + p031/p033/p039, which nothing was tuned on).
- **Always a best answer.** The storyliner fills `StoryDraft`, which requires the answer-first
  fields: recommendation, confidence with its reason, the 2-3 evidence ids it rests on, what
  would change it (older saved drafts still load). The prompt and `skills/deliverables.md` say
  "collect more data before deciding" is only ever a next step. The validator flags an answer
  that defers (`deferred_answer`, regex on phrasings like "cannot conclusively recommend",
  "collect data before deciding"; caught p012's real answer, none of 5 committed answers),
  the review loop sends it back as a blocking non_sequitur, and the repair pass gets one
  targeted rewrite. The budget fallback draft now commits to the best-supported hypothesis
  at low confidence. The number rules are unchanged. Memo and judge text lead with
  recommendation → confidence + reason → rests on → what would change it.
- **Partner: blocking vs comments.** Blocking = unsupported_claim, maths, non_sequitur,
  missing_branch, client_pleasing at major/critical. New non-blocking kinds: wording,
  emphasis, framing (and minor anything). Verdict set in code: approve with comments when
  nothing blocks. The prompt says to recompute before calling something maths (the false
  objection on the clean draft). `partner_eval` passes only with 5/5 caught as blocking AND
  the clean draft approved. Offline test: realistic clean-draft comments → approved; every
  planted flaw → blocking, even when the model itself says approve. Live behaviour untested.
- **Period check.** A number taken from a cited fact must be for the period the claim states
  (FY24 claim → FY24 fact; a quarterly fact can't back an annual claim). Test uses the smoke
  case (a Jul 2026 quarterly margin cited for FY24). Month names don't count as sub-year:
  "Mar 2024" labels a full fiscal year in Indian sources. Period flags go through the repair
  pass like unsupported numbers. On the stored drafts: no false positives.
- **Efficiency.** Both candidates scored 1.00 because both really were inefficient (judge notes:
  questions spent on data the case doesn't have, repeats), but the judge only had a raw
  question count and a vague rubric. Efficiency is now measured, not judged: wasted questions
  (unanswered + no new information) vs the reference interview's candidate turns, with fixed
  anchors; tokens and searches are reported, not scored (scoring tokens would penalise the
  research architecture by design). On the stored runs 62-88% of questions went unanswered
  for both. That also raises a question about the simulator's item matching (logged in PLAN).
  Offline discrimination test: an efficient run scores 5, a wasteful one 1, at every
  reference length, and the score never rises as waste grows.
- **Fresh fact questions.** `consultant/evals/fact_questions_fresh.yaml`: 10 proposed
  questions (exports, GST, two-wheelers, forex reserves, major-port cargo, Kotak, Tata Steel,
  Sun Pharma, NTPC, FDI equity), each with the answer found, source URL, source type and a
  confidence note. Not used or run; a test checks nothing in `consultant/` loads them.
- Offline suite: 119 passed.

### 2026-10-14: simulator calibration + Claude on the Pro subscription (branch `claude-provider`)
**Fresh fact questions:** user spot-checked all 10 (`fact_questions_fresh.yaml`); committed on
answer-quality before the merge. answer-quality merged into main; this branch is not.

**Part 1: case interviewer calibration (offline).** Calibration sets are built at run time from
the casebook's own interviews (`consultant/evals/simulator_calibration.py`; train split only,
the 6 re-run cases excluded, nothing derived committed). Positives: real candidate turns whose
real interviewer reply hands over a case item (the item's words are >= 60% in the reply).
Negatives: fishing / off-topic questions, worded differently for the fit and check halves.
The LLM part can't run offline on Claude, so it was measured on the local model
(ollama qwen3:4b-instruct, no network or quota); Haiku will differ.

| half | matcher | right item answered | any answer | precision | negatives refused |
|---|---|---|---|---|---|
| fit (89 pos / 160 neg) | legacy | 58% | 92% | 0.36 | 81% |
| fit | new | 65% | 94% | 0.51 | 96% |
| check (98 pos / 160 neg) | legacy | 67% | 100% | 0.41 | 76% |
| check | new | 68% | 97% | 0.54 | 99% |

The new matcher: fishing requests refused before any model call (regex, messages of <= 30
words; catches 9/10 unseen check wordings, 0 of 187 real questions); a hand-over prompt
written from how the real interviews work; the model's top item plus the BM25 top item when it
clearly matches (score >= 6, set on fit); at most 2 items. Caveats: the 4B model, not Haiku,
did the picking; I saw the check half's fishing false positives once before adding the
30-word limit. On the live Claude run below, Haiku answered 11 of 12 questions (previously
62-88% went unanswered on gpt-oss-20b).

**Part 2: Claude via the subscription.** ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN: not set in
any .env (the line is empty), the shell, Windows user/system/process env, or Claude Code's
config (no apiKeyHelper / primaryApiKey); Claude Code is OAuth-logged-in.
- `consultant/llm/claude_cli.py`: `claude -p --output-format stream-json --verbose --model
  <m> --system-prompt-file <role prompt> --tools "" --strict-mcp-config --setting-sources ""
  --disable-slash-commands --no-session-persistence`, prompt on stdin, empty temp cwd, API key
  vars stripped from the child env. `--bare` is avoided (it only accepts an API key).
  stream-json's final result event is what `--output-format json` prints; the stream also
  carries the rate-limit event (5-hour / 7-day utilization), which is logged per call.
  Concurrency 1. Usage limit -> wait and retry every 20 min (up to 72 h, then a clean
  QuotaExhausted stop; rerun resumes). Tokens (incl. cache split) and notional cost go to the
  ledger and traces. JSON goes through the existing structured-output helper; no agent changes.
- `config/models.claude.yaml` (Sonnet: EM, research, analyst, storyliner, partner, baseline;
  Opus: judge; Haiku: interviewer, extraction) and a `claude` budget profile (1M tokens,
  250 calls, 3 iterations, 12 questions).
- `consult compare <problem.md> <human_answer.md>`: real-mode run, both answers through the
  same blinding (plain text, no evidence ids or system-only sections), random A/B with the
  key in `key/mapping.json`, each judged in a fresh uncached Opus call on a reference-free
  rubric. Offline test only; not run live.
- Offline tests use recorded CLI streams (`tests/fixtures/claude_cli/`, sanitized; the
  usage-limit stream is synthetic, in the recorded shape).
- **Live checks** (the only live calls): smoke call per model, all `apiKeySource: none`
  (Haiku 2.2 s, Sonnet 1.5 s, Opus 1.6 s, ~760 input tokens each). One case-mode engagement,
  `iimb-p056-grocery-retail-chain` (not in the re-run or calibration sets), system only:
  **5 min 45 s, 35 calls (17 Sonnet / 18 Haiku), ~140K tokens (Sonnet 106K, Haiku 33K),
  notional $0.54, 5-hour window 1% -> 5% (~4%), 7-day window unchanged at 41%**, no limit
  waits, within budget (14% of the claude cap), validation passed, committed answer (low
  confidence), interviewer answered 11/12. One analyst call came back flagged as failed with
  a valid-looking reply and succeeded on retry; failures now log subtype/exit/stderr.
- **6-case estimate** (DEFERRED_EVALS.md): about 1-1.5 h and roughly half of one 5-hour window
  from a fresh window; judge (Opus) cost is the least certain part (not measured).
- Offline suite: 130 passed.

### 2026-10-14: diagnosis of the 6-case Claude eval (system 2.67 vs baseline 3.04)
Measurement only, no system changes. Train split only.
- **p033 (recommendation 2 vs 5).** (Case details are left out: casebook content is not
  committed.) The baseline did the decision maths in its answer and picked the reference's
  option. The system's first two drafts picked the same option. The partner's first two
  reviews then argued for the one analysis (of 10) whose favourable assumptions supported
  the alternative, and the third draft switched to it. The third review found that analysis
  used a wrong unit cost for one scenario (critical), but no rounds were left; the repair
  pass only fixes numbers, so the switched draft shipped with 6 unresolved points. Causes:
  10 analyses with conflicting assumptions and no reconciled model; each revision rewritten
  from scratch without the previous draft; the last draft ships, not the best-reviewed one.
- **p017 (communication 3 vs 4; key insight 4 vs 5).** The case reveals its revenue-side
  facts only when the candidate answers the interviewer's prompts. The baseline answered
  them and got those facts; the system only sent data requests and search-style queries,
  never received them, and recommended the opposite of the reference. Lost at the interview,
  before any analysis. Communication: hedging, evidence ids, unrounded floats (15+ digits),
  open questions listed twice.
- **Context-loss bug (confirmed).** `evidence_text` cuts every fact claim at 220 chars.
  Client replies are stored as "Client, asked '<question[:120]>': <reply>", so the
  storyliner, partner, assessor and planner see only ~82 chars of each reply (measured
  on all 6 cases). Replies without digits get no extracted facts, so the rest is lost.
  Example: in p012 the client's stated budget and motive for entering never reached any
  agent; the judge marked the answer down for calling the client's capital unknown, and the
  EM asked about capital again. The brief also omits the original problem statement.
  The 60-fact limit dropped nothing.
- **Format bias (outcome dims, Opus uncached; result in
  `eval_results/format-bias-train6.json`).** Gap system minus baseline, mean of
  key_insight, recommendation and communication: original -1.00; same text re-judged -1.11
  (judge stable: 33/36 cells identical); both rewritten by Sonnet into plain prose with the
  same prompt (cap 300 words; came out at 296-394 for the system vs 355-443 for the
  baseline) -0.94. Communication stays -1.0 in neutral prose. The loss is in content
  (hedging, gating, caveats), not format. Note: the outcome judge clips answers to 2,700
  chars; the system's are 6.7-8.6K, so the judge never saw its arguments.
- **Efficiency.** The system asked 12 questions in all 6 cases (the profile cap); the
  baseline asked 2-7 with 0 unanswered. System waste (unanswered + no new info): 11, 8, 10,
  9, 10 and 7 of 12. 16 of the 72 questions were web-search queries sent to the client (12
  unanswered); near-repeats: 3, 3, 1, 1, 1 and 2. The EM over-asks: no stop rule, research
  queries go to the client in case mode, it can't see earlier answers (context-loss bug),
  and it never answers the interviewer's prompts. Part of the "no new info" count is the
  simulator re-sending item 0 for clarifications it can't answer (p033, p012).
- **Judge issue (not changed):** process dims see only the baseline's transcript, not its
  final answer, so its correct maths in p033 scored maths 1 and evidence 1.
- **Partner fixture:** F3 listed base-year cost lines next to the Rs 22 crore total (6 + 4 +
  10 = 20), which the partner flagged. Each line now states both years (6 -> 7.8, 4 -> 4.2,
  10 flat); the consistency test now requires both years for every line. Partner eval not
  re-run live.
- Proposed fixes (not implemented; ranked in the session's report): full client replies in
  the brief; a case-mode interview that answers prompts, sends only client questions and
  stops when nothing new comes back; revisions that see the previous draft and ship the best
  reviewed draft; one reconciled decision model; calibrated confidence and a cleaner final
  answer.
- Offline suite: 130 passed.

### 2026-10-14: judge fix + corrected reference (branch `fix-context-loss`, step 1 of 3)
**Why the judge changed (rule 7: the gate was wrong, not the system).** Two measurement
flaws found in the diagnosis above, both now fixed in `judge.py`:
1. The outcome call clipped the final answer to 900 tokens (~2,700 chars, head + tail).
   The system's answers are 6.7-8.6K chars, so the judge saw the recommendation and the
   open questions/slide titles but none of the supporting arguments. Now `ANSWER_CLIP =
   6000` tokens, the same for both sides, above every answer seen so far.
2. The process call saw the transcript and work record only. The baseline keeps no work
   record and does its maths in its answer, so correct maths scored 1 (p033). The process
   call now also sees the final answer, for both sides. `test_run_case_produces_judged_result`
   asserted the old design ("process must not see the answer"); the assertion now requires
   the answer to be there. That's a deliberate design change, not a weakened test.
Re-judged the stored runs with the fixed judge (no new engagements; Opus, 24 calls):
`eval_results/system-train6-j2`, `eval_results/baseline-train6-j2`. **This is the new
reference** (`report --split` prints it):

| Dimension | System (old judge) | Baseline (old) | System (fixed) | Baseline (fixed) | Diff (fixed) |
|---|---|---|---|---|---|
| scoping | 3.00 | 3.50 | 2.83 | 3.67 | -0.83 |
| structure | 3.33 | 1.50 | 3.33 | 2.83 | +0.50 |
| hypotheses | 2.67 | 2.00 | 2.67 | 2.00 | +0.67 |
| key_insight | 2.67 | 3.50 | 2.50 | 3.33 | -0.83 |
| maths | 3.00 | 3.17 | 3.33 | 3.83 | -0.50 |
| evidence | 2.50 | 1.83 | 2.50 | 2.67 | -0.17 |
| recommendation | 2.33 | 3.50 | 2.33 | 3.50 | -1.17 |
| communication | 3.00 | 4.00 | 2.67 | 3.67 | -1.00 |
| efficiency | 1.50 | 4.33 | 1.50 | 4.33 | -2.83 |
| **overall** | **2.67** | **3.04** | **2.63** | **3.31** | **-0.69** |

The fix mostly helped the baseline: once the judge sees the baseline's answer, its maths,
evidence and structure count. The system now wins only structure and hypotheses (2/9). The
old judge was flattering the system's process wins.
- `report.py --split`: tables for the 6 studied cases, the untouched cases and both combined.
- Offline suite green.

### 2026-10-14: fixes 1-5 (branch `fix-context-loss`, step 2 of 3; offline only)
Built against the 6 studied train cases' failure modes; nothing tuned on the test split.
1. **Context loss.** A client reply is stored whole as the claim (no "Client, asked..."
   prefix); a reply already on file returns the existing fact; `evidence_text` shows client
   facts in full and never drops them (web facts still clipped to 220 chars, latest 60).
   The brief, assessor, planner and partner get the problem as the client stated it.
   Regression test: a 330-char, digit-free reply reaches the brief whole, stored once (fails
   on the old code).
2. **Interview as a conversation** (`agents/interview.py`, `_ask_client`). Deterministic
   rules: `prompts_candidate` (the reply asks the candidate something or tells them what to
   analyse next, e.g. "Sure, go ahead", "Let's focus on volume"), `search_style` (a web query,
   never sent to the client), `repeats` (Jaccard >= 0.35 against questions already asked).
   On a prompt, the EM answers with a structure/hypotheses and at most one question
   (`RESPOND_SYS`, one LLM call per prompt). Two replies in a row with nothing new end the
   interview (`STALE_STOP`); `max_questions` stays only as a ceiling. Case-mode planner and
   assessor are told to write questions as speech and see what was already asked.
   **Checked on real casebook interviews**: train cases 13-43 only (31 cases, not the 12 eval
   cases), aggregates only, via `test_rules_hold_on_real_casebook_interviews` (skips without
   local data). Prompt detector vs "the real candidate answered with substance next":
   precision 0.87, recall 0.68 (most misses are data replies the candidate analysed
   unprompted, i.e. not prompts). Search-style flags 2 of 291 real candidate turns, and all
   16 search queries the system sent in the first Claude run. Repeat filter flags 1 of 291
   real turns and 9 of the system's 72 questions. 0 of 31 real interviews had two
   non-informative replies in a row before the end, so the stop rule wouldn't have cut any
   real interview short. Live smoke: one Sonnet `CandidateTurn` call (answers the prompt with
   hypotheses, then one data question).
3. **Review loop.** Revisions get the previous draft and are told to keep its
   recommendation unless a blocking issue shows the evidence contradicts it (a
   counterargument alone never flips it). The reviewed draft with the fewest blocking issues
   (then fewest critical, then the later one) ships; `notes.shipped_draft` records it and
   `unresolved_review` is that draft's own blocking list. Regression test reproduces p033's
   shape with synthetic content (6 / 3 / 1 critical + 5 blocking issues): draft 1 ships.
4. **Reconciled decision model** (step `model`, before `story0`, synthesis budget). With 2+
   successful analyses, the analyst builds one model: `inputs`, `assumptions` (each
   modelling choice and why), `options`, `best`, `sensitivities` (the readings the earlier
   analyses disagreed on). Earlier analyses get `superseded_by` and show in the brief as
   "SUPERSEDED (don't cite)"; they stay in model.xlsx for audit. A failure leaves them as
   they were.
5. **Clean, calibrated answer.** Storyliner prompt + `skills/deliverables.md`: low
   confidence only for a named gap that would flip the answer; numbers rounded to 2-3
   significant figures; no evidence ids in prose; use the reconciled model. Rendering:
   `answer_md` (memo body, and now the judge's final answer) strips id groups, renames ids
   used as words ("in A4" -> "in the analysis"), rounds raw floats (`display_numbers`),
   and merges the draft's and the engagement's open questions into one de-duplicated list.
   The memo's ids move to a Sources section with readable labels. The validator accepts
   display rounding to 2+ significant figures (42,500 for 42,525), not 1 (1,000 for 1,450
   is still flagged). Confidence calibration is prompt-only; whether Sonnet follows it is
   untested.
- Tests changed by design (not weakened): the end-to-end step list gains `model`; the memo
  test checks the Sources section instead of an inline "Rests on: F1, A1" and asserts the
  answer body has no ids.
- **Untested live:** everything above except the one smoke call. Risks: the prompt-reply
  turn adds calls per case; the stop rule may end interviews early on cases whose first
  questions miss; the reconcile step may fail on qualitative cases (it falls back to the
  existing analyses).
- Offline suite: 149 passed. Step 3 command: DEFERRED_EVALS.md ("Re-run after the
  context-loss fixes").

### 2026-10-15: history rewrite for a copyright leak + originality pre-commit hook
**Step 3 (12-case re-run) has not run:** on 2026-10-15 no `system-train12-v2`,
`baseline-train12`, engagements or traces existed in this checkout, the worktrees or the other
clones. The launch command in DEFERRED_EVALS.md is unchanged and still to run.

**Audit** (every blob, all branches, local and remote; 63 commits). Method: 12-word and
8-word shingle overlap with the extracted casebook and Bridgeway text and the extracted
cases, a 6-word pass against the casebook, a pass for 3-word phrases unique to one case,
a scan of added lines for case-specific nouns, risky paths, fixtures and commit messages.
Found (no details here, by design):
1. ab89e48 (and 0988d9f, which carried it): PROGRESS.md diagnosis entry with one verbatim
   casebook sentence plus case figures and case facts for two studied cases.
2. 9ae6d84: an 8-word casebook candidate phrase in a tests/test_simulator.py string.
3. 7671900: a tests/test_deliverables.py example answer restating a case fact.
4. a3f1343: a made-up search query in tests/test_interview.py that echoed a case's query
   (no case fact; changed to be safe).
Clean: no data/, PDF, derived, index, eval_results, engagements or traces path ever
committed; claude_cli fixtures hold no case text; commit messages clean; 8-word skills hits
match only our own notes. The shingle checks did not catch #3 and #4 (paraphrase level);
they were found by the noun scan.

**Rewrite** (`git filter-repo` via uvx, two blob-callback passes with exact old -> new
strings; local mirror backup at `D:/Projects/consultantmaxxing-prerewrite-backup.git`,
which still holds the old objects: delete it once satisfied). Rewritten branches (old ->
new tip): claude-provider ab89e48 -> 45e9728, fix-context-loss 90730b0 -> a6d4e86 (37e1633,
the earlier scrub commit, became empty and was dropped), main f4b8187 -> e1745ec (local only),
answer-quality 9c3c7de -> 02e0ec5 (local only). Unchanged: eval-fairness, overnight-fixes,
and the public main 765bed5. At every tip the only differences are the replaced strings.
Force-pushed claude-provider (lease on ab89e48); pushed fix-context-loss (new on the remote).
Local main and answer-quality weren't pushed: their public copy (main 765bed5) never had the
leak. After the rewrite: tests pass, the audit finds nothing, and the four strings appear
nowhere in history.

**GitHub:** 0 forks, no PRs, no Actions runs. The old commits that were public (ab89e48,
9ae6d84, 7671900) still resolve by SHA (API and web, HTTP 200); 0988d9f was never pushed
(404). Removing them from GitHub needs a request to GitHub Support (sensitive data removal),
citing those SHAs.

**Hook:** `.githooks/pre-commit` -> `consultant/originality.py`: blocks staged text sharing an
8-word run with the sources (12 missed the 11-word quote) and any data/ or PDF path; enable
per clone with `git config core.hooksPath .githooks` (done here); `.gitattributes` keeps it LF.
On the current tree (102 files) it finds nothing. Tests: unit checks plus a real `git commit`
in a temp repo blocked and then allowed. CLAUDE.md rule 1 notes the hook and says case facts
stay out of notes (the hook can't catch paraphrase).

### 2026-10-16: ablation variants (branch `ablations`; offline only, nothing run live)
12-case result (fixed judge, `system-train12-v2` vs `baseline-train12`): system 3.06 vs
baseline 3.34; untouched 6: 2.98 vs 3.37. The system wins structure, hypotheses and maths
and loses key insight, recommendation, communication and efficiency. Instead of more fixes,
three variants behind config flags; the default system's prompts and flow are unchanged (its
investigation calls are byte-identical, so cached runs still replay).
- **A, Opus brain:** `config/models.claude-opus.yaml` = `extends: models.claude.yaml` with
  engagement_manager and storyliner on Opus (the storyliner's repair pass follows the role).
  `models_config()` now merges an `extends` base file section by section.
- **B, single-brain synthesis:** budget profile `claude-single` (`claude` + `synthesis:
  single`, YAML merge key). Pipeline as now through the decision model; the storyliner call
  then gets `_full_context()` (problem as stated, the whole interview, the live decision model
  with its full result incl. inputs, every fact unclipped; no hypotheses) instead of the
  brief, with the same storyliner prompt and output contract. Two rounds: partner review; if
  it blocks, one revision of the same call, which is also reviewed so `best_reviewed` can pick;
  repair pass unchanged. Reading of "the partner reviews once": once per draft, two drafts max.
- **C, baseline+:** `consult eval baseline --plus`. Baseline loop with all 11 playbooks and
  the analysis templates in the system prompt (~24K chars) and a third action, `analyse`,
  that runs the script in the sandbox (max 4 runs on top of the 8 questions; runs in
  `engagements/<name>/<case>/`). The candidate sees each RESULT; the interviewer never does.
  The judge's transcript is the interview only (efficiency is measured on it); the runs go in
  the work record, as the system's analyses do. The conversation clip is 6000 tokens (the
  baseline's 4000 would cut code and results first).
- **Report:** `report --compare RUN ...` prints studied 6 / untouched 6 / all, one column per
  run, on the cases every run has finished. On the existing runs it reproduces 3.06 vs 3.34.
- Judge, simulator and rubric untouched. Tests: `tests/test_ablations.py` (A's merged config;
  B's input, two rounds and best-draft shipping, default profile still on the brief with 3
  rounds; C's prompt, sandbox results, failed run, budget end, clean transcript; compare
  table). Offline suite: 158 passed.
- **Untested live:** all three variants. Risks: Opus on Pro burns the window faster (A is
  the long leg); B's full context is large on cases with many facts; C may spend its turns
  analysing instead of asking. Launch command: DEFERRED_EVALS.md ("Ablations").

### Ablation result and confirmation setup (2026-10-16)
- **Result (12 train cases):** C (baseline+) 3.51 overall, 3.46 on the untouched 6; baseline
  3.34 / 3.37; A (Opus brain) 3.23; B (single-brain synthesis) 2.98.
- **Confirmation, not yet run:** C, plain baseline, and C with Opus as the agent
  (`config/models.claude-opus-agent.yaml`: only the `baseline` role on Opus) on the next 6
  train cases, plus C vs baseline pooled over 18. No code change: the first 12 are seeded by
  copying result files (the runner skips existing ones). Command: DEFERRED_EVALS.md.
- **Next:** PLAN.md section 5b, rebuild around one agent (C), only if the confirmation holds.
