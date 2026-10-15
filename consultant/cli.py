"""consult: command-line interface.

  consult new "Why did our margins fall?"      start a real-mode engagement (interactive)
  consult resume <engagement-id>               continue a stopped engagement
  consult list                                 engagements and their status
  consult eval system|baseline|facts|partner|backtests|retrieval|tokens
  consult report                               write EVAL_REPORT.md
  consult knowledge build                      extract casebook + build the retrieval index
  consult ui                                   Streamlit dashboard
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer

if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

app = typer.Typer(help="consultantmaxxing: an AI strategy consultant.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation runners (live model calls).", no_args_is_help=True)
knowledge_app = typer.Typer(help="Knowledge base from the private casebook.",
                            no_args_is_help=True)
app.add_typer(eval_app, name="eval")
app.add_typer(knowledge_app, name="knowledge")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def _checkpoint(stage: str, ws) -> str | None:
    st = ws.state
    typer.secho(f"\n--- checkpoint: {stage} ---", fg="cyan", bold=True)
    if stage == "scoping":
        typer.echo(f"Question: {st.scope.get('restated_question')}")
        typer.echo(f"Case type: {st.case_type} | objective: {st.scope.get('objective')}")
        for q in st.scope.get("clarifying_questions", []):
            typer.echo(f"  ? {q}")
        prompt = "Answer the questions above (Enter to skip; they become open questions)"
    else:
        for h in st.hypotheses:
            typer.echo(f"  {h.id} [p{h.priority}] {h.statement}")
        prompt = "Feedback on these hypotheses (Enter to accept)"
    return typer.prompt(prompt, default="", show_default=False) or None


def _run(ws, interactive: bool) -> None:
    from consultant.agents.engagement_manager import Engagement
    from consultant.llm import LLM, ModelBlocked, QuotaExhausted
    from consultant.research.web import Web
    from consultant.tracing import Tracer
    tracer = Tracer(run_id=f"engagement-{ws.state.engagement_id}")
    llm = LLM(tracer=tracer)
    try:
        out = Engagement(llm, ws, web=Web(tracer=tracer),
                         checkpoint=_checkpoint if interactive else None).run()
    except QuotaExhausted as e:
        typer.secho(f"\nStopped cleanly: model quota exhausted ({e}). Progress is saved.",
                    fg="yellow")
        typer.echo(f"Resume later with:  consult resume {ws.state.engagement_id}")
        raise typer.Exit(2) from e
    except ModelBlocked as e:
        typer.secho(f"\n{e}. Remove it from CONSULTANT_BLOCK_MODELS in .env or point the "
                    "role at another model (config/models.yaml or CONSULTANT_MODELS_FILE).",
                    fg="red")
        raise typer.Exit(2) from e
    d = ws.dir / "deliverables"
    report = json.loads((d / "validation.json").read_text(encoding="utf-8"))
    typer.secho(f"\nDone. Deliverables in {d}", fg="green", bold=True)
    for k, v in out["deliverables"].items():
        typer.echo(f"  {k:13} {ws.dir / v}")
    typer.echo(f"Validation: {'passed' if report['passed'] else 'FLAGS — see validation.json'}"
               + (f" | stopped early: {out['stopped']}" if out["stopped"] else ""))
    for model, u in tracer.usage().items():
        typer.echo(f"  {model}: {int(u['calls'])} calls, "
                   f"{int(u['prompt_tokens'] + u['completion_tokens']):,} tokens")


@app.command()
def new(problem: str = typer.Argument(..., help="the business problem, in plain words"),
        name: str = typer.Option(None, help="engagement id (default: from the problem)"),
        profile: str = typer.Option("dev", help="budget profile: dev or full"),
        interactive: bool = typer.Option(True, "--interactive/--auto",
                                         help="ask you at the checkpoints")):
    """Start a real-mode engagement: research the web and deliver a memo, model and sources."""
    from consultant.workspace import Workspace
    eid = name or f"{_slug(problem)}-{datetime.now():%Y%m%d-%H%M}"
    ws = Workspace.create(eid, mode="real", profile=profile, problem_statement=problem)
    typer.echo(f"Engagement {eid} ({profile} profile)")
    _run(ws, interactive)


@app.command()
def resume(engagement_id: str,
           interactive: bool = typer.Option(False, "--interactive/--auto")):
    """Continue a stopped engagement from its last completed step."""
    from consultant.workspace import Workspace
    ws = Workspace.load(engagement_id)
    typer.echo(f"Resuming {engagement_id}; done: {', '.join(ws.state.steps_done) or 'nothing'}")
    _run(ws, interactive)


@app.command("list")
def list_engagements():
    """Engagements on this machine and their progress."""
    from consultant.workspace import ENGAGEMENTS
    for p in sorted(ENGAGEMENTS.glob("*/workspace.json")):
        st = json.loads(p.read_text(encoding="utf-8"))
        status = "delivered" if "deliver" in st["steps_done"] else \
            (st["steps_done"][-1] if st["steps_done"] else "new")
        typer.echo(f"{st['engagement_id']:50} {st['mode']:5} {status}")


@app.command()
def report(system: str = "system-test", baseline: str = "baseline"):
    """Write EVAL_REPORT.md from eval_results/."""
    from consultant.evals import report as rep
    rep.main(["--system", system, "--baseline", baseline])


@app.command()
def compare(problem: str = typer.Argument(..., help="markdown file with the client problem"),
            human_answer: str = typer.Argument(..., help="markdown file with a human answer"),
            name: str = typer.Option(None, help="output folder under engagements/"),
            profile: str = typer.Option("claude", help="budget profile for the system run")):
    """Run the system on a problem, blind both answers as A/B and judge each separately.
    Set CONSULTANT_MODELS_FILE (e.g. models.claude.yaml) first."""
    from consultant.evals import compare as cmp
    name = name or f"compare-{_slug(Path(problem).stem)}-{datetime.now():%Y%m%d-%H%M}"
    res = cmp.compare(Path(problem), Path(human_answer), name, profile)
    typer.echo(f"Blinded answers and scores in {res['dir']} (key/mapping.json reveals A/B)")
    for who in ("system", "human"):
        typer.echo(f"  {who:6} answer {res[who]['label']}: {res[who]['total']:.2f} "
                   f"{res[who]['scores']}")


@app.command()
def ui(port: int = 8501):
    """Open the Streamlit dashboard."""
    app_path = Path(__file__).parent / "ui" / "app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(app_path),
                    "--server.port", str(port)], check=False)


# ---- evals ----------------------------------------------------------------------------
@eval_app.command("system")
def eval_system(split: str = "train", n: int = typer.Option(None), name: str = None,
                profile: str = "dev"):
    """Full system in case mode on a split, scored by the judge."""
    from consultant.evals import run_system
    run_system.main(["cases", "--split", split, "--profile", profile]
                    + (["--n", str(n)] if n else []) + (["--name", name] if name else []))


@eval_app.command("baseline")
def eval_baseline(n: int = typer.Option(None), split: str = "test", name: str = "baseline",
                  plus: bool = typer.Option(False, help="baseline+ (ablation C): playbooks "
                                            "in the prompt and the analysis sandbox")):
    """Single-model baseline (default: the test split)."""
    from consultant.evals import baseline
    typer.echo(json.dumps(baseline.run(split, limit=n, name=name, plus=plus), indent=1,
                          default=str))


@eval_app.command("facts")
def eval_facts(dev: bool = False, n: int = typer.Option(None)):
    """Fact-finding accuracy and citation verification (20 gate questions; --dev: 3)."""
    from consultant.evals import fact_finding
    typer.echo(json.dumps(fact_finding.run(n, dev=dev), indent=1, default=str))


@eval_app.command("partner")
def eval_partner():
    """Does the partner catch the seeded flaws?"""
    from consultant.evals import partner_eval
    typer.echo(json.dumps({k: v for k, v in partner_eval.run().items() if k != "rows"}))


@eval_app.command("backtests")
def eval_backtests(include_unverified: bool = False):
    """Real-mode backtests answered as of a past date."""
    from consultant.evals import backtests
    typer.echo(json.dumps(backtests.run(include_unverified), indent=1))


@eval_app.command("retrieval")
def eval_retrieval():
    """Retrieval hit-rate on the hand-written queries (offline)."""
    from consultant.evals import retrieval_eval
    typer.echo(json.dumps(retrieval_eval.run(), indent=1))


@eval_app.command("tokens")
def eval_tokens(profile: str = "dev", traces: str = None):
    """Token estimate per engagement and Phase 7 duration (or actuals from a trace run)."""
    from consultant.evals import token_estimate as te
    if traces:
        typer.echo(json.dumps(te.from_traces(traces), indent=1))
        return
    for mode in ("case", "real"):
        e = te.estimate(profile, mode)
        typer.echo(f"{mode}: {e['total']:,} tokens {e['per_model']}")
    for n in (5, 15):
        typer.echo(json.dumps(te.phase7_days(n_cases=n, profile_name=profile)))


# ---- knowledge ------------------------------------------------------------------------
@knowledge_app.command("build")
def knowledge_build():
    """Extract cases/primers from data/private/ (LLM calls) and build the retrieval index."""
    from consultant.knowledge import retrieval
    from consultant.knowledge.casebook import CASEBOOK
    if not CASEBOOK.exists():
        typer.secho(f"Missing {CASEBOOK}: the private casebook isn't on this machine. The "
                    "system still runs (no case library); case-mode evals need it.", fg="yellow")
        raise typer.Exit(1)
    subprocess.run([sys.executable, "-m", "consultant.knowledge.extract", "all"], check=True)
    typer.echo(retrieval.build())


if __name__ == "__main__":
    app()
