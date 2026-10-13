"""Engagement Manager: the orchestrator.

scope -> classify -> playbook + similar cases -> issue tree + ranked hypotheses -> [gather
evidence -> assess hypotheses -> run analyses] x N -> (real mode: verify citations) ->
storyline -> partner review (max 3 rounds) -> deliver.

Case mode: evidence comes from the interviewer simulator (the 'client'). Real mode: from
Research Associates on the open web. Every stage is a workspace step, so a killed run
resumes where it stopped. Budget: investigation gets 70% of the profile's tokens, synthesis
the rest, so a capped run still delivers (with open questions).
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from consultant import deliverables
from consultant.agents import analyst, partner, storyliner
from consultant.agents.research_associate import ResearchAssociate
from consultant.config import profile as load_profile
from consultant.deliverables.validate import validate_draft
from consultant.knowledge import playbooks
from consultant.knowledge.retrieval import Retriever, add_primer
from consultant.knowledge.schema import CaseType
from consultant.research.extract import extract_facts
from consultant.research.fact_store import Fact
from consultant.research.verify import verify_facts
from consultant.research.web import Page
from consultant.tracing import Budget, BudgetExceeded
from consultant.workspace import Hypothesis, IssueNode, Workspace

MAX_REVIEW_ROUNDS = 3
INVESTIGATE_SHARE = 0.7


# ---- LLM output schemas -------------------------------------------------------------------
class Scope(BaseModel):
    restated_question: str = Field(description="the one decision/question to answer")
    objective: str
    success_metric: str
    horizon: str
    constraints: list[str]
    client_prior: str = Field(description="what the client already believes, if stated")
    clarifying_questions: list[str] = Field(max_length=5)
    case_type: CaseType
    industry: str
    entity: str = Field(description="client/company name if known, else a description")


class TreeNode(BaseModel):
    id: str
    text: str
    parent: str | None = None


class HypPlan(BaseModel):
    id: str = Field(description="H1, H2, ...")
    statement: str = Field(description="a falsifiable hypothesis")
    branch_id: str
    priority: int = Field(ge=1, le=3, description="1 = most likely/impactful")
    test: str = Field(description="the analysis or data that would prove/kill it")
    questions: list[str] = Field(max_length=4, description="data questions to ask/research")


class TreePlan(BaseModel):
    nodes: list[TreeNode] = Field(min_length=2)
    hypotheses: list[HypPlan] = Field(min_length=1, max_length=8)


class HypUpdate(BaseModel):
    id: str
    status: Literal["open", "supported", "killed", "needs_more"]
    evidence: list[str] = Field(description="evidence ids: F#, A#, S#")
    reasoning: str


class Assessment(BaseModel):
    updates: list[HypUpdate]
    analyses_needed: list[str] = Field(max_length=2, description="quantitative analyses to "
                                       "run now, as requests to the analyst")
    next_questions: list[str] = Field(max_length=4)
    new_hypotheses: list[HypPlan] = Field(default_factory=list, max_length=2)
    done: bool = Field(description="true if priority hypotheses are resolved")


SCOPE_SYS = ("You are the engagement manager at a strategy consultancy. Scope the client's "
             "problem: restate it as one question, with objective, success metric, horizon and "
             "constraints; note any belief the client already holds about the answer; list the "
             "clarifying questions that most change the approach; classify the case type.")
PLAN_SYS = ("Build a MECE issue tree for the question (flat list of nodes with parent ids; "
            "root has parent null) and 3-6 falsifiable hypotheses on its most likely branches, "
            "ranked. Each hypothesis says what would prove or kill it and which data questions "
            "to ask. Avoid simply adopting the client's initial belief: include competing "
            "explanations.")
ASSESS_SYS = ("Update every hypothesis from the evidence: supported, killed, needs_more or "
              "open, citing evidence ids (F#, A#, S#). Never cite an id that isn't listed. Ask "
              "for quantitative analyses where numbers would settle a hypothesis. Propose the "
              "next questions for unresolved priority hypotheses. Set done when the priority "
              "hypotheses are resolved or more evidence would not change the answer.")


def evidence_text(ws: Workspace, verified_only: bool = False, limit: int = 60) -> str:
    """Compact evidence list shared by the EM, storyliner and partner (context management:
    ids and short claims, not raw pages)."""
    eid = ws.state.engagement_id
    facts = ws.facts.eligible(eid) if verified_only else ws.facts.facts(eid)
    rows = [f"F{f.id}: {f.claim[:220]} | value={f.value} {f.unit} | period={f.period} | "
            f"source={f.source_type}" for f in facts[-limit:]]
    rows += [f"A{a.id[1:]}: {a.name} -> {a.result}" if a.ok else
             f"A{a.id[1:]}: {a.name} FAILED ({a.error})" for a in ws.state.analyses]
    rows += [f"S{a.id}: {a.name} = {a.value} {a.unit} (estimate: {a.rationale[:100]})"
             for a in ws.facts.assumptions(eid)]
    return "\n".join(rows) or "(no evidence yet)"


def hypotheses_text(ws: Workspace) -> str:
    return "\n".join(f"{h.id} [p{h.priority}, {h.status}] {h.statement} | evidence: "
                     f"{h.evidence_fact_ids + h.analysis_ids}" for h in ws.state.hypotheses)


class Engagement:
    def __init__(self, llm, ws: Workspace, *, interviewer=None, web=None,
                 retriever: Retriever | None = None,
                 checkpoint: Callable[[str, Workspace], str | None] | None = None,
                 role: str = "engagement_manager", as_of: str | None = None):
        self.llm, self.ws, self.iv, self.web = llm, ws, interviewer, web
        self.as_of = as_of  # backtests: research only sources dated on/before this
        self.retriever = retriever or Retriever.load()
        self.checkpoint, self.role = checkpoint, role
        self.prof = load_profile(ws.state.profile)
        cap = self.prof["max_tokens"]
        self.investigate = Budget({"tokens": int(cap * INVESTIGATE_SHARE),
                                   "calls": self.prof["max_calls"],
                                   "searches": self.prof["max_searches"],
                                   "pages": self.prof["max_pages"]})
        self.synthesis = Budget({"tokens": cap - int(cap * INVESTIGATE_SHARE)})
        if ws.state.mode == "case" and interviewer is None:
            raise ValueError("case mode needs an interviewer (the simulated client)")

    # ---- helpers ---------------------------------------------------------------------
    def _ask(self, system: str, user: str, schema, role: str | None = None):
        return self.llm.structured(role or self.role, [{"role": "system", "content": system},
                                                       {"role": "user", "content": user}],
                                   schema)

    def _feedback(self, stage: str) -> str:
        fb = self.checkpoint(stage, self.ws) if self.checkpoint else None
        if fb:
            self.ws.decide("client", f"feedback at {stage}", fb)
        return fb or ""

    def _client_fact(self, question: str, reply: str) -> list[int]:
        """Store a client (interviewer) answer as sourced facts: the reply itself, plus any
        numbers in it via the same verbatim-quote extractor used for web pages."""
        eid, src = self.ws.state.engagement_id, f"case://{self.iv.case.id}/interviewer"
        base = dict(engagement_id=eid, source_url=src, source_title="Client (case data)",
                    source_type="client", credibility=5)
        ids = [self.ws.facts.add(Fact(claim=f"Client, asked '{question[:120]}': "
                                            f"{reply[:300]}", quote=reply, **base))]
        if any(ch.isdigit() for ch in reply):
            page = Page(src, "Client (case data)", reply)
            found, _ = extract_facts(self.llm, page, question)
            ids += [self.ws.facts.add(Fact(**base, **pf.model_dump())) for pf in found]
        for i in ids:
            self.ws.facts.set_verified(i, True, "client-provided case data")
        return ids

    def _ask_client(self, questions: list[str]) -> None:
        for q in questions:
            asked = len(self.iv.log)
            if asked >= self.prof.get("max_questions", 10):
                self.ws.state.open_questions.append(q)
                continue
            reply = self.iv.ask(q)
            if not reply.startswith("I don't have that information"):
                self._client_fact(q, reply)
            self.ws.state.notes["interview_log"] = self.iv.log  # survives a mid-run kill
            self.ws.save()

    def _research(self, questions: list[str], hid: str | None = None) -> None:
        eid = self.ws.state.engagement_id
        caps = {"searches": 3, "pages": 6, "tokens": 20000}

        def one(q):
            ra = ResearchAssociate(self.llm, self.web, self.ws.facts, eid, caps=caps,
                                   as_of=self.as_of)
            return ra.research(q, hypothesis_id=hid)
        with ThreadPoolExecutor(max_workers=2) as pool:  # parallel, still rate-limited
            for res in pool.map(one, questions):
                for kind in ("tokens", "calls", "searches", "pages"):
                    self.investigate.charge(kind, res.spent.get(kind, 0))
                if res.brief.value is None and not res.facts:
                    self.ws.state.open_questions.append(res.question)
        self.ws.save()

    # ---- steps -----------------------------------------------------------------------
    def _scope(self) -> dict:
        st = self.ws.state
        if st.mode == "case":
            st.problem_statement = self.iv.opening()
        sc = self._ask(SCOPE_SYS, f"CLIENT PROBLEM: {st.problem_statement}", Scope)
        st.case_type = sc.case_type
        st.scope = sc.model_dump()
        if st.mode == "case":
            self._ask_client(sc.clarifying_questions)
        else:
            fb = self._feedback("scoping")
            if not fb:
                st.open_questions += [f"(unanswered scoping) {q}" for q in
                                      sc.clarifying_questions]
            else:
                st.scope["client_answers"] = fb
        self.ws.decide("engagement_manager", f"classified as {sc.case_type}",
                       sc.restated_question)
        return st.scope

    def _plan(self) -> dict:
        st = self.ws.state
        similar = self.retriever.similar_cases(st.problem_statement, 3)
        refs = "\n".join(f"- {h.doc.title} ({h.doc.meta['case_type']}): {h.doc.text[:300]} "
                         f"| structure used: {h.doc.meta['structure'][:300]}" for h in similar)
        user = (f"QUESTION: {st.scope['restated_question']}\nSCOPE: {st.scope}\n"
                f"EVIDENCE SO FAR:\n{evidence_text(self.ws)}\n\nPLAYBOOK:\n"
                f"{playbooks.playbook(st.case_type)}\n\nSIMILAR PAST CASES (patterns only):\n"
                f"{refs or '(none)'}")
        plan = self._ask(PLAN_SYS, user, TreePlan)
        nodes = {n.id: IssueNode(id=n.id, text=n.text) for n in plan.nodes}
        root = None
        for n in plan.nodes:
            if n.parent and n.parent in nodes:
                nodes[n.parent].children.append(nodes[n.id])
            elif root is None:
                root = nodes[n.id]
        self.ws.set_tree(root, [Hypothesis(id=h.id, statement=h.statement,
                                           branch_id=h.branch_id, priority=h.priority,
                                           notes=f"test: {h.test}") for h in plan.hypotheses])
        st.notes["questions"] = {h.id: h.questions for h in plan.hypotheses}
        self._feedback("hypotheses")
        return {"similar_cases": [h.doc.id for h in similar]}

    def _iterate(self, i: int) -> dict:
        st = self.ws.state
        open_h = sorted([h for h in st.hypotheses if h.status in ("open", "needs_more")],
                        key=lambda h: h.priority)[:3]
        pending = st.notes.get("questions", {})
        questions = [q for h in open_h for q in pending.get(h.id, [])[:2]]
        questions += st.notes.get(f"iter{i - 1}", {}).get("next_questions", [])
        questions = list(dict.fromkeys(questions))[:self.prof.get("max_research", 4)]
        if st.mode == "case":
            self._ask_client(questions)
        elif questions:
            self._research(questions, open_h[0].id if open_h else None)
        for h in open_h:
            pending.pop(h.id, None)
        a = self._ask(ASSESS_SYS, f"QUESTION: {st.scope['restated_question']}\nHYPOTHESES:\n"
                                  f"{hypotheses_text(self.ws)}\n\nEVIDENCE:\n"
                                  f"{evidence_text(self.ws)}", Assessment)
        known = self._known_refs()
        for u in a.updates:
            if any(h.id == u.id for h in st.hypotheses):
                facts = [int(r[1:]) for r in u.evidence if r in known and r[0] == "F"]
                self.ws.update_hypothesis(u.id, status=u.status, evidence_fact_ids=facts,
                                          notes=u.reasoning)
        for h in a.new_hypotheses:
            if not any(x.id == h.id for x in st.hypotheses):
                st.hypotheses.append(Hypothesis(id=h.id, statement=h.statement,
                                                branch_id=h.branch_id, priority=h.priority))
                pending[h.id] = h.questions
        for req in a.analyses_needed:
            analyst.analyse(self.llm, self.ws, req, [h.id for h in open_h])
        resolved = all(h.status in ("supported", "killed") for h in st.hypotheses
                       if h.priority == 1)
        return {"next_questions": a.next_questions, "done": a.done or resolved}

    def _known_refs(self) -> set[str]:
        eid = self.ws.state.engagement_id
        return ({f"F{f.id}" for f in self.ws.facts.facts(eid)}
                | {f"A{a.id[1:]}" for a in self.ws.state.analyses}
                | {f"S{a.id}" for a in self.ws.facts.assumptions(eid)})

    def _brief(self) -> str:
        st = self.ws.state
        return (f"CLIENT QUESTION: {st.scope['restated_question']}\nOBJECTIVE: "
                f"{st.scope['objective']} | METRIC: {st.scope['success_metric']} | HORIZON: "
                f"{st.scope['horizon']}\nHYPOTHESES:\n{hypotheses_text(self.ws)}\n\n"
                f"EVIDENCE:\n{evidence_text(self.ws, verified_only=True)}\n\nOPEN QUESTIONS: "
                f"{'; '.join(st.open_questions) or 'none'}")

    def _finalise_real_mode(self) -> dict:
        """Verify citations; teach the primer index about industries it didn't know."""
        st = self.ws.state
        stats = verify_facts(self.ws.facts, self.web, st.engagement_id, self.llm,
                             role="research_associate")
        ind = st.scope.get("industry", "")
        good = self.ws.facts.eligible(st.engagement_id)
        if ind and good and not self.retriever.has_industry(ind):
            add_primer(ind, "research_notes", "; ".join(f.claim for f in good[:15]),
                       [f.id for f in good[:15]])
        return stats

    def _review(self, d: storyliner.Draft) -> dict:
        """Partner review plus the deterministic validator: an untraceable number or a
        topic-only slide title is a major issue even if the partner missed it."""
        st = self.ws.state
        rv = partner.review(self.llm, st.scope["restated_question"],
                            st.scope.get("client_prior", ""), storyliner.render_text(d),
                            evidence_text(self.ws, verified_only=True))
        for i in validate_draft(self.ws, d):
            kind = "unsupported_claim" if i["kind"] != "no_action_title" else "other"
            rv.issues.append(partner.Issue(kind=kind, severity="major", where=i["where"],
                                           detail=f"[validator] {i['detail']}"))
            rv.verdict = "revise"
        return rv.model_dump()

    # ---- run -------------------------------------------------------------------------
    def run(self) -> dict:
        ws, st = self.ws, self.ws.state
        stopped = None
        with self.llm.scoped(self.investigate):
            try:
                ws.step("scope", self._scope)
                ws.step("plan", self._plan)
                for i in range(self.prof.get("max_iterations", 2)):
                    out = ws.step(f"iter{i}", lambda i=i: self._iterate(i))
                    if out and out.get("done"):
                        break
            except BudgetExceeded as e:
                stopped = str(e)
                ws.decide("system", "investigation stopped: budget cap", stopped)
                st.open_questions.append("Investigation stopped at the budget cap; "
                                         "unresolved hypotheses remain open.")
                ws.save()
        if st.mode == "real" and self.web is not None:
            ws.step("verify", self._finalise_real_mode)
        with self.llm.scoped(self.synthesis):
            draft = storyliner.Draft(**ws.step(
                "story0", lambda: storyliner.write(self.llm, self._brief()).model_dump()))
            for r in range(MAX_REVIEW_ROUNDS):
                rv = partner.Review(**ws.step(f"review{r}", lambda d=draft: self._review(d)))
                if rv.verdict == "approve" or r == MAX_REVIEW_ROUNDS - 1:
                    break
                draft = storyliner.Draft(**ws.step(f"story{r + 1}", lambda rv=rv: storyliner.write(
                    self.llm, self._brief(), partner.format_review(rv)).model_dump()))
        ws.step("deliver", lambda: deliverables.render(ws, draft))
        return {"stopped": stopped, "draft": draft, "deliverables": st.deliverables}
