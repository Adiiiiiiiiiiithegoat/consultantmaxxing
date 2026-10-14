"""Extract sourced facts from a fetched page. The LLM proposes facts; a fact is kept only if
its quote is found verbatim in the page (whitespace/punctuation-normalised) and its value
appears as a number inside that quote. Nothing the page doesn't say gets stored."""

import re
import unicodedata

from pydantic import BaseModel, Field

from consultant.llm import LLM, StructuredOutputError
from consultant.research.web import Page

CHUNK, OVERLAP, TOP_CHUNKS = 2500, 300, 3

SCALE = {"thousand": 1e3, "lakh": 1e5, "lac": 1e5, "lakhs": 1e5, "million": 1e6, "mn": 1e6,
         "crore": 1e7, "cr": 1e7, "crores": 1e7, "billion": 1e9, "bn": 1e9,
         "lakh crore": 1e12, "trillion": 1e12, "tn": 1e12}


class PageFact(BaseModel):
    claim: str = Field(description="one-sentence statement of the fact")
    metric: str = Field(description="short snake_case name, e.g. revenue_from_operations")
    value: float | None = Field(description="the number exactly as written in the quote, "
                                            "without commas; null if not numeric")
    unit: str = Field(description="unit/scale as written, e.g. 'crore', 'lakh crore', "
                                  "'million', '%', 'USD billion', 'people'")
    entity: str
    period: str = Field(description="e.g. FY2023-24, 2023, Q1 FY25; empty if none")
    quote: str = Field(description="the sentence copied character-for-character from the "
                                   "text, containing the value")
    header: str = Field(default="", description="if the quote is a table row: the table's "
                        "header row (with the column years), copied verbatim; else empty")


class PageFacts(BaseModel):
    facts: list[PageFact]


EXTRACT_SYS = (
    "You extract facts relevant to a research question from a web page excerpt. Only "
    "extract what the excerpt states. Each fact needs a quote copied character-for-character "
    "from the excerpt (one sentence or table row) that contains the value. Prefer the exact "
    "figure asked for; include closely related figures (other years, units) only if they "
    "help answer. For a table row holding several years, `value` and `period` must be the "
    "column for the year asked about, and `header` must copy the header row that shows the "
    "years. Return at most 5 facts; return an empty list if nothing is relevant.")


_EMPTY_CELLS = re.compile(r"\|(?:[ \t]*\|)+")  # "|  |  |  |" runs in scraped tables


def collapse_cells(text: str) -> str:
    """Runs of empty table cells add nothing and make small models loop copying them."""
    return _EMPTY_CELLS.sub("|", text)


def norm(text: str) -> str:
    text = collapse_cells(unicodedata.normalize("NFKC", text)).lower()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"')
    text = text.replace("”", '"').replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def quote_in_text(quote: str, text: str) -> bool:
    q = norm(quote).strip(" .\"'")
    return len(q) >= 12 and q in norm(text)


def numbers_in(text: str) -> list[float]:
    out = []
    for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", unicodedata.normalize("NFKC", text)):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return out


def value_in_quote(value: float, quote: str, rel: float = 0.005) -> bool:
    return any(abs(n - value) <= rel * max(abs(value), 1e-9) for n in numbers_in(quote))


def to_base(value: float, unit: str) -> tuple[float, str]:
    """Normalise scale words: (153670, 'crore') -> (1.5367e12, ''); currencies are kept
    as the remaining unit text so INR and USD never mix."""
    u = norm(unit).replace("₹", " inr ").replace("$", " usd ").replace("lakh crore", "lakhcrore")
    alias = {"rs": "inr", "rs.": "inr", "rupees": "inr", "rupee": "inr", "dollars": "usd",
             "us": "usd", "lakhcrore": "lakh crore"}
    scale, rest = 1.0, []
    for tok in u.replace("(", " ").replace(")", " ").split():
        tok = alias.get(tok, tok)
        if tok in SCALE:
            scale *= SCALE[tok]
        elif tok not in rest:
            rest.append(tok)
    return value * scale, " ".join(rest)


STOP = {"what", "was", "the", "for", "and", "in", "of", "how", "many", "which", "with", "its",
         "did", "were", "according", "total", "rupees", "per", "from"}
# reports word the same metric differently; a chunk using any synonym is a candidate
SYNONYMS = [
    ("revenue", "income from operations", "revenue from operations", "turnover", "total income",
     "net sales", "sales"),
    ("net profit", "net income", "profit for the year", "profit after tax", "pat"),
    ("production", "output", "produced"),
    ("population", "persons"),
    ("arpu", "average revenue per user"),
    ("sales", "volumes", "units sold", "dispatches"),
    ("generation", "generated", "billion units", "bu"),
]


def period_variants(question: str) -> set[str]:
    """'FY2023-24' -> {'2023-24', 'fy24', 'fy2024', 'fy 2023-24', '2024', 'fy2023-24'}."""
    out = set()
    for a, b in re.findall(r"(20\d{2})\s*[-–/]\s*(\d{2,4})", question):
        end = b[-2:]
        out |= {f"{a}-{end}", f"fy{end}", f"fy20{end}", f"fy {a}-{end}", f"fy{a}-{end}",
                f"20{end}"}
    out |= set(re.findall(r"(?:19|20)\d{2}", question))
    return {norm(x) for x in out}


def chunks(text: str, question: str) -> list[str]:
    """Most relevant chunks: distinct question terms, metric synonyms, the period asked,
    and some weight for numbers (the answer is a number)."""
    if len(text) <= CHUNK:
        return [text]
    q = norm(question)
    terms = {w for w in re.findall(r"[a-z0-9]{3,}", q)} - STOP
    phrases = {p for group in SYNONYMS if any(s in q for s in group) for p in group}
    periods = period_variants(question)
    pieces = [text[i:i + CHUNK] for i in range(0, len(text), CHUNK - OVERLAP)]

    def score(p: str) -> float:
        n = norm(p)
        words = set(re.findall(r"[a-z0-9]{3,}", n))
        return (len(words & terms) + 3 * sum(ph in n for ph in phrases)
                + 3 * sum(pv in n for pv in periods) + min(len(numbers_in(p)), 40) / 20)
    return sorted(pieces, key=score, reverse=True)[:TOP_CHUNKS]


_YEAR = re.compile(r"(?:fy\s?)?((?:19|20)\d{2})(?:\s*[-–/]\s*(\d{2,4}))?|fy\s?(\d{2})(?!\d)",
                   re.I)


def header_years(header: str) -> list[str]:
    """Column years in order: 'FY 2024', '2023-24', 'March 2024', 'FY24' -> '2024'."""
    out = []
    for m in _YEAR.finditer(unicodedata.normalize("NFKC", header)):
        if m.group(3):
            out.append("20" + m.group(3))
        elif m.group(2):
            out.append(m.group(2) if len(m.group(2)) == 4 else m.group(1)[:2] + m.group(2)[-2:])
        else:
            out.append(m.group(1))
    return out


def is_table_like(line: str, min_gaps: int = 2) -> bool:
    """Column layout: pipe-separated cells or wide (2+ space) gaps, and not a long sentence.
    Rows need 2 gaps (label + 2 values); a 2-column header needs only 1."""
    return bool(line) and len(line) <= 400 and (
        line.count("|") >= 2 or len(re.findall(r"\S {2,}\S|\t", line)) >= min_gaps)


def find_header(text: str, quote: str) -> str:
    """Nearest line above the quoted row that names >= 2 years, copied from the page."""
    words = re.findall(r"\S+", quote)[:8]
    if not words:
        return ""
    m = re.search(r"\s+".join(map(re.escape, words)), text)
    if not m:
        return ""
    for line in reversed(text[max(0, m.start() - 4000):m.start()].splitlines()):
        if len(header_years(line)) >= 2 and is_table_like(line.strip(), 1):
            return line.strip()
    return ""


def expand_table_fact(f: PageFact) -> list[PageFact]:
    """A multi-year table row + its verified header -> one fact per year, aligned by position
    from the right (leading extra cells are TTM/latest columns). Small models often pick the
    wrong column; this removes that choice. No expansion if the counts don't line up.
    A multi-value row whose header can't be verified keeps no period: the model's column
    pick is a guess (it labelled an FY26 cell FY2023-24)."""
    values = numbers_in(re.sub(r"\((?:[ivxl+\s]+)\)", " ", f.quote, flags=re.I))  # drop (I+II)
    unverified = not f.header or norm(f.header) == norm(f.quote)
    if is_table_like(f.quote) and len(values) >= 2 and unverified:
        return [f.model_copy(update={"period": ""})]
    if not (is_table_like(f.quote) and is_table_like(f.header, 1)) or unverified:
        return [f]
    years = header_years(f.header)
    if len(years) < 2 or not 0 <= len(values) - len(years) <= 1:  # at most one extra (TTM)
        return [f]
    by_year: dict[str, set[float]] = {}
    for y, v in zip(years, values[-len(years):], strict=True):
        by_year.setdefault(y, set()).add(v)
    if any(len(vs) > 1 for vs in by_year.values()):  # e.g. quarter and year columns
        return [f]                                   # share a year: ambiguous, don't guess
    return [f.model_copy(update={"value": v, "period": y,
                                 "claim": f"{f.metric} for {y}: {v} {f.unit} ({f.entity})"})
            for y, (v,) in by_year.items()]


def extract_facts(llm: LLM, page: Page, question: str,
                  role: str = "extractor") -> tuple[list[PageFact], int]:
    """Returns (validated facts, number rejected by validation)."""
    kept, rejected = [], 0
    text = collapse_cells(page.text)
    for chunk in chunks(text, question):
        msgs = [{"role": "system", "content": EXTRACT_SYS},
                {"role": "user", "content": f"QUESTION: {question}\nSOURCE: {page.title} "
                                            f"({page.url})\n\nEXCERPT:\n{chunk}"}]
        try:
            found = llm.structured(role, msgs, PageFacts).facts
        except StructuredOutputError:
            continue  # one unreadable chunk shouldn't sink the page (or the run)
        for f in found:
            ok = quote_in_text(f.quote, text) and (
                f.value is None or value_in_quote(f.value, f.quote))
            if not ok:
                rejected += 1
                continue
            if f.header and not quote_in_text(f.header, text):
                f.header = ""  # unverifiable context is dropped, not trusted
            if not f.header and is_table_like(f.quote):
                f.header = find_header(text, f.quote)
            for g in expand_table_fact(f):
                if not any(norm(g.quote) == norm(k.quote) and g.value == k.value
                           and g.period == k.period for k in kept):
                    kept.append(g)
    return kept, rejected
