"""Source-type classification and credibility ranking by URL.

Order (PLAN.md): filings / regulators / government > established research & business press >
trade press > blogs / forums. A heuristic on domains and paths; unknown domains rank as
trade press, not as authoritative.
"""

from urllib.parse import urlparse

CREDIBILITY = {"filing": 5, "government": 5, "research": 4, "press": 4, "trade": 3,
               "aggregator": 2, "blog": 1}

GOVERNMENT = (".gov", ".gov.in", ".nic.in", ".gov.uk", ".europa.eu", "rbi.org.in", "sebi.gov.in",
              "mospi.gov.in", "data.gov.in", "worldbank.org", "imf.org", "oecd.org", "un.org",
              "censusindia.gov.in", "pib.gov.in", "trai.gov.in", "dgca.gov.in")
EXCHANGES = ("bseindia.com", "nseindia.com", "sec.gov", "mca.gov.in", "annualreports.com")
RESEARCH = ("mckinsey.com", "bcg.com", "bain.com", "pwc.com", "pwc.in", "deloitte.com", "ey.com",
            "kpmg.com", "kearney.com", "ibef.org", "crisil.com", "icra.in", "careratings.com",
            "nasscom.in", "ficci.in", "cii.in", "niti.gov.in", "statista.com", "brookings.edu")
PRESS = ("reuters.com", "bloomberg.com", "ft.com", "wsj.com", "economist.com",
         "economictimes.indiatimes.com", "livemint.com", "business-standard.com",
         "thehindubusinessline.com", "financialexpress.com", "moneycontrol.com", "cnbc.com",
         "thehindu.com", "indianexpress.com", "hindustantimes.com", "timesofindia.indiatimes.com",
         "forbes.com", "fortune.com", "nytimes.com", "bbc.com", "bbc.co.uk")
AGGREGATORS = ("stockanalysis.com", "macrotrends.net", "screener.in", "tickertape.in",
               "marketscreener.com", "companiesmarketcap.com", "wikipedia.org", "trendlyne.com")
BLOGS = ("medium.com", "quora.com", "reddit.com", "blogspot.", "wordpress.", "substack.com",
         "linkedin.com", "facebook.com", "x.com", "twitter.com", "youtube.com", "scribd.com",
         "slideshare.net")
IR_HINTS = ("/investor", "/investors", "annual-report", "annualreport", "/ir/", "integrated-report")


def _match(host: str, domains) -> bool:
    return any(host == d.lstrip(".") or host.endswith(d if d.startswith(".") else "." + d)
               or (d.endswith(".") and d in host) for d in domains)


def classify(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.lower()
    if _match(host, EXCHANGES):
        return "filing"
    if _match(host, GOVERNMENT):
        return "government"
    if _match(host, BLOGS):
        return "blog"
    if _match(host, AGGREGATORS):
        return "aggregator"
    if _match(host, RESEARCH):
        return "research"
    if _match(host, PRESS):
        return "press"
    if any(h in path for h in IR_HINTS):  # a company's own investor-relations pages
        return "filing"
    return "trade"


def credibility(url: str) -> int:
    return CREDIBILITY[classify(url)]


def domain(url: str) -> str:
    """Registrable-ish domain, for 'independent source' checks."""
    host = urlparse(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    keep = 3 if len(parts) > 2 and parts[-2] in ("co", "gov", "nic", "org", "ac", "com") else 2
    return ".".join(parts[-keep:])
