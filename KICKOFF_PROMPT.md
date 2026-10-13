# Kickoff prompt for Claude Code

Paste the block below into Claude Code, started in this folder. Use plan mode for
the first turn if you want to review the approach before any code is written.

---

You're building **consultantmaxxing**, an AI strategy/management consultant system.
Read `CLAUDE.md`, `PLAN.md` and `PROGRESS.md` in full before doing anything else.

Build the complete system described in PLAN.md, phase by phase, following the working
loop in CLAUDE.md: implement a phase, run its gate, and only move on when the gate
passes. Update PROGRESS.md and commit at the end of each phase. If a gate still fails
after 3 attempts, stop, record the blocker in PROGRESS.md and ask me.

Key constraints:
- The only model access right now is the Groq free tier (key in `.env`) and local
  Ollama. Design everything model-agnostic so Claude can be swapped in later through
  `config/models.yaml`. Rate limits are tight, so the limiter, cache and context
  management in Phase 0 matter.
- This repo is public. The PDFs in `data/private/` and everything derived from them
  must never be committed. Check `git status` before every commit.
- Don't weaken a test or gate to get it to pass, and never tune on the test split.

Start with Phase 0. Before writing code, check Groq's current model list and my
account's limits page, and pick the default per-role models in `config/models.yaml`
accordingly. Tell me which you chose and why.
