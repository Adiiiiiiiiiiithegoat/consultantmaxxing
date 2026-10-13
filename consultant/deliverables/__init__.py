"""Deliverables: memo, storyline. (Phase 6 adds the model spreadsheet, sources appendix,
assumptions page and the validator.)"""

import json

from consultant.agents.storyliner import Draft


def memo_md(ws, d: Draft) -> str:
    st = ws.state
    args = "\n\n".join(f"### {i}. {a.title}\n{a.text}  \n*Evidence: {', '.join(a.refs)}*"
                       for i, a in enumerate(d.arguments, 1))
    bullets = "\n".join
    return (f"# {st.scope.get('restated_question', st.problem_statement)}\n\n"
            f"## Answer\n{d.answer}\n\n*Confidence: {d.confidence}*\n\n"
            f"## Why\n{args}\n\n## Risks\n{bullets(f'- {r}' for r in d.risks)}\n\n"
            f"## Next steps\n{bullets(f'- {s}' for s in d.next_steps)}\n\n"
            f"## Open questions\n{bullets(f'- {q}' for q in d.open_questions) or '- none'}\n")


def render(ws, d: Draft) -> dict:
    out = ws.dir / "deliverables"
    out.mkdir(exist_ok=True)
    (out / "memo.md").write_text(memo_md(ws, d), encoding="utf-8")
    (out / "storyline.json").write_text(json.dumps([s.model_dump() for s in d.slides],
                                                   indent=1, ensure_ascii=False),
                                        encoding="utf-8")
    ws.state.deliverables = {"memo": "deliverables/memo.md",
                             "storyline": "deliverables/storyline.json"}
    ws.save()
    return ws.state.deliverables
