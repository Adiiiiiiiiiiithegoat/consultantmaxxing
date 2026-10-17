"""Citation verifier: re-fetch each fact's source, check the quote is still there and that it
supports the claim (numbers deterministically; non-numeric claims via an LLM entailment
check). Marks facts verified / not verified with a note."""

import re
import time

from pydantic import BaseModel

from consultant.llm import LLM
from consultant.research.extract import quote_in_text, value_in_quote
from consultant.research.fact_store import Fact, FactStore
from consultant.research.web import Web


class Entailment(BaseModel):
    supports: bool
    reason: str


ENTAIL_SYS = ("Does the QUOTE, on its own, state the CLAIM? Answer supports=true only if a "
              "reader of the quote alone would accept the claim as stated.")


def check(fact: Fact, page_text: str, llm: LLM | None, role: str) -> tuple[bool, str]:
    if not quote_in_text(fact.quote, page_text):
        return False, "quote not found on re-fetched page"
    if fact.value is not None:
        if value_in_quote(fact.value, fact.quote):
            return True, "quote found; value present in quote"
        return False, "value not present in quote"
    if llm is None:
        return False, "non-numeric claim and no LLM for entailment"
    e = llm.structured(role, [{"role": "system", "content": ENTAIL_SYS},
                              {"role": "user", "content": f"CLAIM: {fact.claim}\n"
                                                          f"QUOTE: {fact.quote}"}], Entailment)
    return e.supports, f"quote found; entailment: {e.reason}"


_TRANSIENT = re.compile(r"Timeout|ConnectError|RemoteProtocolError|Server error|'429 ")


def refetch(web: Web, url: str, tries: int = 3, sleep=time.sleep):
    """Fresh fetch; a timeout/5xx/429 is retried (one slow response used to fail every fact
    from that page). 403/404/robots fail at once."""
    for i in range(tries):
        page = web.fetch(url, fresh=True)
        if not page.error or not _TRANSIENT.search(page.error) or i == tries - 1:
            return page
        sleep(5 * (i + 1))


def verify_facts(store: FactStore, web: Web, engagement_id: str, llm: LLM | None = None,
                 role: str = "research_associate", sleep=time.sleep) -> dict:
    """Verify every unchecked fact of an engagement; returns counts."""
    pages: dict[str, str | None] = {}
    ok = bad = 0
    for fact in store.facts(engagement_id):
        if fact.verified is not None:
            ok, bad = ok + fact.verified, bad + (not fact.verified)
            continue
        if fact.source_url not in pages:
            page = refetch(web, fact.source_url, sleep=sleep)
            pages[fact.source_url] = None if page.error else page.text
        text = pages[fact.source_url]
        good, note = (False, "re-fetch failed") if text is None else check(fact, text, llm, role)
        store.set_verified(fact.id, good, note)
        ok, bad = ok + good, bad + (not good)
    return {"verified": ok, "failed": bad, "rate": ok / (ok + bad) if ok + bad else None}
