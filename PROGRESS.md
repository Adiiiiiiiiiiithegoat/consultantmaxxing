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
