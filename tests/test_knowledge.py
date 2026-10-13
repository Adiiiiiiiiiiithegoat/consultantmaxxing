"""Phase 4: retrieval, leakage, playbook originality and completeness. Offline.
Tests that need the private casebook data skip on machines without it."""

import json
import re

import pytest

from consultant.config import ROOT
from consultant.knowledge import playbooks
from consultant.knowledge.retrieval import BM25, Doc, Retriever, add_primer, build

DERIVED = ROOT / "data/derived"
needs_private = pytest.mark.skipif(not (DERIVED / "cases/train").exists(),
                                   reason="private casebook data not on this machine")


def test_bm25_ranks_the_relevant_doc_first():
    docs = [Doc("a", "case", "Bakery", "bakery profit falling butter costs", {}),
            Doc("b", "case", "Airline", "airline fuel costs rising on one route", {}),
            Doc("p", "primer", "Cement: costs", "cement kiln energy limestone costs", {})]
    bm = BM25(docs)
    assert bm.search("why did fuel costs rise for an airline", k=1)[0].doc.id == "b"
    assert [h.doc.id for h in bm.search("costs", kind="primer")] == ["p"]
    assert bm.search("unrelated words zebra") == []


def test_primers_auto_extend(tmp_path, monkeypatch):
    import consultant.knowledge.retrieval as r
    monkeypatch.setattr(r, "CHUNKS", tmp_path / "none.jsonl")
    monkeypatch.setattr(r, "case_docs", lambda: [])
    add_primer("Quantum Computing", "market_structure", "few vendors, long sales cycles", [7],
               index_dir=tmp_path)
    build(tmp_path)
    ret = Retriever.load(tmp_path)
    assert ret.has_industry("quantum computing")
    assert ret.primers("quantum vendors")[0].doc.meta["fact_ids"] == [7]


def test_build_refuses_a_leaked_test_case(monkeypatch):
    import consultant.knowledge.extract as ex
    from consultant.knowledge.retrieval import case_docs

    class C:
        id, case_type, industry, problem_statement, key_insight = "x", "pricing", "", "", ""
    monkeypatch.setattr(ex, "load_cases", lambda split: [C()])
    with pytest.raises(RuntimeError, match="leakage"):
        case_docs()


@needs_private
def test_leakage_no_test_case_content_in_any_index():
    from consultant.knowledge.extract import load_cases
    build()
    docs = json.loads((ROOT / "data/index/docs.json").read_text(encoding="utf-8"))
    test = load_cases("test")
    ids = {d["id"] for d in docs}
    assert not ids & {c.id for c in test}
    blob = " ".join(d["text"] for d in docs).lower()
    for c in test:
        assert c.problem_statement.lower()[:120] not in blob, c.id
        assert c.key_insight.lower()[:120] not in blob, c.id


@needs_private
def test_retrieval_eval_meets_gate():
    from consultant.evals.retrieval_eval import QUERIES, run
    if not QUERIES.exists():
        pytest.skip("retrieval queries not on this machine")
    build()
    out = run()
    assert out["n"] >= 20 and out["hit_rate"] >= 0.8, out["misses"]


def shingles(text: str, n: int = 12) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[i:i + n]) for i in range(len(words) - n + 1)}


@needs_private
def test_playbooks_share_no_12_word_run_with_source_material():
    sources = [p.read_text(encoding="utf-8") for p in DERIVED.glob("*.txt")]
    sources += [p.read_text(encoding="utf-8") for p in (DERIVED / "cases/all").glob("*.json")]
    sources.append((DERIVED / "bridgeway_notes.md").read_text(encoding="utf-8")
                   if (DERIVED / "bridgeway_notes.md").exists() else "")
    assert len(sources) > 2, "expected casebook + Bridgeway text and extracted cases"
    src = set().union(*(shingles(s) for s in sources))
    for p in sorted(playbooks.SKILLS.glob("*.md")):
        overlap = shingles(p.read_text(encoding="utf-8")) & src
        assert not overlap, (p.name, [" ".join(o) for o in list(overlap)[:3]])


def test_playbooks_complete():
    for t in playbooks.CASE_TYPES:
        text = playbooks.playbook(t)
        for section in ("## Clarify first", "## Structure", "## Traps", "## Definition of done"):
            assert section in text, (t, section)
        assert "training cases" in text or t == "ma_pe", t   # root causes mined from train
    for name in playbooks.CROSS_CUTTING:
        assert len(playbooks.load(name)) > 500
    assert playbooks.playbook("something_else") == playbooks.playbook("unconventional")
