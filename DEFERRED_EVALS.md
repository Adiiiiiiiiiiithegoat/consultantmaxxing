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
| 1.2 | Judge discrimination | `uv run pytest -m live tests/test_judge.py -s` | strong beats flawed by ≥1.0 on ≥4/5 (passed 5/5 on Groq, 2026-10-13) |
| 1.3 | Baseline on the 15 test cases | `uv run python -m consultant.evals.baseline` | all 15 in `eval_results/baseline/`, summary in `eval_results/baseline.json` |

Notes for 1.3: the pinned worktree `D:/Projects/consultantmaxxing-baseline` (commit
`b557bfe`) holds a partial Groq run (0/15 finished; cached calls replay free). If roles
change to a paid model, rerun from scratch in the main checkout instead: the baseline must
use the same model as the system's engagement manager, and each result records
`code_version`. Rerun 1.1 and 1.2 too if the interviewer/judge model changes.

## 2. Phase 2: research layer
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 2.1 | Fact-finding accuracy (20 gate questions) | `uv run python -m consultant.evals.fact_finding` | accuracy ≥ 70% within tolerance |
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
| 5.2 | Partner catches seeded flaws | `uv run python -m consultant.evals.partner_eval` | ≥4/5 caught (strict: named kind, major/critical); clean draft approved |
| 5.3 | One real-mode run on a simple public-company question | `uv run python -m consultant.evals.run_system real "Why did Asian Paints' margins change in FY24?"` | completes; deliverables written; facts verified |
| 5.4 | Token actuals | `uv run python -m consultant.evals.token_estimate --traces eval-system-train` | replace the PROGRESS.md estimate |

Use a 5-case **train** subset (`--n 5`) for iteration; never iterate on the test split.

## 5. Phase 6: deliverables
Gate ran offline (validator, live formulas, action titles). Deferred live check:
| # | Check | Command | Pass criterion |
|---|---|---|---|
| 6.1 | Deliverables from real engagements validate | after 5.1/5.3: inspect `engagements/<name>/*/deliverables/validation.json` | `passed: true` for every engagement; any flags reviewed by hand |
