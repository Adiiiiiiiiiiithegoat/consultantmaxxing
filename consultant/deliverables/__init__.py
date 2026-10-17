"""Deliverables: executive memo, slide storyline (JSON + markdown), model spreadsheet with
live formulas, sources appendix, assumptions & confidence page, and a validation report.
(.docx/.pptx rendering is optional in PLAN.md and not built; markdown converts easily.)"""

import json
import re

from consultant.agents.interview import repeats
from consultant.agents.storyliner import Draft
from consultant.deliverables import model_xlsx
from consultant.deliverables.validate import validate_draft

# ---- client-facing text: no evidence ids, display-rounded numbers ----------------------------
_ID = r"[FAS]\d+"
_ID_GROUP = re.compile(rf"\s*[\[(]\s*{_ID}(?:\s*(?:,|;|/|&|and|to|-|–)\s*{_ID})*\s*[\])]")
_ID_RUN = re.compile(rf"\b{_ID}(?:(?:\s*,\s*|\s+and\s+|\s+to\s+|\s*-\s*){_ID})*\b")
_NAMES = {"F": ("the client data", "the sources"), "A": ("the analysis", "the analyses"),
          "S": ("the estimate", "the estimates")}
_LONG_FLOAT = re.compile(r"(?<![\w.])(\d+\.\d{4,})(?![\w.])")
_POINT_ZERO = re.compile(r"(?<![\w.])(\d+)\.0(?![\w.]|\d)")


def strip_refs(text: str) -> str:
    """Evidence ids are for the audit trail, not the client: '(A4, F5)' and '[F3]' go, and an
    id used as a word ('in A4, ...') becomes what it is ('in the analysis, ...')."""
    text = _ID_GROUP.sub("", text)

    def name(m):
        ids = re.findall(_ID, m.group())
        kinds = {i[0] for i in ids}
        if len(kinds) > 1:
            return "the evidence"
        return _NAMES[kinds.pop()][len(ids) > 1]
    return _ID_RUN.sub(name, text)


def display_numbers(text: str) -> str:
    """Round raw floats for reading: 0.36666666666666664 -> 0.367, 42525.0 -> 42525. The
    exact values stay in model.xlsx; the validator accepts the rounding (validate._matches)."""
    text = _LONG_FLOAT.sub(lambda m: f"{float(m.group(1)):.3g}", text)
    return _POINT_ZERO.sub(r"\1", text)


def client(text: str) -> str:
    return display_numbers(strip_refs(text))


def open_questions(ws, d: Draft) -> list[str]:
    """One list: the draft's open questions, then the engagement's that aren't repeats."""
    out: list[str] = []
    for q in d.open_questions + ws.state.open_questions:
        if q.strip() and q not in out and not repeats(q, out, 0.5):
            out.append(q)
    return out


def _bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items) or "- none"


def answer_md(ws, d: Draft) -> str:
    """The answer as the client reads it (also what the judge scores): recommendation,
    confidence, what would change it, why, risks, next steps, open questions."""
    args = "\n\n".join(f"### {i}. {client(a.title)}\n{client(a.text)}"
                       for i, a in enumerate(d.arguments, 1))
    return (f"## Answer\n**Recommendation:** {client(d.answer)}\n\n**Confidence: "
            f"{d.confidence}.** {client(d.confidence_reason)}\n\n"
            + (f"**What would change this:**\n{_bullets(map(client, d.would_change))}\n\n"
               if d.would_change else "")
            + f"## Why\n{args}\n\n## Risks\n{_bullets(map(client, d.risks))}\n\n"
            f"## Next steps\n{_bullets(map(client, d.next_steps))}\n\n"
            f"## Open questions\n{_bullets(map(client, open_questions(ws, d)))}")


def ref_label(ws, ref: str) -> str:
    """A reader's name for an evidence id (memo sources list)."""
    ref = ref.strip()
    if ref.startswith("F") and ref[1:].isdigit() and (f := ws.facts.get(int(ref[1:]))):
        who = "client" if f.source_type == "client" else (f.source_title or f.source_url)
        return f"{who}: \"{f.claim[:90]}{'…' if len(f.claim) > 90 else ''}\""
    a = next((a for a in ws.state.analyses if a.id == ref), None)
    if a:
        return f"analysis '{a.name}' (model.xlsx)"
    s = next((s for s in ws.facts.assumptions(ws.state.engagement_id)
              if f"S{s.id}" == ref), None)
    return f"estimate '{s.name}' (assumptions.md)" if s else ref


def memo_md(ws, d: Draft, issues: list[dict]) -> str:
    st = ws.state
    sources = "\n".join(f"{i}. {client(a.title)}: " + "; ".join(ref_label(ws, r) for r in a.refs)
                        for i, a in enumerate(d.arguments, 1) if a.refs)
    rests = "; ".join(ref_label(ws, r) for r in d.key_evidence)
    flagged = ""
    unresolved = st.notes.get("unresolved_review") or []
    if unresolved:
        flagged += ("\n\n## ⚠ Unresolved review points\nThe partner's last review raised these "
                    "and no further revision fitted the budget:\n" +
                    _bullets(f"[{i['severity']}] {i['kind']} @ {i['where']}: {i['detail']}"
                             for i in unresolved))
    if removed := st.notes.get("unverified_removed"):
        flagged += ("\n\n## Figures removed as unverified\nThese numbers were not in the "
                    "evidence they cited and were replaced by '[unverified figure]':\n" +
                    _bullets(removed))
    if issues:
        flagged += ("\n\n## ⚠ Validation flags\nThese items could not be traced to cited "
                   "evidence and must not be relied on:\n" +
                   _bullets(f"{i['where']}: {i['detail']}" for i in issues))
    return (f"# {st.scope.get('restated_question') or st.problem_statement}\n\n"
            f"{answer_md(ws, d)}\n\n## Sources\n"
            + (f"The recommendation rests on: {rests}\n\n" if rests else "")
            + f"{sources or '- none'}\n\nFull detail: sources.md (facts), model.xlsx "
            f"(analyses), assumptions.md (estimates).{flagged}\n")


def storyline_md(ws, d: Draft) -> str:
    return "\n\n".join(f"## {i}. {client(s.action_title)}\n{_bullets(map(client, s.points))}\n"
                       f"*Source: {'; '.join(ref_label(ws, r) for r in s.refs) or 'none'}*"
                       for i, s in enumerate(d.slides, 1))


def sources_md(ws) -> str:
    rows = ["| ID | Claim | Value | Source | Date | Type | Credibility | Verified |",
            "|---|---|---|---|---|---|---|---|"]
    for f in ws.facts.facts(ws.state.engagement_id):
        val = "" if f.value is None else f"{f.value:g} {f.unit}".strip()
        title = (f.source_title or f.source_url).replace("|", "/")[:80]
        verified = {True: "yes", False: "NO", None: "not checked"}[f.verified]
        rows.append(f"| F{f.id} | {f.claim[:160].replace('|', '/')} | {val} | "
                    f"[{title}]({f.source_url}) | {f.source_date or ''} | {f.source_type} | "
                    f"{f.credibility}/5 | {verified} |")
    return "# Sources appendix\n\nCredibility: 5 filings/government/client data, 4 research & " \
           "business press, 3 trade, 2 aggregators, 1 blogs/forums.\n\n" + "\n".join(rows) + "\n"


def assumptions_md(ws, d: Draft) -> str:
    st = ws.state
    rows = ["| ID | Assumption | Value | Rationale |", "|---|---|---|---|"]
    rows += [f"| S{a.id} | {a.name} | {a.value:g} {a.unit} | {a.rationale[:200]} |"
             for a in ws.facts.assumptions(st.engagement_id)]
    hyps = _bullets(f"{h.id} ({h.status}): {h.statement}" for h in st.hypotheses)
    return (f"# Assumptions & confidence\n\n**Overall confidence: {d.confidence}**\n\n"
            f"## Estimates (not sourced facts)\n" + "\n".join(rows) +
            f"\n\n## Hypotheses status\n{hyps}\n\n## What would change the answer\n"
            f"{_bullets(d.would_change or open_questions(ws, d))}\n\n## Risks\n"
            f"{_bullets(d.risks)}\n")


def render(ws, d: Draft) -> dict:
    out = ws.dir / "deliverables"
    out.mkdir(exist_ok=True)
    issues = validate_draft(ws, d)
    xlsx = out / "model.xlsx"
    stats = model_xlsx.build(ws, xlsx)
    xlsx_issues = model_xlsx.check(xlsx)
    (out / "memo.md").write_text(memo_md(ws, d, issues), encoding="utf-8")
    (out / "storyline.json").write_text(json.dumps([s.model_dump() for s in d.slides],
                                                   indent=1, ensure_ascii=False),
                                        encoding="utf-8")
    (out / "storyline.md").write_text(storyline_md(ws, d), encoding="utf-8")
    (out / "sources.md").write_text(sources_md(ws), encoding="utf-8")
    (out / "assumptions.md").write_text(assumptions_md(ws, d), encoding="utf-8")
    formula_issues = [f"{a.id} {i}" for a in ws.state.analyses for i in a.formula_issues]
    report = {"draft_issues": issues, "xlsx_issues": xlsx_issues,
              "formula_issues": formula_issues, "xlsx": stats,
              "passed": not issues and not xlsx_issues and not formula_issues}
    (out / "validation.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    ws.state.deliverables = {k: f"deliverables/{v}" for k, v in {
        "memo": "memo.md", "storyline": "storyline.json", "storyline_md": "storyline.md",
        "model": "model.xlsx", "sources": "sources.md", "assumptions": "assumptions.md",
        "validation": "validation.json"}.items()}
    ws.save()
    return ws.state.deliverables
