"""Engagement workspace: everything an engagement knows, serialised to disk after every
change so a killed run resumes without redoing completed work.

engagements/<id>/
  workspace.json   problem, scope, issue tree, hypotheses, decisions, analyses, progress
  facts.sqlite     fact store + assumptions register (consultant.research.fact_store)
  analyses/        sandbox runs (code, stdout, result) — consultant.analysis.sandbox
  deliverables/    memo, storyline, model.xlsx, appendix
"""

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from consultant.analysis import sandbox
from consultant.config import ROOT
from consultant.llm.cache import atomic_write
from consultant.research.fact_store import FactStore

ENGAGEMENTS = Path(os.environ.get("CONSULTANT_ENGAGEMENTS") or ROOT / "engagements")


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
    # spreadsheet rows: {"name", "expression" over F#/S#/earlier names, "unit"}; mirrored as
    # live Excel formulas in model.xlsx and checked against `result`
    formulas: list[dict[str, Any]] = Field(default_factory=list)
    formula_issues: list[str] = Field(default_factory=list)


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
    def create(cls, engagement_id: str, root: Path | None = None, **fields) -> "Workspace":
        root = root or ENGAGEMENTS  # resolved at call time (overridable for tests/UI)
        ws = cls(WorkspaceState(engagement_id=engagement_id, **fields), root)
        ws.save()
        return ws

    @classmethod
    def load(cls, engagement_id: str, root: Path | None = None) -> "Workspace":
        root = root or ENGAGEMENTS
        path = root / engagement_id / "workspace.json"
        return cls(WorkspaceState.model_validate_json(path.read_text(encoding="utf-8")), root)

    @classmethod
    def open(cls, engagement_id: str, root: Path | None = None, **fields) -> "Workspace":
        """Load if it exists (resume), else create."""
        root = root or ENGAGEMENTS
        if (root / engagement_id / "workspace.json").exists():
            return cls.load(engagement_id, root)
        return cls.create(engagement_id, root, **fields)

    def save(self) -> None:
        # atomic: a kill never leaves a half-written workspace
        atomic_write(self.dir / "workspace.json", self.state.model_dump_json(indent=1))

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
                     hypothesis_ids: list[str] | None = None,
                     formulas: list[dict] | None = None, **kw) -> AnalysisRecord:
        """Run code in the sandbox and index the result; numbers live in result.json."""
        res = sandbox.run(code, self.dir / "analyses", name, **kw)
        rec = AnalysisRecord(id=f"A{len(self.state.analyses) + 1}", name=name, purpose=purpose,
                             dir=str(res.workdir.relative_to(self.dir)), ok=res.ok,
                             result=res.result, error=res.error,
                             hypothesis_ids=hypothesis_ids or [],
                             formulas=formulas or [])
        if rec.ok and rec.formulas:
            rec.formula_issues = self.check_formulas(rec)
        self.state.analyses.append(rec)
        for hid in rec.hypothesis_ids:
            if any(h.id == hid for h in self.state.hypotheses):
                self.hypothesis(hid).analysis_ids.append(rec.id)
        self.save()
        return rec

    def input_values(self) -> dict[str, float]:
        eid = self.state.engagement_id
        vals = {f"F{f.id}": f.value for f in self.facts.facts(eid) if f.value is not None}
        vals |= {f"S{a.id}": a.value for a in self.facts.assumptions(eid)}
        return vals

    def check_formulas(self, rec: AnalysisRecord) -> list[str]:
        """Each formula, evaluated over the inputs, must equal the sandbox RESULT value."""
        from consultant.research.estimate import safe_eval
        names, issues = self.input_values(), []
        inputs = dict(names)
        for row in rec.formulas:
            for lit in re.findall(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])", row["expression"]):
                x = float(lit)
                hits = [k for k, v in inputs.items()
                        if x not in (0, 1, 10, 100, 1000) and abs(v - x) <= 1e-9 * max(1, abs(x))]
                if hits:  # typed-in input: the spreadsheet wouldn't be live
                    issues.append(f"{row['name']}: hard-codes {lit}; reference {hits[0]} "
                                  "instead so the model stays live")
            try:
                v = safe_eval(row["expression"], names)
            except (ValueError, KeyError, ZeroDivisionError, SyntaxError) as e:
                issues.append(f"{row['name']}: cannot evaluate ({e})")
                continue
            names[row["name"]] = v
            want = (rec.result or {}).get(row["name"])
            if not isinstance(want, int | float) or abs(v - want) > 1e-6 * max(1, abs(want)):
                issues.append(f"{row['name']}: formula gives {v}, sandbox gave {want}")
        return issues
