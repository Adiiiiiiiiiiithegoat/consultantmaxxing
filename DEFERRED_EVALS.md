# Deferred live evaluations

Build-only mode (see CLAUDE.md) deferred every gate check that needs live LLM calls. Run
them in this order once a paid key is available. All runners resume from where they stopped.

## 0. Before running anything
1. Put the paid key in `.env` (e.g. `ANTHROPIC_API_KEY=...`).
2. In `config/models.yaml`, point the roles at the paid models (e.g.
   `model: anthropic/claude-sonnet-5-5`) and add their limits under `models:`.
   Keep the judge on a different model family from the writer where possible.
3. Remove the `CONSULTANT_BLOCK_MODELS=...` line from `.env` (it reserves gpt-oss-120b).
4. `uv sync && uv run pytest` must be green (offline suite).
5. `uv run pytest -m live tests/test_live.py`: one call per configured provider.

## 1. Phase 1: evaluation foundation
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 1.1 | Simulator reveals only relevant data | `uv run pytest -m live tests/test_simulator.py` | 8/8 relevant, 0/5 irrelevant (passed once on Groq, 2026-10-13) |
| 1.2 | Judge discrimination | `uv run pytest -m live tests/test_judge.py -s` | strong beats flawed by ≥1.0 on ≥4/5 (5/5 on Groq 2026-10-13; 5/5 again 2026-10-14 after the process/outcome split; re-run: efficiency is now measured, not judged) |
| 1.3 | Baseline on the 15 test cases | `uv run python -m consultant.evals.baseline` | all 15 in `eval_results/baseline/`, summary in `eval_results/baseline.json` |

Notes for 1.3: the pinned worktree `D:/Projects/consultantmaxxing-baseline` (commit
`b557bfe`) holds a partial Groq run (0/15 finished; cached calls replay free). If roles
change to a paid model, rerun from scratch in the main checkout instead: the baseline must
use the same model as the system's engagement manager, and each result records
`code_version`. Rerun 1.1 and 1.2 too if the interviewer/judge model changes.

## 2. Phase 2: research layer
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 2.1 | Fact-finding accuracy (20 gate questions: 10 dev + 10 held out) | `uv run python -m consultant.evals.fact_finding` | `accuracy_heldout` ≥ 70% within tolerance (held-out per-question detail is not printed; iterate on dev) |
| 2.2 | Citation verifier | same run (`eval_results/fact_finding/summary.json`) | `verifier_rate` ≥ 0.90 |
| 2.3 | No deliverable-eligible fact without a source | same run | `eligible_without_source` = 0 (also schema-enforced and unit-tested) |
| 2.4 | Dev set for iteration (not a gate) | `uv run python -m consultant.evals.fact_finding --dev` | used to debug before 2.1 |

Notes: an interrupted attempt on Groq (gpt-oss-20b + local qwen3:4b-instruct extractor)
got 2/5 before it was stopped; the table/prose fixes made after it are untested live.
Iterate on the dev set, not the 20 gate questions.

## 3. Phase 4: playbooks + retrieval
Ran offline and passed (leakage, retrieval 95.5% top-5, originality); nothing deferred.
Re-run after changing the index: `uv run pytest tests/test_knowledge.py`.

## 4. Phase 5: agents + engagement loop
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 5.1 | 3 train cases end-to-end in case mode | `uv run python -m consultant.evals.run_system cases --split train --n 3 --name system-train` | all 3 complete within budget (`within_budget` = 3), all artefacts in each workspace |
| 5.2 | Partner catches seeded flaws | `uv run python -m consultant.evals.partner_eval` | all 5 caught as blocking (named kind, major/critical) and the clean draft approved (comments allowed); `pass` in the output checks both |
| 5.3 | One real-mode run on a simple public-company question | `uv run python -m consultant.evals.run_system real "Why did Asian Paints' margins change in FY24?"` | completes; deliverables written; facts verified |
| 5.4 | Token actuals | `uv run python -m consultant.evals.token_estimate --traces eval-system-train` | replace the PROGRESS.md estimate |

Use a 5-case **train** subset (`--n 5`) for iteration; never iterate on the test split.

**Re-run plan after the answer-quality changes (2026-10-14).** Use 6 train cases: the
original 3 (p012, p017, p025) plus the next 3 in the train order (p031, p033, p039), which no
fix was developed against. Same model for system and baseline.

**On Claude via the Pro subscription** (`config/models.claude.yaml`: Sonnet for EM, research,
analyst, storyliner, partner and baseline; Opus for the judge; Haiku for the interviewer and
extraction; `claude` budget profile). No API key is used (llm/claude_cli.py strips it). One
line in a plain PowerShell terminal (resumable: rerun the same line after any stop):
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; uv run consult eval system --split train --n 6 --name system-train6 --profile claude; uv run consult eval baseline --split train --n 6 --name baseline-train6; uv run consult eval partner
```
Then the comparison:
```
uv run python -c "from consultant.evals.report import *; from consultant.config import ROOT; print(gate(load_rows(ROOT/'eval_results','system-train6'), load_rows(ROOT/'eval_results','baseline-train6')))"
```
Expected time on Pro (from one measured case-mode engagement, 2026-10-14: 5 min 45 s, 35
calls, ~140K tokens, ~4% of the 5-hour window): system ~35 min for 6 cases (~24% of a window);
baseline ~15 min (~6-12%); judge 24 Opus calls ~15 min (not measured; ~12-24%); partner eval
~1 min. About 1-1.5 hours and roughly half of one 5-hour window if it starts in a fresh
window and nothing else (including Claude Code sessions) uses the plan meanwhile. If the
limit is hit, calls wait and retry every 20 minutes until the window resets (worst case adds
up to ~5 hours); nothing is lost.

Report the original 3 and the new 3 separately as well as together. Fact-finding: report
`accuracy_heldout`; the 10 spot-checked questions in `consultant/evals/fact_questions_fresh.yaml`
become the held-out set when that eval is next wired up.

**Done 2026-10-14** (system 2.67 vs baseline 3.04; fixed-judge reference 2.63 vs 3.31, see
PROGRESS.md). Superseded by the re-run below.

**Re-run after the context-loss fixes (branch `fix-context-loss`).** System on the 6 studied
train cases plus the next 6 in train order (p042, p044, p046, p049, p054, p056: nothing was
developed against them). Baseline only on the new 6; the studied 6 reuse the baseline runs
re-judged with the fixed judge (`baseline-train6-j2`, copied in). Same models config, fixed
judge for everything. One line in a plain PowerShell terminal, resumable (rerun the same
line after any stop):
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; New-Item -ItemType Directory -Force eval_results\baseline-train12 | Out-Null; Copy-Item eval_results\baseline-train6-j2\*.json eval_results\baseline-train12\; uv run consult eval system --split train --n 12 --name system-train12-v2 --profile claude; uv run consult eval baseline --split train --n 12 --name baseline-train12; uv run python -m consultant.evals.report --system system-train12-v2 --baseline baseline-train12 --split
```
The last command prints three tables: studied 6, untouched 6, combined. If the studied 6
improve much more than the untouched 6, the fixes fitted the cases we read. Note: cases 7-12
were in the simulator calibration set (the interviewer's matching, not the system); the new
interview rules were checked on train cases 13+ only. Expected: system ~1.5-2 h (12 cases,
fewer questions but a reconcile step and prompt replies), baseline ~15 min, judge 36 Opus
calls; roughly one 5-hour window, so a limit wait is possible (calls wait and resume).

**Done 2026-10-16:** system 3.06 vs baseline 3.34 (untouched 6: 2.98 vs 3.37). The system
wins structure, hypotheses and maths; loses key insight, recommendation, communication and
efficiency.

**Ablations (branch `ablations`).** Three variants on the same 12 train cases, each under
its own name; the default system is unchanged.
- A `ablation-a-opus`: `CONSULTANT_MODELS_FILE=models.claude-opus.yaml` (Opus for the
  engagement manager and storyliner, incl. its repair pass), `claude` profile.
- B `ablation-b-single`: `claude-single` profile (`synthesis: single`): pipeline as now up
  to the decision model; one storyliner call writes from the problem statement, full
  transcript, decision model and all facts; partner review; one revision if it blocks (also
  reviewed); best-reviewed draft ships; repair pass as before.
- C `ablation-c-baseplus`: `consult eval baseline --plus`: baseline + all playbooks in its
  system prompt + an `analyse` action that runs the sandbox (max 4 runs on top of 8 questions).
One line in a plain PowerShell terminal, resumable (rerun the same line after any stop):
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; uv run consult eval system --split train --n 12 --name ablation-b-single --profile claude-single; uv run consult eval baseline --split train --n 12 --name ablation-c-baseplus --plus; $env:CONSULTANT_MODELS_FILE="models.claude-opus.yaml"; uv run consult eval system --split train --n 12 --name ablation-a-opus --profile claude; uv run python -m consultant.evals.report --compare system-train12-v2 baseline-train12 ablation-a-opus ablation-b-single ablation-c-baseplus
```
Prints one table per group (studied 6, untouched 6, all 12), one column per run, on the
cases every run has finished. Order is cheapest first: B's investigation makes the same calls
as system-v2, so it should replay from `.llm_cache` (only synthesis and judging are new); A
is the expensive one (Opus for every manager call). Don't pick a winner from this: confirm it
on 6 new train cases first: the next 6 in train order are p059, p065, p068, p073, p075, p081
(their real interviews were among the 31 used, in aggregate only, to check the interview
rules; no variant was built against them).

**Done 2026-10-16:** C won: 3.51 overall (untouched 6: 3.46) vs baseline 3.34 (3.37); A
(Opus brain) 3.23, B (single-brain synthesis) 2.98.

**Confirmation of C on the next 6 train cases (p059, p065, p068, p073, p075, p081).** No code
change. Three runs: C with the current models, plain Sonnet baseline, and C with Opus as the
agent (`models.claude-opus-agent.yaml`: only the `baseline` role moves to Opus; simulator and
judge unchanged). `--n 18` takes the first 18 train cases; the runner skips any case whose
result file exists, so the first 12 are seeded by copying: C's and the baseline's real
12-case results (giving 18-case pools), and C's 12 as skip markers for the Opus run, deleted
after it so that run holds only the new 6. One line in a plain PowerShell terminal,
resumable (rerun the same line after any stop):
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; foreach ($r in "c-train18","baseline-train18","c-opus-new6") { New-Item -ItemType Directory -Force eval_results\$r | Out-Null }; Copy-Item eval_results\ablation-c-baseplus\*.json eval_results\c-train18\; Copy-Item eval_results\baseline-train12\*.json eval_results\baseline-train18\; Copy-Item eval_results\ablation-c-baseplus\*.json eval_results\c-opus-new6\; uv run consult eval baseline --split train --n 18 --name c-train18 --plus; uv run consult eval baseline --split train --n 18 --name baseline-train18; $env:CONSULTANT_MODELS_FILE="models.claude-opus-agent.yaml"; uv run consult eval baseline --split train --n 18 --name c-opus-new6 --plus; Get-ChildItem eval_results\ablation-c-baseplus\*.json | ForEach-Object { Remove-Item "eval_results\c-opus-new6\$($_.Name)" }; uv run python -m consultant.evals.report --compare c-train18 baseline-train18 c-opus-new6; uv run python -m consultant.evals.report --system c-train18 --baseline baseline-train18 --split
```
Output: `--compare` runs on the cases all three have, i.e. the new 6 (its "Studied 6" table
is empty, "Untouched" and "All" are the new 6). `--split` pools C vs baseline: studied 6,
untouched 12, and **Combined = all 18**. The summary file `eval_results/c-opus-new6.json` is
written before the markers are deleted, so its averages include C's 12; use the tables, not
that file. Expected time (from C's 12-case run: ~24 min of agent and judge calls): C ~12 min,
baseline ~8 min, C-Opus ~15-25 min, about 45 min in all; a usage-limit wait resumes itself.
Pass: C beats the baseline on the new 6 and on the pooled 18. If C-Opus beats C by a clear
margin, the rebuild uses Opus for the agent.

**Done 2026-10-16:** new 6: C 3.28, baseline 3.15, C with Opus 3.52. Pooled 18: C 3.43 vs
baseline 3.28 (untouched 12: 3.37 vs 3.26). The rebuild uses Opus for the agent.

**Single agent (PLAN.md 5b, branch `single-agent`).** `consult eval system` now runs the agent
(`consultant/agent.py`); the commands above that ran the old pipeline need `--legacy` to
reproduce. Run the gates in this order, each line in a plain PowerShell terminal; each is
resumable (rerun the same line after any stop; finished cases are skipped).

*Gate 1: no regression from wrapping C into the product (18 train cases).* C with Opus has only
the new 6, so it first runs on the first 12 (seeded with its 6), then the agent on all 18:
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude-opus-agent.yaml"; New-Item -ItemType Directory -Force eval_results\c-opus-train18 | Out-Null; Copy-Item eval_results\c-opus-new6\*.json eval_results\c-opus-train18\; uv run consult eval baseline --split train --n 18 --name c-opus-train18 --plus; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; uv run consult eval system --split train --n 18 --name agent-train18; uv run python -m consultant.evals.report --compare c-opus-train18 agent-train18 c-train18 baseline-train18
```
Pass: `agent-train18` overall >= `c-opus-train18` overall in the "All (n = 18)" table, and
on the untouched 12. Expected: C-Opus 12 cases ~40-50 min; the agent ~3-5 min a case
(Opus turns, research via Sonnet + Haiku, write-up and repair), ~1.5 h for 18. Allow two
5-hour windows; a usage-limit wait resumes itself.

*Gate 2: real mode, 2 smoke runs on public-company questions.* Same models file:
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; uv run consult new "Why did Asian Paints' consolidated EBITDA margin change between FY2023-24 and FY2024-25, and what should management do about it?" --name real-smoke-asianpaints; uv run consult new "Should Eternal (Zomato) keep expanding Blinkit's dark-store network, given its FY2024-25 results?" --name real-smoke-eternal; foreach ($e in "real-smoke-asianpaints","real-smoke-eternal") { Get-ChildItem engagements\$e\deliverables | Select-Object Name; Get-Content engagements\$e\deliverables\validation.json }
```
If a run stops, continue it with `uv run consult resume <name>` (not `new` again). Pass: all
seven files in `deliverables/` (memo.md, storyline.md/.json, model.xlsx, sources.md,
assumptions.md, validation.json), `passed: true`, and every number in memo.md traceable to a
row in sources.md, model.xlsx or assumptions.md. Read the memo's "Figures removed as
unverified" list: a long list means the write-up isn't citing its evidence. ~10-20 min each.

*Gate 3: partner on vs off (the 6 new train cases).* Run after gate 1 (it compares with
`agent-train18`). The investigation and first draft make the same calls as gate 1, so they
replay from the cache; only the reviews, revision and repair are new:
```
cd D:\Projects\consultantmaxxing; $env:CONSULTANT_MODELS_FILE="models.claude.yaml"; uv run consult eval system --split train --n 18 --skip 12 --name agent-partner-new6 --profile agent-partner; uv run python -m consultant.evals.report --compare agent-train18 agent-partner-new6
```
The `--compare` tables cover the cases both runs have (the new 6). Keep the flag only if
partner-on beats partner-off overall without losing key insight or recommendation; otherwise
delete it. ~20-30 min.

## 5. Phase 6: deliverables
Gate ran offline (validator, live formulas, action titles). Deferred live check:
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 6.1 | Deliverables from real engagements validate | after 5.1/5.3: inspect `engagements/<name>/*/deliverables/validation.json` | `passed: true` for every engagement; any flags reviewed by hand |

## 6. Phase 7: full evaluation (run last, in this order)
| # | Step | Command | Notes |
|---|---|---|---|
| 7.1 | Verify backtest outcomes | edit `consultant/evals/backtests.yaml`: check each `outcome` against a primary source, set `verified: true` | the runner skips unverified ones |
| 7.2 | Iterate on a 5-case **train** subset | `uv run python -m consultant.evals.run_system cases --split train --n 5 --name system-dev` | debug the weakest layer here, never on the test split |
| 7.3 | Baseline on the 15 test cases | `uv run python -m consultant.evals.baseline` | same model as the engagement manager (see 1.3) |
| 7.4 | Full system on the 15 test cases (once) | `uv run python -m consultant.evals.run_system cases --split test --name system-test --profile dev` | `--profile full` for the final run if budget allows |
| 7.5 | Backtests | `uv run python -m consultant.evals.backtests` | 3 real-mode runs |
| 7.6 | Report + gate | `uv run python -m consultant.evals.report` | writes EVAL_REPORT.md; gate: beat baseline on aggregate and ≥6/9 dimensions |

If 7.6 fails: fix the weakest layer using 7.2 (train cases), re-run 7.4 at most 3 times, then
escalate. Cost and time: see the token estimate in PROGRESS.md (Phase 5).

## 7. Phase 8: interface + hardening
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 8.1 | Fresh clone → `consult new` completes a dev-profile engagement following the README | clone, `uv sync`, `cp .env.example .env` (add key), then `uv run consult new "<question>" --auto --profile dev` | deliverables written, `validation.json` passed |
| 8.2 | UI renders a live run | `uv run consult ui` while 8.1 runs | tree/hypotheses/facts update live (AppTest version passes offline) |

Partial on 2026-10-13: run 1 reached the partner review (bugs found and fixed); run 2 hit
the daily quota. Finish it with
`cd D:/Projects/consultantmaxxing-smoke && CONSULTANT_MODELS_FILE=models.small.yaml uv run consult resume smoke-asianpaints-2`.
