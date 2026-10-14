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
