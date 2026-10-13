"""Deliverables: executive memo, slide storyline (JSON + markdown), model spreadsheet with
live formulas, sources appendix, assumptions & confidence page, and a validation report.
(.docx/.pptx rendering is optional in PLAN.md and not built; markdown converts easily.)"""

import json

from consultant.agents.storyliner import Draft
from consultant.deliverables import model_xlsx
from consultant.deliverables.validate import validate_draft


def _bullets(items) -> str:
    return "\n".join(f"- {x}" for x in items) or "- none"


def memo_md(ws, d: Draft, issues: list[dict]) -> str:
    st = ws.state
    args = "\n\n".join(f"### {i}. {a.title}\n{a.text}  \n*Evidence: {', '.join(a.refs)}*"
                       for i, a in enumerate(d.arguments, 1))
    flagged = ""
    if issues:
        flagged = ("\n\n## ⚠ Validation flags\nThese items could not be traced to cited "
                   "evidence and must not be relied on:\n" +
                   _bullets(f"{i['where']}: {i['detail']}" for i in issues))
    return (f"# {st.scope.get('restated_question') or st.problem_statement}\n\n"
            f"## Answer\n{d.answer}\n\n*Confidence: {d.confidence}*\n\n## Why\n{args}\n\n"
            f"## Risks\n{_bullets(d.risks)}\n\n## Next steps\n{_bullets(d.next_steps)}\n\n"
            f"## Open questions\n{_bullets(d.open_questions + st.open_questions)}\n\n"
            f"*Evidence ids: F = sourced fact (see sources.md), A = analysis (see model.xlsx), "
            f"S = assumption (see assumptions.md).*{flagged}\n")


def storyline_md(d: Draft) -> str:
    return "\n\n".join(f"## {i}. {s.action_title}\n{_bullets(s.points)}\n*{', '.join(s.refs)}*"
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
            f"{_bullets(d.open_questions + st.open_questions)}\n\n## Risks\n{_bullets(d.risks)}\n")


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
    (out / "storyline.md").write_text(storyline_md(d), encoding="utf-8")
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
