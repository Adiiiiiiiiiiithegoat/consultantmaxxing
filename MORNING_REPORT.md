# Morning report: overnight run 2026-10-14

Branch: `overnight-fixes` (not pushed). Models: step 1 uses `models.small.yaml` (as the
original resume command says); gpt-oss-120b stays blocked.

## Step status
| # | Step | Status |
|---|---|---|
| 1 | Resume smoke-asianpaints-2 to complete deliverables | **PASS** (2nd attempt): all 7 deliverables written. `validation.json` still `passed: false` with 2 genuine flags (see known issues) |
| 2 | `consult eval system --split train --n 3` | **PASS** (run 5, commit 1bbe790): 3/3 done, `within_budget` 3/3, all 7 artefacts in each workspace, one code version. Judge overall 1.41/5 (p012 1.22, p017 1.11, p025 1.89): low, see known issues. Validation passed on 2/3 |
| 3 | `consult eval facts` | **PASS** (run 3, commit 7e9f42c): accuracy 75% (15/20, gate ≥70%), verifier_rate 0.993 (≥0.90), eligible_without_source 0. Still wrong: TCS (quarterly), Tata Motors (FY23 column), HDFC (balance-sheet total), Jio (214 vs 181.7), crude steel (see known issues) |
| 4 | `consult eval partner` | **PASS** on the eval's own gate (1st attempt): 5/5 seeded flaws caught (≥4). Clean draft got `revise`, not `approve`: the fixture's clean draft is internally inconsistent (see unsolved). No code changed |

## Fixes (commit, layer, what changed)
| Commit | Layer | Issue → fix |
|---|---|---|
| fc7abf1 | research / estimation | Resume crashed: the estimator's LLM formula was an assignment (`margin_fy22 = margin_fy23 - improvement_fy24`), `ast.parse` threw SyntaxError and killed the whole engagement. Now the formula is validated inside `EstimatePlan` (bad formula → validation error → the model retries with the message), `x = expr` is accepted, unknown names are rejected, and an estimate that still fails is skipped instead of crashing. Test: `test_estimate_formula_assignment_and_bad_formula_is_a_validation_error`. Also added `config/models.overnight.yaml` (models.yaml with 120b roles on 20b) for steps 2-4. |
| 64ae3b5 | deliverables / validator | False positives: fiscal-year labels (`FY22‑23`, `FY24‑25`) read the tail as a data number, and `(1) (2) (3)` lists weren't recognised as enumeration. Both now skipped; real data numbers are still checked (test extended in `test_enumeration_only_skips_counting_sequences`). Smoke flags went 7 → 3. |
| 78afe96 | llm client / structured output | Step 2, case p025 died at scoping: gpt-oss-20b hit Groq `json_validate_failed` twice, then a third reply dropped `case_type/industry/entity`. The retry loop threw away Groq's `failed_generation`, told the model only "(invalid or empty JSON)" and asked it to "keep it brief". Now it parses `failed_generation` itself (often valid JSON wrapped in text), otherwise shows it back with the real error, and asks for "every required field". Re-running the same call by hand succeeded, so the failure is intermittent. Test: `test_json_validate_failed_salvages_or_shows_the_failed_generation`. |
| e2b321c | config (overnight only) | Step 2 run 1 completed 3/3 but `within_budget` = 0/3: every case hit the dev synthesis cap (30% of 60K = 18K) during draft → review → redraft. Storyliner drafts used 1.6–2.9K completion tokens each because the overnight config inherited `reasoning_effort: medium` from the 120b roles; one draft also padded whitespace up to `max_tokens` (a known gpt-oss JSON-mode failure) and 2/3 cases needed a retry for omitted Draft fields. Set the 20b writer roles to `low`, as in the tested small-model plan. Budgets and gates unchanged. Run 1 kept in `eval_results/system-train-a1*` and `engagements/system-train-a1`. |
| 8d906c1 | llm client + orchestration | Step 2 run 2 crashed: the qwen analyst's code-in-JSON hit its 1000-token output cap, the retry was identical (and since 78afe96 no longer said "brief"), so it truncated 3 times → `StructuredOutputError` killed the eval. Now a retry after `finish_reason=length` tells the model it was cut off and must be much shorter, and a failed analysis is logged in the decision log instead of crashing the engagement. Tests: `test_truncated_structured_reply_asks_for_a_shorter_one`, `test_failed_analysis_does_not_crash_the_engagement`. |
| ff5fb26 | llm client / structured output | Step 2 run 3: p025 Scope failed because the model returned 10, then 8, then 8 clarifying questions (schema max 5) and ignored the error each time. Lists over `max_length` are now trimmed to the first N during validation (no extra call); any other validation error still goes back to the model. Test: `test_over_long_lists_are_trimmed_not_retried`. |
| 1bbe790 | orchestration / budgets | Step 2 run 4: 3/3 done, `within_budget` 1/3. p012/p017 stopped in synthesis at 37–48K tokens, well under the 60K run cap: investigation used only 23–34K of its fixed 70% share (42K), but synthesis was capped at its fixed 30% (18K), so the unused investigation tokens were lost. Synthesis now keeps its 30% reserve **plus** whatever investigation left unspent. The run cap (`budgets.yaml`) and the gate are unchanged. Test: `test_synthesis_gets_the_reserve_plus_unspent_investigation_tokens`. Runs 2–4 kept in `eval_results/system-train-a2*`. Re-ran all 3 cases from scratch so the results come from one code version. |
| 3d8bbff | research / extraction | Step 3 crashed at question 6: the local qwen3:4b extractor looped copying runs of empty table cells (`|  |  |  | …`) from a scraped page until max_tokens, three times, and the `StructuredOutputError` killed the eval. Runs of empty cells are now collapsed in the extractor's input and in quote matching (so quotes still verify), and an unreadable chunk is skipped. Test: `test_extractor_sees_collapsed_empty_cells_and_survives_a_bad_chunk`. |
| 0543742 | research / brief | Step 3 run 1: 20/20 done, accuracy 60% (gate 70%); verifier 99%, unsourced 0. Misses by class: wrong period (TCS quarterly ₹61,237 cr reported as the year; Tata Motors FY23 column), wrong metric (HDFC ₹24,680 bn balance-sheet total labelled net profit, picked over the ₹608.1 bn PAT on the same page), source choice (Jio: two news sites over ril.com), unverified table column (Infosys), search coverage (UPI, crude steel, Infosys revenue). Root cause for the first three: the brief model saw only the extractor's paraphrase, never the quote or page title. `facts_table` rows now include both. Test: `test_facts_table_shows_quote_and_source_title`. |
| e218eec | research / extraction | Infosys PAT and HCL (dev): stockezee rows repeat every value (`Mar 2024 | Apr 2024` columns), which blocked per-year expansion, and a row with no verified header had its last (FY26) cell labelled FY2023-24. Duplicated year columns now expand when their values agree (and are still refused when they differ, e.g. quarter vs year). A multi-value row with no verified header keeps no period, so it can't be reported as the asked year. Test: `test_duplicated_year_columns_and_headerless_rows`. |
| 7e9f42c | research / verifier | Step 3 run 2: accuracy 75% (pass), but `verifier_rate` fell to 0.82 (gate 0.90). 21 of the 23 failed facts were L&T facts whose source page timed out (`ReadTimeout`) on the verifier's single fresh re-fetch; the same facts verified 23/23 in run 1. Timeouts, connection errors, 5xx and 429 are now retried (3 tries, 5s/10s pauses); 403/404/robots still fail at once. The metric and threshold are unchanged. Test: `test_verifier_retries_a_timed_out_refetch_but_not_a_404`. Gate re-run fresh on the final code (LLM calls replay from cache). |

## Known issues (not blocking a step)
- **Smoke deliverables: 2 genuine validator flags.** The final draft says "≈43.6 % in Q4"
  (in no evidence: made up) and cites 42.39 % to F5/F24 when it comes from analysis A2
  (wrong citation). The memo shows both under "Validation flags", which is how it's meant to
  work. Root cause: the dev token budget (42K) ran out during synthesis
  (`tokens budget: 37775+4418 > 42000`), so the draft that validation would have sent back
  for revision was delivered as-is. Options: a bigger synthesis reserve on `dev`, or a
  cheaper revision pass for validator-only flags. That's a budget design decision, so I
  haven't made it.
- **Judge scores are low (≈1–2 of 5) on all train cases.** The judge's notes say "no explicit
  hypotheses", "no calculations", "missed the key insight". Part of it is real quality
  (gpt-oss-20b, dev profile, the insight isn't found). Part may be an **eval design issue,
  logged and not changed**: `run_system.submission()` gives the judge only the interview
  transcript and the final answer, never the issue tree, hypotheses or analyses, so
  structure/hypotheses/maths are scored on what shows through the client questions. Worth
  deciding whether the submission should include the tree/hypotheses/analysis results
  (e.g. p025 scored maths 5 only because a number showed up in the answer).
- **Validator flags recommendation timeframes** (p017: "target 3‑month horizon", "within 3
  months") as unsupported numbers. They're advice, not facts. Whether recommendation
  parameters must be sourced is a policy call, so I left the validator strict.
- **Fact-finding margin is thin (15/20 vs 14 needed).** A run is not fully deterministic
  (search results and pages change), so a re-run can land at 70% or below. Crude steel found
  the right number (144.3 million tonnes) but was scored as 144.3: the brief's unit didn't
  convert to base, which looks like a unit-normalisation gap in the brief (`to_base`), not the
  eval. Not fixed, to avoid tuning further on the gate questions.
- **Gate-question exposure:** the fact-finding fixes were motivated by failures seen on the
  20 gate questions (I checked them on the 3 dev questions first, but dev stayed 0/3 because
  its misses are source-data/definition problems). The fixes are general (show quotes to the
  brief, table-column handling, verifier retry), but treat 75% as slightly optimistic.
- **Budgets don't persist across resumes**: a resumed engagement starts with fresh
  investigation counters, so an engagement resumed after a quota stop can spend more than the
  profile cap in total. Not hit in a gate tonight; not fixed.

## Unsolved issues (eval problems logged for you, not changed)
- **`partner_seeded.yaml`, clean case: the "clean" draft contradicts its own evidence.**
  F1 says profits fell 20%, but F2/F3 give revenue flat at Rs 24 cr and costs Rs 19→22 cr,
  i.e. profit 5→2 (−60%). The partner (qwen3.8-27b) flagged exactly this as a major maths
  issue, plus the ignored bread −5%, and returned `revise`. That's correct partner behaviour.
  `partner_eval.run()` sets `pass` from the 5 flawed drafts only, so the eval passes, but
  DEFERRED_EVALS 5.2's "clean draft approved" can't be met until the fixture is consistent
  (e.g. make F1 "profits fell 60%", or costs 19→20). I didn't edit the fixture (eval answer
  key, your rule).

## What to do next
1. Review the branch: `git log main..overnight-fixes` (11 commits; every code fix has a regression test). Merge if happy.
2. Decide the three open points now in PLAN.md: the partner fixture, dev synthesis
   budget/revision for validator flags, and what the judge sees.
3. Judge scores on train cases are about 1.4/5. Before Phase 7, iterate on the 5-case train
   subset (`consult eval system --split train --n 5 --name system-dev`) with
   `CONSULTANT_MODELS_FILE=models.overnight.yaml`. The weak dimensions are hypotheses,
   key insight and maths; the low score is mostly the 20b model plus the judge's limited view.
4. Fact-finding passes with one question to spare. Remaining misses are period/metric
   picks from scraped tables and a unit-normalisation gap (crude steel). Iterate on the dev
   set before touching the gate again.
5. Model plan used tonight: `config/models.overnight.yaml` (all 120b roles on 20b with
   `reasoning_effort: low`; partner/judge on qwen3.8-27b). gpt-oss-120b stayed blocked.

## Where things are
- Results: `eval_results/system-train*`, `eval_results/fact_finding*`,
  `eval_results/partner_seeded.json`. Earlier attempts are kept with `-a1/-a2/-old` suffixes.
- Smoke deliverables: `D:/Projects/consultantmaxxing-smoke/engagements/smoke-asianpaints-2/deliverables/`
  (that clone is on `overnight-fixes` too).
- Offline suite: 109 passed (`uv run pytest`), ruff clean.

## What to do next
(filled in at the end)
