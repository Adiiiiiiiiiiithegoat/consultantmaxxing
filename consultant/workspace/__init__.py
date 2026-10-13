"""Engagement workspace: everything an engagement knows, serialised to disk after every
change so a killed run resumes without redoing completed work.

engagements/<id>/
  workspace.json   problem, scope, issue tree, hypotheses, decisions, analyses, progress
  facts.sqlite     fact store + assumptions register (consultant.research.fact_store)
  analyses/        sandbox runs (code, stdout, result) — consultant.analysis.sandbox
  deliverables/    memo, storyline, model.xlsx, appendix
"""

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from consultant.analysis import sandbox
from consultant.config import ROOT
from consultant.research.fact_store import FactStore

ENGAGEMENTS = ROOT / "engagements"


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class IssueNode(BaseModel):
    id: str
    text: str
    children: list["IssueNode"] = Field(default_factory=list)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


class Hypothesis(BaseModel):
    id: str
    statement: str
    branch_id: str = ""
    priority: int = 2                      # 1 = highest
    status: Literal["open", "supported", "killed", "needs_more"] = "open"
    evidence_fact_ids: list[int] = Field(default_factory=list)
    analysis_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class Decision(BaseModel):
    ts: str = Field(default_factory=now)
    actor: str
    decision: str
    rationale: str = ""


class AnalysisRecord(BaseModel):
    id: str
    name: str
    purpose: str
    dir: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    hypothesis_ids: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)


class WorkspaceState(BaseModel):
    engagement_id: str
    created_at: str = Field(default_factory=now)
    mode: Literal["case", "real"] = "real"
    profile: str = "dev"
    problem_statement: str = ""
    scope: dict[str, Any] = Field(default_factory=dict)
    case_type: str = ""
    issue_tree: IssueNode | None = None
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    analyses: list[AnalysisRecord] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    deliverables: dict[str, str] = Field(default_factory=dict)
    steps_done: list[str] = Field(default_factory=list)
    notes: dict[str, Any] = Field(default_factory=dict)  # step outputs (briefs, drafts, ...)


class Workspace:
    def __init__(self, state: WorkspaceState, root: Path):
        self.state, self.dir = state, root / state.engagement_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._facts: FactStore | None = None

    # ---- persistence ------------------------------------------------------------------
    @classmethod
    def create(cls, engagement_id: str, root: Path = ENGAGEMENTS, **fields) -> "Workspace":
        ws = cls(WorkspaceState(engagement_id=engagement_id, **fields), root)
        ws.save()
        return ws

    @classmethod
    def load(cls, engagement_id: str, root: Path = ENGAGEMENTS) -> "Workspace":
        path = root / engagement_id / "workspace.json"
        return cls(WorkspaceState.model_validate_json(path.read_text(encoding="utf-8")), root)

    @classmethod
    def open(cls, engagement_id: str, root: Path = ENGAGEMENTS, **fields) -> "Workspace":
        """Load if it exists (resume), else create."""
        if (root / engagement_id / "workspace.json").exists():
            return cls.load(engagement_id, root)
        return cls.create(engagement_id, root, **fields)

    def save(self) -> None:
        path = self.dir / "workspace.json"
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(self.state.model_dump_json(indent=1), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a kill never leaves a half-written workspace

    @property
    def facts(self) -> FactStore:
        if self._facts is None:
            self._facts = FactStore(self.dir / "facts.sqlite")
        return self._facts

    # ---- resumable steps --------------------------------------------------------------
    def step(self, name: str, fn: Callable[[], Any]) -> Any:
        """Run `fn` once per engagement; on resume a completed step returns its saved output."""
        if name in self.state.steps_done:
            return self.state.notes.get(name)
        out = fn()
        if out is not None:
            self.state.notes[name] = out
        self.state.steps_done.append(name)
        self.save()
        return out

    # ---- mutations (each saves) -------------------------------------------------------
    def decide(self, actor: str, decision: str, rationale: str = "") -> None:
        self.state.decisions.append(Decision(actor=actor, decision=decision, rationale=rationale))
        self.save()

    def set_tree(self, tree: IssueNode, hypotheses: list[Hypothesis]) -> None:
        self.state.issue_tree, self.state.hypotheses = tree, hypotheses
        self.save()

    def hypothesis(self, hid: str) -> Hypothesis:
        return next(h for h in self.state.hypotheses if h.id == hid)

    def update_hypothesis(self, hid: str, **changes) -> Hypothesis:
        h = self.hypothesis(hid)
        for k, v in changes.items():
            if k in ("evidence_fact_ids", "analysis_ids"):
                v = sorted(set(getattr(h, k)) | set(v))
            setattr(h, k, v)
        self.save()
        return h

    def run_analysis(self, name: str, purpose: str, code: str,
                     hypothesis_ids: list[str] | None = None, **kw) -> AnalysisRecord:
        """Run code in the sandbox and index the result; numbers live in result.json."""
        res = sandbox.run(code, self.dir / "analyses", name, **kw)
        rec = AnalysisRecord(id=f"A{len(self.state.analyses) + 1}", name=name, purpose=purpose,
                             dir=str(res.workdir.relative_to(self.dir)), ok=res.ok,
                             result=res.result, error=res.error,
                             hypothesis_ids=hypothesis_ids or [])
        self.state.analyses.append(rec)
        for hid in rec.hypothesis_ids:
            if any(h.id == hid for h in self.state.hypotheses):
                self.hypothesis(hid).analysis_ids.append(rec.id)
        self.save()
        return rec
