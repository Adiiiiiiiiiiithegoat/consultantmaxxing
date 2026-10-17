"""Streamlit dashboard: watch an engagement live (issue tree, hypotheses, facts as they
arrive, budget use) and download its deliverables. Start new engagements from the sidebar.
Run: consult ui   (or: streamlit run consultant/ui/app.py)"""

import json
import subprocess
import sys
from pathlib import Path

import streamlit as st

from consultant.config import ROOT
from consultant.config import profile as load_profile
from consultant.workspace import ENGAGEMENTS, Workspace

STATUS_ICON = {"supported": "✅", "killed": "❌", "needs_more": "🔎", "open": "⏳"}


def engagements() -> list[str]:
    return sorted((p.parent.name for p in ENGAGEMENTS.glob("*/workspace.json")),
                  key=lambda n: -(ENGAGEMENTS / n / "workspace.json").stat().st_mtime)


def usage(eid: str) -> dict:
    path = ROOT / "traces" / f"engagement-{eid}" / "events.jsonl"
    tok = calls = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            e = json.loads(line)
            if e["kind"] == "llm" and not e.get("cached"):
                calls += 1
                tok += (e.get("prompt_tokens") or 0) + (e.get("completion_tokens") or 0)
    return {"tokens": tok, "calls": calls}


def tree_md(node, depth=0) -> str:
    out = f"{'  ' * depth}- **{node.id}** {node.text}\n"
    return out + "".join(tree_md(c, depth + 1) for c in node.children)


def sidebar() -> str | None:
    st.sidebar.title("consultantmaxxing")
    with st.sidebar.form("new"):
        problem = st.text_area("New engagement: the business problem")
        prof = st.selectbox("Budget profile", ["agent-real"])
        if st.form_submit_button("Start") and problem.strip():
            log = ENGAGEMENTS / "ui-launch.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            subprocess.Popen([sys.executable, "-m", "consultant.cli", "new", problem,
                              "--auto", "--profile", prof],
                             stdout=log.open("a"), stderr=subprocess.STDOUT, cwd=ROOT)
            st.sidebar.success("Started; it will appear in the list shortly.")
    names = engagements()
    if not names:
        st.sidebar.info("No engagements yet.")
        return None
    return st.sidebar.selectbox("Engagement", names)


def show(eid: str) -> None:
    ws = Workspace.load(eid, root=ENGAGEMENTS)
    s = ws.state
    st.title(s.scope.get("restated_question") or s.problem_statement)
    st.caption(f"{eid} · {s.mode} mode · {s.case_type or 'unclassified'} · profile {s.profile}")
    prof = load_profile(s.profile)
    u = usage(eid)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Steps done", len(s.steps_done), s.steps_done[-1] if s.steps_done else "")
    c2.metric("Tokens used", f"{u['tokens']:,}", f"of {prof['max_tokens']:,}")
    c3.metric("LLM calls", u["calls"], f"of {prof['max_calls']}")
    facts = ws.facts.facts(eid)
    c4.metric("Facts", len(facts), f"{sum(bool(f.verified) for f in facts)} verified")
    st.progress(min(1.0, u["tokens"] / prof["max_tokens"]))

    if log := s.notes.get("agent_log"):  # the single agent: its turns, newest last
        st.subheader("Agent's work")
        for e in log[1:]:
            ids = f" [{', '.join(e['ids'])}]" if e["ids"] else ""
            st.markdown(f"**{e['role']}{ids}**: {e['content'][:600]}")
    else:  # the legacy pipeline: issue tree and hypotheses
        left, right = st.columns(2)
        with left:
            st.subheader("Issue tree")
            st.markdown(tree_md(s.issue_tree) if s.issue_tree else "_not built yet_")
        with right:
            st.subheader("Hypotheses")
            for h in sorted(s.hypotheses, key=lambda h: h.priority):
                st.markdown(f"{STATUS_ICON[h.status]} **{h.id}** (p{h.priority}) {h.statement}"
                            f"  \n<small>{h.status} · evidence {h.evidence_fact_ids} "
                            f"{h.analysis_ids}</small>", unsafe_allow_html=True)

    st.subheader("Facts (newest first)")
    st.dataframe([{"id": f"F{f.id}", "claim": f.claim[:140], "value": f.value, "unit": f.unit,
                   "period": f.period, "source": f.source_url, "type": f.source_type,
                   "cred": f.credibility, "verified": f.verified} for f in reversed(facts)],
                 use_container_width=True, hide_index=True)
    if s.analyses:
        st.subheader("Analyses")
        for a in s.analyses:
            st.markdown(f"**{a.id} {a.name}**: {'ok' if a.ok else 'FAILED'} · `{a.result}`")
    if s.open_questions:
        st.subheader("Open questions")
        st.markdown("\n".join(f"- {q}" for q in s.open_questions))
    with st.expander("Decision log"):
        for d in s.decisions:
            st.markdown(f"- `{d.ts}` **{d.actor}**: {d.decision}: {d.rationale}")

    if s.deliverables:
        st.subheader("Deliverables")
        cols = st.columns(len(s.deliverables))
        for col, (key, rel) in zip(cols, s.deliverables.items(), strict=True):
            p = ws.dir / rel
            if p.exists():
                col.download_button(key, p.read_bytes(), file_name=Path(rel).name,
                                    key=f"dl-{key}")
        memo = ws.dir / s.deliverables.get("memo", "")
        if memo.is_file():
            st.markdown(memo.read_text(encoding="utf-8"))


st.set_page_config(page_title="consultantmaxxing", layout="wide")
choice = sidebar()
if choice:
    live = st.sidebar.toggle("Live refresh (5 s)", value=True)

    @st.fragment(run_every=5 if live else None)
    def panel():
        show(choice)
    panel()
else:
    st.title("consultantmaxxing")
    st.write("Start an engagement from the sidebar, or run `consult new \"...\"`.")
