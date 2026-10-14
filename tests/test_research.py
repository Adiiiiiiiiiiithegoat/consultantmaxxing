"""Research layer, offline: real LLM client + real Web against a mock OpenAI server, a fake
search provider and fake sites. All content is fictional."""

import json
import sqlite3

import httpx
import pytest

from consultant.agents.research_associate import ResearchAssociate
from consultant.llm import LLM
from consultant.research.credibility import classify, domain
from consultant.research.estimate import safe_eval
from consultant.research.extract import PageFact, quote_in_text
from consultant.research.fact_store import Fact, FactStore
from consultant.research.triangulate import triangulate
from consultant.research.verify import verify_facts
from consultant.research.web import SearchResult, Web
from tests.test_client import chat
from tests.test_limiter import Clock

CFG = {
    "providers": {"fake": {"base_url": "http://llm/v1", "api_key": "x"},
                  "local": {"base_url": "http://llm/v1", "api_key": "x", "local": True}},
    "models": {"fake/m": {}, "local/m": {}},
    "roles": {"research_associate": {"model": "fake/m", "params": {"max_tokens": 300}},
              "extractor": {"model": "local/m", "params": {"max_tokens": 300}}},
}

AR = ("<html><head><title>Acme Annual Report</title></head><body>"
      "<p>Acme Ltd is a fictional maker of widgets used across many industries in India.</p>"
      "<p>Acme Ltd reported revenue from operations of Rs 1,200 crore in FY2024, up 10% "
      "from the previous year, driven by strong demand for its industrial widgets.</p>"
      "<p>The board recommended a dividend and approved plans for a new plant in Pune.</p>"
      "</body></html>")
NEWS = AR.replace("Acme Annual Report", "Acme results").replace(
    "reported revenue from operations of Rs 1,200 crore",
    "posted revenue of Rs 1,201 crore")
SITES = {"acme.example": AR, "news.example.com": NEWS, "blog.example.org": AR}


def site(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if request.url.path == "/robots.txt":
        body = "User-agent: *\nDisallow: /\n" if host == "blog.example.org" else ""
        return httpx.Response(200 if body else 404, text=body)
    site.hits.append(str(request.url))
    return httpx.Response(200, text=SITES[host], headers={"content-type": "text/html"})


class Provider:
    def __init__(self):
        self.queries = []

    def search(self, query, max_results=8):
        self.queries.append(query)
        n = len(self.queries)
        return [SearchResult("Blog", f"https://blog.example.org/p{n}", ""),
                SearchResult("News", f"https://news.example.com/acme{n}", ""),
                SearchResult("AR", f"https://acme.example/investors/ar{n}.html", "")]


def llm_server(answered=False):
    def reply(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if "search queries" in system:
            out = {"queries": ["acme revenue fy2024", "acme annual report 2024"]}
        elif "You extract facts" in system:
            out = {"facts": [
                {"claim": "Acme revenue FY2024 was Rs 1,200 crore", "metric": "revenue",
                 "value": 1200, "unit": "Rs crore", "entity": "Acme Ltd", "period": "FY2024",
                 "quote": "Acme Ltd reported revenue from operations of Rs 1,200 crore in "
                          "FY2024" if "reported revenue" in body["messages"][-1]["content"]
                 else "Acme Ltd posted revenue of Rs 1,201 crore in FY2024"},
                {"claim": "Acme has 5,000 staff", "metric": "employees", "value": 5000,
                 "unit": "people", "entity": "Acme Ltd", "period": "FY2024",
                 "quote": "Acme employs 5,000 people"}]}          # hallucinated: not on page
        elif "Judge whether" in system:
            out = {"answered": answered, "missing": "more sources",
                   "next_query": f"acme revenue {len(reply.calls)}"}
        elif "research brief" in system:
            out = {"answer": "Rs 1,200 crore [F1]", "value": 1200, "unit": "Rs crore",
                   "period": "FY2024", "fact_ids": [1, 999], "confidence": "high", "gaps": ""}
        else:
            raise AssertionError(system[:80])
        reply.calls.append(system[:20])
        return chat(json.dumps(out), prompt=400, completion=100)
    reply.calls = []
    return reply


def make(tmp_path, answered=False, caps=None):
    site.hits = []
    clock = Clock()
    llm = LLM(CFG, cache_dir=tmp_path / "c", sleep=clock.sleep, clock=clock,
              http_client=httpx.Client(transport=httpx.MockTransport(llm_server(answered))))
    provider = Provider()
    web = Web(provider, cache_dir=tmp_path / "w", sleep=lambda s: None,
              client=httpx.Client(transport=httpx.MockTransport(site)))
    store = FactStore()
    ra = ResearchAssociate(llm, web, store, "eng1", caps=caps, allow_estimate=False)
    return ra, provider, store, web


def test_credibility_ranking():
    assert classify("https://www.rbi.org.in/x") == "government"
    assert classify("https://www.bseindia.com/x.pdf") == "filing"
    assert classify("https://www.infosys.com/investors/reports/ar-24.pdf") == "filing"
    assert classify("https://economictimes.indiatimes.com/a") == "press"
    assert classify("https://medium.com/@x/y") == "blog"
    assert classify("https://someindustrymag.com/a") == "trade"
    assert domain("https://www.business-standard.co.in/a") == "business-standard.co.in"


def test_fact_without_source_or_quote_is_impossible():
    store = FactStore()
    db = store.db
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO facts (engagement_id, claim, source_url, quote, source_type, "
                   "credibility, retrieved_at) VALUES ('e','c','','q','trade',3,'t')")
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO assumptions (engagement_id, name, value, rationale, label, "
                   "created_at) VALUES ('e','n',1,'r','fact','t')")


def test_research_associate_end_to_end_offline(tmp_path):
    ra, provider, store, _ = make(tmp_path, answered=True)
    res = ra.research("What was Acme's revenue in FY2024?", hypothesis_id="H1")
    assert all(f.source_url and f.quote for f in res.facts)
    assert not any("5,000" in f.quote for f in res.facts)       # hallucinated fact dropped
    assert res.rejected_facts > 0
    assert not any("blog.example.org" in h for h in site.hits)  # robots.txt respected
    assert res.brief.fact_ids == [1]                            # unknown id 999 removed
    assert all(f.hypothesis_ids == ["H1"] for f in res.facts)
    f = next(x for x in res.findings if x.metric == "revenue")
    assert f.status == "confirmed" and len(f.domains) == 2      # AR + news agree within 2%


def test_caps_are_respected(tmp_path):
    caps = {"searches": 2, "pages": 3, "tokens": 50_000}
    ra, provider, _, _ = make(tmp_path, answered=False, caps=caps)
    res = ra.research("What was Acme's revenue in FY2024?")
    assert len(provider.queries) <= 2 and res.spent["searches"] <= 2
    assert len(site.hits) <= 3 and res.spent["pages"] <= 3
    assert res.brief is not None


def test_token_cap_stops_research_but_brief_is_written(tmp_path):
    ra, provider, _, _ = make(tmp_path, answered=False,
                              caps={"searches": 10, "pages": 30, "tokens": 6000})
    res = ra.research("What was Acme's revenue in FY2024?")
    assert res.spent["tokens"] <= 6000
    assert res.brief.answer


def test_triangulation_conflict_kept_as_range():
    def fact(i, url, v):
        return Fact(id=i, engagement_id="e", claim="c", metric="revenue", value=v,
                    unit="crore", entity="Acme", period="FY24", source_url=url, quote="q",
                    source_type="press", credibility=4)
    out = triangulate([fact(1, "https://a.com/x", 100), fact(2, "https://b.com/y", 130)])
    assert out[0].status == "conflict" and (out[0].low, out[0].high) == (1e9, 1.3e9)
    single = triangulate([fact(1, "https://a.com/x", 100), fact(2, "https://a.com/z", 100)])
    assert single[0].status == "single_source"


def test_verifier_refetches_and_checks(tmp_path):
    ra, _, store, web = make(tmp_path, answered=True)
    ra.research("What was Acme's revenue in FY2024?")
    store.add(Fact(engagement_id="eng1", claim="bad", metric="x", value=7, unit="",
                   source_url="https://acme.example/investors/ar1.html",
                   quote="this sentence is not on the page", source_type="filing",
                   credibility=5))
    stats = verify_facts(store, web, "eng1")
    assert stats["failed"] == 1 and stats["verified"] >= 1
    assert all(f.verified for f in store.eligible("eng1"))


def test_safe_eval_and_quote_check():
    assert safe_eval("a * b / (c + 1)", {"a": 10, "b": 6, "c": 2}) == 20
    with pytest.raises(ValueError):
        safe_eval("__import__('os')", {})
    assert quote_in_text("Revenue  was Rs 5 crore", "x. Revenue was Rs 5 crore. y")
    assert not quote_in_text("short", "short")  # too short to count as a quote
    PageFact(claim="c", metric="m", value=None, unit="", entity="", period="", quote="q")



def test_estimate_formula_assignment_and_bad_formula_is_a_validation_error():
    # smoke-asianpaints-2 crashed on an LLM formula written as an assignment
    from pydantic import ValidationError

    from consultant.research.estimate import EstimatePlan
    assert safe_eval("margin_fy22 = margin_fy23 - improvement_fy24",
                     {"margin_fy23": 20, "improvement_fy24": 3}) == 17
    f = [{"name": "a", "value": 2, "unit": "", "rationale": "r"}]
    assert EstimatePlan(approach="top_down", factors=f, formula="a * 3", unit="").formula
    for bad in ("a * b", "a; a", "a ="):  # unknown name, statement, not an expression
        with pytest.raises(ValidationError):
            EstimatePlan(approach="top_down", factors=f, formula=bad, unit="")

def test_table_rows_are_aligned_to_header_years_deterministically():
    from consultant.research.extract import expand_table_fact, header_years
    assert header_years("Fiscal Year | TTM | FY 2026 | FY 2025 | FY 2024") == ["2026", "2025",
                                                                              "2024"]
    assert header_years("For the year ended 31st March 2024  31st March 2023") == ["2024", "2023"]
    assert header_years("| FY24 | FY23 |") == ["2024", "2023"]
    assert header_years("2023-24  2022-23") == ["2024", "2023"]
    row = PageFact(claim="c", metric="revenue", value=405750, unit="million INR", entity="T",
                   period="FY 2023", quote="Revenue | 924,170 | 875,840 | 604,560 | 510,840",
                   header="Fiscal Year | TTM | FY 2026 | FY 2025 | FY 2024")
    got = {g.period: g.value for g in expand_table_fact(row)}
    assert got == {"2026": 875840, "2025": 604560, "2024": 510840}   # TTM column skipped
    short = row.model_copy(update={"quote": "Revenue | 924,170"})
    assert expand_table_fact(short) == [short]                         # counts don't line up
    roman = row.model_copy(update={"quote": "Revenue from operations (I+II+III)  51,084  40,575",
                                   "header": "31st March 2024  31st March 2023"})
    assert {g.period: g.value for g in expand_table_fact(roman)} == {"2024": 51084,
                                                                     "2023": 40575}


def test_brief_value_must_match_a_cited_fact_for_the_period():
    from consultant.agents.research_associate import Brief, brief_problem
    def fact(i, v, period):
        return Fact(id=i, engagement_id="e", claim="c", metric="m", value=v, unit="crore",
                    period=period, source_url="https://a.com", quote="q", source_type="press",
                    credibility=4)
    facts = [fact(1, 13499, "Mar2023"), fact(2, 15702, "FY2023-24")]
    q = "What was X in FY2023-24?"
    def brief(v, ids):
        return Brief(answer="a", value=v, unit="crore", period="FY2023-24", fact_ids=ids,
                     confidence="high", gaps="")
    assert brief_problem(brief(13499, [1]), facts, q) is not None    # cited fact is FY23
    assert brief_problem(brief(14000, [2]), facts, q) is not None    # value not in fact
    assert brief_problem(brief(15702, [2]), facts, q) is None
    assert brief_problem(brief(None, []), facts, q) is None          # honest null is fine


def test_find_header_takes_nearest_year_line_above_the_row():
    from consultant.research.extract import find_header
    page = ("Key financials\n| Particulars | Mar2025 | Mar2024 | Mar2023 |\n| --- | --- |\n"
            "| Consolidated Net Profit | ₹17,390 Cr | ₹15,702 Cr | ₹14,851 Cr |\n")
    row = "| Consolidated Net Profit | ₹17,390 Cr | ₹15,702 Cr | ₹14,851 Cr |"
    assert find_header(page, row) == "| Particulars | Mar2025 | Mar2024 | Mar2023 |"
    assert find_header("no table here", "| X | 1 | 2 | 3 |") == ""


def test_prose_and_duplicated_columns_are_not_expanded():
    from consultant.research.extract import expand_table_fact
    prose = PageFact(claim="c", metric="net_profit", value=26233, unit="crore", entity="I",
                     period="FY24", quote="In FY2024 net profit rose 8.9 per cent to Rs 26,233 "
                     "crore from Rs 24,095 crore", header="On a year-on-year basis 2023 and 2024")
    assert expand_table_fact(prose) == [prose]
    dup = prose.model_copy(update={
        "quote": "Net Profit | 22,110 | 22,110 | 24,095 | 24,095 | 26,233 | 26,233",
        "header": "| Mar 2022 | Mar 2023 | Mar 2024 |"})
    assert expand_table_fact(dup) == [dup]   # 6 values for 3 years: ambiguous, don't guess


def test_fact_store_concurrent_add_and_read():
    from concurrent.futures import ThreadPoolExecutor
    store = FactStore()

    def work(i):
        store.add(Fact(engagement_id="e", claim=f"c{i}", source_url="https://a.com",
                       quote="q", source_type="press", credibility=4))
        return len(store.facts("e"))
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(work, range(100)))
    assert len(store.facts("e")) == 100


def test_extractor_sees_collapsed_empty_cells_and_survives_a_bad_chunk():
    # fact-finding tatamotors: qwen3:4b looped copying "|  |  |" runs until max_tokens, and
    # the StructuredOutputError killed the eval
    from consultant.llm import StructuredOutputError
    from consultant.research.extract import extract_facts
    from consultant.research.web import Page

    class FakeLLM:
        def __init__(self, fail):
            self.fail, self.seen = fail, []

        def structured(self, role, msgs, schema):
            self.seen.append(msgs[-1]["content"])
            if self.fail:
                raise StructuredOutputError("extractor: no valid PageFacts after 3 tries")
            return schema(facts=[PageFact(
                claim="Revenue FY24 was 4,37,928 crore", metric="revenue", value=437928,
                unit="crore", entity="Tata Motors", period="FY24",
                quote="| Revenue | 4,37,928 | 3,45,967 |")])
    text = "Results table " * 10 + "\n| Revenue |  |  |  | 4,37,928 |  |  | 3,45,967 |\n"
    page = Page(url="https://x.com/a", title="t", text=text)
    llm = FakeLLM(fail=False)
    facts, rejected = extract_facts(llm, page, "Tata Motors revenue FY24")
    assert "|  |" not in llm.seen[0] and facts and facts[0].value == 437928 and not rejected
    assert extract_facts(FakeLLM(fail=True), page, "q") == ([], 0)


def test_facts_table_shows_quote_and_source_title():
    # fact-finding: the brief picked a quarterly "Revenue at Rs 61,237 cr" for a full-year
    # question and a balance-sheet total labelled as profit; it only saw the extractor's claim
    from consultant.agents.research_associate import facts_table
    f = Fact(id=16, engagement_id="e", source_url="https://smestreet.in/x",
             source_title="TCS Q4 FY24 results", claim="TCS revenue FY2023-24 was 61,237 cr",
             metric="revenue", value=61237, unit="cr", entity="TCS", period="FY 2023-24",
             quote="Revenue at ₹61,237 cr,\n +3.5% YoY", source_type="press", credibility=3)
    row = facts_table([f], "TCS revenue FY2023-24")
    assert '"TCS Q4 FY24 results"' in row and 'quote: "Revenue at ₹61,237 cr, +3.5% YoY"' in row


def test_duplicated_year_columns_and_headerless_rows():
    # fact-finding infosys: "Mar 2024 | Apr 2024" columns repeat each value, and a row with no
    # verified header had its FY26 cell labelled FY2023-24 by the extractor
    from consultant.research.extract import expand_table_fact
    row = PageFact(claim="c", metric="revenue", value=178650, unit="crore", entity="I",
                   period="Apr 2026",
                   quote="Revenue | 1,46,767 | 1,46,767 | 1,53,670 | 1,53,670",
                   header="Line item | Mar 2023 | Apr 2023 | Mar 2024 | Apr 2024")
    assert {g.period: g.value for g in expand_table_fact(row)} == {"2023": 146767,
                                                                   "2024": 153670}
    mixed = row.model_copy(update={"quote": "Revenue | 37,923 | 36,000 | 1,53,670 | 1,46,767",
                                   "header": "| Q4 2024 | Q4 2023 | FY 2024 | FY 2023 |"})
    assert expand_table_fact(mixed) == [mixed]          # quarter and year share a year
    bare = row.model_copy(update={"quote": "Net Profit | 26,233 | 26,713 | 29,440",
                                  "header": "", "period": "FY2023-24"})
    assert expand_table_fact(bare)[0].period == ""     # unverifiable column pick
    single = row.model_copy(update={"quote": "Net profit | 26,233", "header": ""})
    assert expand_table_fact(single) == [single]        # one value: nothing to mix up


def test_verifier_retries_a_timed_out_refetch_but_not_a_404():
    # fact-finding L&T: one ReadTimeout on the re-fetch failed 20 facts from one page
    from consultant.research.verify import refetch
    from consultant.research.web import Page

    class FlakyWeb:
        def __init__(self, errors):
            self.errors, self.calls = list(errors), 0

        def fetch(self, url, fresh=False):
            self.calls += 1
            err = self.errors.pop(0) if self.errors else None
            return Page(url, "t", "" if err else "text", error=err)
    web = FlakyWeb(["ReadTimeout('The read operation timed out')"])
    assert refetch(web, "u", sleep=lambda s: None).error is None and web.calls == 2
    web = FlakyWeb(["HTTPStatusError(\"Client error '404 Not Found'\")"])
    assert refetch(web, "u", sleep=lambda s: None).error and web.calls == 1
