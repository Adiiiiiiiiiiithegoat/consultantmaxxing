"""The consultant: one tool-using agent (ablation C, baseline+, made the product).

Each turn the agent takes one action:
  ask       (case mode only) one message to the interviewer; a reply with data becomes a
            client fact F#
  analyse   a Python script run in the sandbox; it becomes analysis A# (model.xlsx); estimates
            it uses are registered as assumptions S# (assumptions page)
  research  one web research question: the research associate searches, reads and stores
            every fact with URL + quote; the ids come back as F#
  finish    the final answer, in prose
Then the same agent writes that answer up as a cited draft, an optional partner review picks
the better of two drafts (profile `partner: true`), the repair pass removes any number not in
the evidence it cites, and the deliverables are rendered. Real mode: the same loop with no
interviewer. Caps come from the budget profile (budgets.yaml: `agent`, `agent-real`).
"""

import json
from typing import Literal

from pydantic import BaseModel, Field

from consultant import deliverables
from consultant.agents import partner, storyliner
from consultant.agents.analyst import TEMPLATES_DOC, preamble
from consultant.agents.engagement_manager import best_reviewed, evidence_text, fallback_draft
from consultant.agents.research_associate import ResearchAssociate
from consultant.config import profile as load_profile
from consultant.deliverables import repair
from consultant.evals.simulator import NOT_AVAILABLE
from consultant.knowledge import playbooks
from consultant.llm.context import clip
from consultant.research.fact_store import Assumption, Fact
from consultant.research.verify import verify_facts
from consultant.tracing import Budget, BudgetExceeded
from consultant.workspace import Workspace

LOOP_SHARE = 0.7          # of max_tokens; the write-up gets the rest plus what the loop leaves
RESEARCH_CAPS = {"searches": 3, "pages": 6, "tokens": 30000}  # per research action
DEFAULTS = {"max_questions": 8, "max_analyses": 4, "max_research": 3}  # C's caps

_ACTIONS = {
    "ask": ("'ask': say something to the interviewer, i.e. one question to clarify the problem "
            "or request data; if the interviewer asked you something, reply briefly here and "
            "continue."),
    "analyse": ("'analyse': content is a short Python script that assigns a dict of named "
                "results to RESULT (first line: a # comment naming what it computes); it runs "
                "in a sandbox (standard library and the templates below, no network or files) "
                "and you see RESULT next turn; the interviewer "
                "never sees it. Do every calculation this way, never in your head. List any "
                "estimate the script uses that no reply or source gave under 'assumptions' "
                "(name, value, unit, rationale): each becomes an S# you can read as "
                "ASSUMPTIONS['S#']; FACTS[#] holds the value of each fact F#."),
    "research": ("'research': content is one self-contained web research question about ONE "
                 "figure or fact: one company or market, one metric, one period (e.g. 'Acme "
                 "Ltd consolidated EBITDA margin FY2024-25'); ask several questions for "
                 "several figures. A research associate searches the web and returns sourced "
                 "facts, each with an id F#."),
    "finish": ("'finish': END the engagement with your answer to the original problem: the "
               "recommendation first, then the supporting analysis and next steps."),
}
_EVIDENCE = ("Every client reply with data, research fact, analysis result and estimate has an "
             "id (F#, A#, S#). Your final answer is written up citing these ids: never state a "
             "number that isn't in one of them.")


def system_prompt(mode: str) -> str:
    if mode == "case":
        head = "You are a management consultant solving a case interview."
        actions = ["ask", "analyse", "research", "finish"]
        tail = (" Research is for public context the client can't give (market size, "
                "benchmarks, competitors); the client's own data comes from the interviewer.")
    else:
        head = ("You are a management consultant answering a client's question. There is no "
                "interviewer: research every fact you need yourself.")
        actions = ["research", "analyse", "finish"]
        tail = ""
    return (f"{head} Each turn is one of {len(actions)} actions. "
            + " ".join(_ACTIONS[a] for a in actions) + tail + f" {_EVIDENCE} You have a "
            "limited number of " + ("questions, " if mode == "case" else "")
            + "analyses and research questions.\n\n" + TEMPLATES_DOC
            + "\n\nPLAYBOOKS (the firm's methods):\n\n"
            + "\n\n".join(playbooks.load(f.stem) for f in sorted(playbooks.SKILLS.glob("*.md"))))


class NewAssumption(BaseModel):
    name: str
    value: float
    unit: str = ""
    rationale: str = Field(description="why this value: the basis for the estimate")


class AgentTurn(BaseModel):
    action: Literal["ask", "analyse", "research", "finish"]
    content: str
    assumptions: list[NewAssumption] = Field(default_factory=list, description="analyse only: "
                                             "estimates the script uses (become S#)")


WRITE_UP = ("Write the final answer above up as the client deliverable. Keep its "
            "recommendation, key insight and reasoning; cite the evidence ids each claim and "
            "number comes from (only ids listed in EVIDENCE). Make the first argument the "
            "structure of the problem: the issue tree or framework you used, its branches, "
            "and which branches the evidence ruled in or out.")


class Agent:
    def __init__(self, llm, ws: Workspace, *, interviewer=None, web=None, role: str = "agent"):
        self.llm, self.ws, self.iv, self.web, self.role = llm, ws, interviewer, web, role
        self.prof = DEFAULTS | load_profile(ws.state.profile)
        self.mode = ws.state.mode
        if self.mode == "case" and interviewer is None:
            raise ValueError("case mode needs an interviewer (the simulated client)")
        cap = self.prof["max_tokens"]
        self.loop_budget = Budget({"tokens": int(cap * LOOP_SHARE),
                                   "calls": self.prof["max_calls"],
                                   "searches": self.prof["max_searches"],
                                   "pages": self.prof["max_pages"]})
        self.write_budget = Budget({"tokens": cap - int(cap * LOOP_SHARE)})
        st = ws.state
        st.notes.setdefault("engine", "agent")
        if self.mode == "case":
            st.problem_statement = interviewer.opening()
        # the conversation the agent sees: {"role", "content", "ids"}; saved every turn so a
        # killed run resumes mid-loop without repeating actions
        self.log: list[dict] = st.notes.setdefault(
            "agent_log", [{"role": "interviewer" if self.mode == "case" else "client",
                           "content": st.problem_statement, "ids": []}])

    # ---- the loop ------------------------------------------------------------------------
    def _count(self, role: str) -> int:
        return sum(e["role"] == role for e in self.log)

    def _left(self) -> dict[str, int]:
        return {"ask": (self.prof["max_questions"] - self._count("candidate")
                        if self.mode == "case" else 0),
                "analyse": self.prof["max_analyses"] - self._count("analysis"),
                "research": (self.prof["max_research"] - self._count("research")
                             if self.web is not None else 0)}

    def _convo(self) -> str:
        return "\n".join(f"{e['role'].upper()}"
                         + (f" [{', '.join(e['ids'])}]" if e["ids"] else "")
                         + f": {e['content']}" for e in self.log)

    def _note(self, left: dict[str, int]) -> str:
        names = {"ask": "Questions", "analyse": "Analyses", "research": "Research questions"}
        parts = [f"{names[k]} left: {v}." for k, v in left.items()
                 if k != "ask" or self.mode == "case"]
        done = not any(left.values())
        return " ".join(parts) + (" Give your final answer now (action=finish)." if done else "")

    def _add(self, role: str, content: str, ids: list[str] | None = None) -> None:
        self.log.append({"role": role, "content": content, "ids": ids or []})
        st = self.ws.state
        if self.iv is not None:
            st.notes["interview_log"] = self.iv.log
        self.ws.save()

    def _ask(self, message: str) -> None:
        reply = self.iv.ask(message)
        self._add("candidate", message)
        self._add("interviewer", reply, self._client_fact(reply))

    def _client_fact(self, reply: str) -> list[str]:
        """The reply, whole, is the claim and the quote (the validator reads its numbers);
        a reply already on file is the same fact."""
        if reply == NOT_AVAILABLE:
            return []
        eid = self.ws.state.engagement_id
        same = [f.id for f in self.ws.facts.facts(eid)
                if f.source_type == "client" and f.claim == reply]
        if same:
            return [f"F{same[0]}"]
        fid = self.ws.facts.add(Fact(
            engagement_id=eid, claim=reply, quote=reply, source_type="client", credibility=5,
            source_url=f"case://{self.iv.case.id}/interviewer", source_title="Client (case data)"))
        self.ws.facts.set_verified(fid, True, "client-provided case data")
        return [f"F{fid}"]

    def _analyse(self, code: str, estimates: list[NewAssumption]) -> None:
        eid = self.ws.state.engagement_id
        sids = [f"S{self.ws.facts.add_assumption(Assumption(engagement_id=eid, **a.model_dump()))}"
                for a in estimates]
        n = self._count("analysis") + 1
        self._add("analysis", code, sids)
        facts, assumptions = self.ws.facts.facts(eid), self.ws.facts.assumptions(eid)
        purpose = code.strip().splitlines()[0].lstrip("# ").strip() if code.strip() else ""
        rec = self.ws.run_analysis(purpose[:60] or f"analysis_{n}", purpose[:200],
                                   preamble(facts, assumptions) + code)
        out = (json.dumps(rec.result, default=str)[:2000] if rec.ok and rec.result else
               f"FAILED: {rec.error or 'the script did not set RESULT'}")
        self._add("result", out, [rec.id] if rec.ok and rec.result else [])

    def _research(self, question: str) -> None:
        eid = self.ws.state.engagement_id
        self._add("research", question)
        # case mode: no model-guessed estimate when the web finds nothing (the judge reads
        # them as unsourced figures); the agent registers its own estimates via analyse
        ra = ResearchAssociate(self.llm, self.web, self.ws.facts, eid, caps=RESEARCH_CAPS,
                               allow_estimate=self.mode != "case")
        res = ra.research(question, context=f"CLIENT PROBLEM: {self.ws.state.problem_statement}")
        for kind in ("tokens", "calls", "searches", "pages"):
            self.loop_budget.charge(kind, res.spent.get(kind, 0))
        b = res.brief
        lines = [f"{b.answer} (confidence: {b.confidence}; gaps: {b.gaps or 'none'})"]
        lines += [f"F{f.id}: {f.claim} | {'' if f.value is None else f'{f.value:g} {f.unit}'} "
                  f"| {f.period} | {f.source_title or f.source_url} ({f.source_date or 'undated'})"
                  for f in res.facts]
        ids = [f"F{f.id}" for f in res.facts]
        if est := res.estimate:
            lines.append(f"ESTIMATE (no source found): {est.value:g} {est.unit} = {est.formula}")
            ids += [f"S{i}" for i in est.assumption_ids]
        if not res.facts and est is None:
            self.ws.state.open_questions.append(question)
        self._add("findings", "\n".join(lines), ids)

    def investigate(self) -> str:
        """Run the loop; returns the agent's final answer in prose ('' if the budget ran out)."""
        st = self.ws.state
        if "agent_final" in st.notes:
            return st.notes["agent_final"]
        system = system_prompt(self.mode)
        final = ""
        with self.llm.scoped(self.loop_budget):
            try:
                while True:
                    left = self._left()
                    msgs = [{"role": "system", "content": system},
                            {"role": "user", "content": f"{clip(self._convo(), 12000)}\n\n"
                                                        f"{self._note(left)}"}]
                    turn = self.llm.structured(self.role, msgs, AgentTurn)
                    if turn.action == "finish" or not any(left.values()):
                        final = turn.content  # out of actions: whatever it says is the answer
                        break
                    if not left.get(turn.action):
                        why = ("there is no interviewer: research instead" if turn.action == "ask"
                               and self.mode != "case" else f"no {turn.action} left")
                        self._add("note", f"'{turn.action}' not available ({why}); choose "
                                          "another action or finish.")
                        if self._count("note") > 3:  # ponytail: stuck on a used-up action
                            break
                        continue
                    if turn.action == "ask":
                        self._ask(turn.content)
                    elif turn.action == "analyse":
                        self._analyse(turn.content, turn.assumptions)
                    else:
                        self._research(turn.content)
            except BudgetExceeded as e:
                self.ws.decide("system", "investigation stopped: budget cap", str(e))
                st.open_questions.append("Investigation stopped at the budget cap.")
        st.notes["agent_final"] = final
        self.ws.save()
        return final

    # ---- write-up, review, repair, deliver -----------------------------------------------
    def _brief(self, final: str) -> str:
        return (f"THE CLIENT'S PROBLEM, AS STATED: {self.ws.state.problem_statement}\n\n"
                f"YOUR WORK (the conversation, analyses and research, with evidence ids):\n"
                f"{self._convo()}\n\nEVIDENCE:\n{evidence_text(self.ws)}\n\n"
                f"YOUR FINAL ANSWER:\n{final or '(none: the budget ran out; answer from the work)'}"
                f"\n\n{WRITE_UP}")

    def _review(self, d: storyliner.Draft) -> dict:
        return partner.review(self.llm, self.ws.state.problem_statement, "",
                              storyliner.render_text(d),
                              evidence_text(self.ws, verified_only=True)).model_dump()

    def run(self) -> dict:
        ws, st = self.ws, self.ws.state
        final = self.investigate()
        stopped = next((d.rationale for d in st.decisions if "budget cap" in d.decision), None)
        if self.web is not None and any(f.source_type != "client"
                                        for f in ws.facts.facts(st.engagement_id)):
            ws.step("verify", lambda: verify_facts(ws.facts, self.web, st.engagement_id,
                                                   self.llm))
        self.write_budget.limits["tokens"] = max(
            self.write_budget.limits["tokens"],
            self.prof["max_tokens"] - self.loop_budget.spent["tokens"])
        brief = self._brief(final)
        draft = None
        with self.llm.scoped(self.write_budget):
            try:
                draft = storyliner.Draft(**ws.step("story0", lambda: storyliner.write(
                    self.llm, brief, role=self.role).model_dump()))
                if self.prof.get("partner"):  # one review, one revision, the better one ships
                    reviewed = []
                    rv = partner.Review(**ws.step("review0", lambda: self._review(draft)))
                    reviewed.append((draft, rv))
                    if rv.verdict != "approve":
                        d1 = storyliner.Draft(**ws.step("story1", lambda: storyliner.write(
                            self.llm, brief, partner.format_review(rv), role=self.role,
                            previous=draft).model_dump()))
                        reviewed.append((d1, partner.Review(**ws.step(
                            "review1", lambda: self._review(d1)))))
                    k = best_reviewed(reviewed)
                    draft, rv = reviewed[k]
                    st.notes["shipped_draft"] = f"story{k}"
                    if rv.verdict != "approve":
                        st.notes["unresolved_review"] = [i.model_dump() for i in rv.blocking()]
            except BudgetExceeded as e:
                stopped = stopped or str(e)
                ws.decide("system", "write-up stopped: budget cap", str(e))
        if draft is None:
            draft = fallback_draft(ws)
        with self.llm.scoped(self.write_budget):
            draft = storyliner.Draft(**ws.step("repair", lambda: self._repair(draft)))
        ws.step("deliver", lambda: deliverables.render(ws, draft))
        return {"stopped": stopped, "draft": draft, "deliverables": st.deliverables,
                "final": final,
                "tokens": self.loop_budget.spent["tokens"] + self.write_budget.spent["tokens"]}

    def _repair(self, d: storyliner.Draft) -> dict:
        """No flagged number ships as fact: re-cite or remove (deliverables.repair)."""
        d, removed = repair.repair(self.llm, self.ws, d,
                                   evidence_text(self.ws, verified_only=True), role=self.role)
        self.ws.state.notes["unverified_removed"] = removed
        if removed:
            self.ws.decide("system", "removed unverified figures", "; ".join(removed))
        return d.model_dump()

    def work(self) -> str:
        """What the judge sees of the process (as C's work record): each analysis with its
        result and each research question with its findings."""
        keep = ("analysis", "result", "research", "findings")
        return "\n\n".join(f"{e['role'].upper()}" + (f" [{', '.join(e['ids'])}]" if e["ids"]
                                                     else "") + f":\n{e['content']}"
                           for e in self.log if e["role"] in keep)
