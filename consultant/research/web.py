"""Web search and fetching: keyless DuckDuckGo search behind a provider interface, and a polite
fetcher (robots.txt, per-host delay, disk cache, HTML via trafilatura, PDF via pymupdf).
Every search and page fetch is charged to the run's Budget."""

import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from consultant.config import ROOT
from consultant.llm.cache import DiskCache
from consultant.tracing import Budget, Tracer

USER_AGENT = "consultantmaxxing-research/0.1 (+https://github.com/; polite research bot)"
CACHE = ROOT / ".llm_cache"
HOST_DELAY = 2.0  # seconds between requests to the same host
MAX_BYTES = 30_000_000  # annual-report PDFs are large


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


@dataclass
class Page:
    url: str
    title: str
    text: str
    date: str | None = None
    content_type: str = "html"
    error: str | None = None


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int = 8) -> list[SearchResult]: ...


class DdgsSearch:
    """Keyless DuckDuckGo via the `ddgs` package. Swap for Tavily/Brave by implementing
    SearchProvider."""

    def search(self, query: str, max_results: int = 8) -> list[SearchResult]:
        from ddgs import DDGS
        rows = DDGS().text(query, max_results=max_results, region="in-en") or []
        return [SearchResult(r.get("title", ""), r["href"], r.get("body", "")) for r in rows
                if str(r.get("href", "")).startswith(("http://", "https://"))]


class Web:
    """Search + fetch with caching, politeness and budget accounting."""

    def __init__(self, provider: SearchProvider | None = None, budget: Budget | None = None,
                 tracer: Tracer | None = None, cache_dir=CACHE, client: httpx.Client | None = None,
                 sleep=time.sleep, clock=time.monotonic):
        self.provider = provider or DdgsSearch()
        self.budget, self.tracer = budget, tracer
        self.search_cache = DiskCache(cache_dir / "search")
        self.page_cache = DiskCache(cache_dir / "pages")
        self.client = client or httpx.Client(headers={"User-Agent": USER_AGENT},
                                             follow_redirects=True, timeout=15)
        self.sleep, self.clock = sleep, clock
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _trace(self, kind, **data):
        if self.tracer:
            self.tracer.log(kind, **data)

    def _charge(self, kind: str, extra: Budget | None) -> None:
        budgets = [b for b in (self.budget, extra) if b]
        for b in budgets:
            b.check(kind)
        for b in budgets:
            b.charge(kind)

    # ---- search -------------------------------------------------------------------------
    def search(self, query: str, max_results: int = 8,
               budget: Budget | None = None) -> list[SearchResult]:
        key = DiskCache.key({"q": query, "n": max_results, "p": type(self.provider).__name__})
        if hit := self.search_cache.get(key):
            self._trace("search", query=query, cached=True, n=len(hit["results"]))
            return [SearchResult(**r) for r in hit["results"]]
        self._charge("searches", budget)
        try:
            results = self.provider.search(query, max_results)
        except Exception as e:  # ddgs raises its own rate-limit/timeout errors; don't cache
            self._trace("search", query=query, cached=False, error=repr(e)[:300])
            return []
        self.search_cache.set(key, {"query": query, "results": [vars(r) for r in results]})
        self._trace("search", query=query, cached=False, n=len(results))
        return results

    # ---- fetch --------------------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        host = urlparse(url)
        base = f"{host.scheme}://{host.netloc}"
        if base not in self._robots:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt", timeout=10)
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])  # unreachable robots.txt: treat as allow-all
            self._robots[base] = rp
        return self._robots[base].can_fetch(USER_AGENT, url)

    def _polite(self, host: str) -> None:
        with self._lock:
            wait = self._last_hit.get(host, -1e9) + HOST_DELAY - self.clock()
            self._last_hit[host] = self.clock() + max(wait, 0)
        if wait > 0:
            self.sleep(wait)

    def fetch(self, url: str, fresh: bool = False, budget: Budget | None = None) -> Page:
        key = DiskCache.key({"url": url})
        if not fresh and (hit := self.page_cache.get(key)):
            self._trace("fetch", url=url, cached=True)
            return Page(**hit)
        if not self.allowed(url):
            self._trace("fetch", url=url, blocked="robots.txt")
            return Page(url, "", "", error="disallowed by robots.txt")
        self._charge("pages", budget)
        self._polite(urlparse(url).netloc)
        try:
            r = self.client.get(url)
            r.raise_for_status()
            if len(r.content) > MAX_BYTES:
                raise ValueError(f"too large ({len(r.content)} bytes)")
            page = parse(url, r.content, r.headers.get("content-type", ""))
        except (httpx.HTTPError, ValueError) as e:
            page = Page(url, "", "", error=repr(e)[:300])
        if not page.error:
            self.page_cache.set(key, vars(page))
        self._trace("fetch", url=url, cached=False, chars=len(page.text), error=page.error)
        return page


def parse(url: str, content: bytes, content_type: str) -> Page:
    if "pdf" in content_type or url.lower().split("?")[0].endswith(".pdf"):
        import pymupdf
        doc = pymupdf.open(stream=content, filetype="pdf")
        text = "\n".join(p.get_text() for p in doc)
        return Page(url, doc.metadata.get("title") or url, text,
                    date=(doc.metadata.get("creationDate") or "")[2:10] or None,
                    content_type="pdf")
    import trafilatura
    html = content.decode("utf-8", errors="replace")
    doc = trafilatura.bare_extraction(html, url=url, with_metadata=True)
    if not doc:
        return Page(url, "", "", error="no extractable text")
    d = doc if isinstance(doc, dict) else doc.as_dict()
    return Page(url, d.get("title") or "", d.get("text") or "", date=d.get("date"))
