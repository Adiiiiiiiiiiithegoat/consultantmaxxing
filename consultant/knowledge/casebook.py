"""Segment the casebook PDF into raw cases and industry reports (deterministic, no LLM).

Case start pages come from the contents table; a case runs until the next case or section
start. Section starts come from the PDF outline. Speakers are marked by colour in the PDF:
12pt white text is the interviewer, 12pt black the candidate; other text (the summary slide:
structure, notes, takeaways) is kept as notes. Output lives in data/derived/ (gitignored).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from consultant.config import ROOT

CASEBOOK = ROOT / "data/private/iimb_casebook_2024-25.pdf"
_DIFF = "Easy|Moderate|Challenging"
_ROW = re.compile(rf"\|(\d+)\|(.+?)\|({_DIFF})\|(.+?)\|(\d+)(?=\|)")
_FOOTER = re.compile(rf"^(.+?)\s*\|\s*({_DIFF})\s*\|\s*(.+?)\s*$", re.M)
SECTION_TYPES = {
    "Profitability": "profitability", "Market Entry Strategy": "market_entry",
    "Pricing Strategy": "pricing", "Growth Strategy": "growth", "M&A & PE Cases": "ma_pe",
    "Unconventional": "unconventional", "Guesstimates": "guesstimate",
}
FOOTER_TYPES = {"profitability": "profitability", "market entry": "market_entry",
                "pricing": "pricing", "growth": "growth", "m&a": "ma_pe", "pe": "ma_pe",
                "private equity": "ma_pe", "unconventional": "unconventional",
                "guesstimate": "guesstimate"}


@dataclass
class RawCase:
    id: str
    title: str
    difficulty: str
    firm: str
    case_type: str
    pages: list[int]
    turns: list[tuple[str, str]] = field(repr=False)  # ("interviewer" | "candidate", text)
    notes: str = field(repr=False)

    @property
    def text(self) -> str:
        dialog = "\n".join(f"[{i}] {who.upper()}: {t}" for i, (who, t) in enumerate(self.turns))
        return f"{dialog}\n\nSUMMARY SLIDE NOTES:\n{self.notes}"


@dataclass
class RawReport:
    industry: str
    page: int
    text: str = field(repr=False)


def _clean(page_text: str) -> str:
    lines = [ln.rstrip() for ln in page_text.splitlines()]
    return "\n".join(ln for ln in lines if ln.strip() and not ln.strip().startswith("ICON, IIM")
                     and not ln.strip().isdigit())


def _title(cell: str) -> str:
    """Contents cells can drag in earlier tokens (S.No, section rows); keep what follows the
    last bare number."""
    toks = cell.split("|")
    last_num = max((i for i, t in enumerate(toks) if t.strip().isdigit()), default=-1)
    return "".join(toks[last_num + 1:])


SPEAKER = {0xFFFFFF: "interviewer", 0x000000: "candidate"}


def _dialog(doc, span: list[int]) -> tuple[list[tuple[str, str]], str]:
    turns: list[list[str]] = []
    notes = []
    for pno in span:
        page = doc[pno - 1]
        if "Key Takeaways" in page.get_text() and "Structure" in page.get_text():
            notes.append(page.get_text())  # the case's summary slide
            continue
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(sp["text"] for sp in line["spans"]).strip()
                if not text or text.startswith("ICON, IIM") or _FOOTER.match(text):
                    continue
                first = line["spans"][0]
                size = round(first["size"])
                who = SPEAKER.get(first["color"]) if size == 12 else None
                if who is None:
                    if size < 18:  # 18+ is the footer title / slide headings
                        notes.append(text)
                elif turns and turns[-1][0] == who:
                    turns[-1][1] += " " + text
                else:
                    turns.append([who, text])
    return [(w, " ".join(t.split())) for w, t in turns], "\n".join(notes)


def _footer_type(text: str) -> str | None:
    m = _FOOTER.search(text)
    if not m:
        return None
    label = m[1].strip().lower()
    return next((v for k, v in FOOTER_TYPES.items() if label.startswith(k)), None)


def segment(pdf: Path = CASEBOOK) -> tuple[list[RawCase], list[RawReport]]:
    doc = pymupdf.open(pdf)
    pages = [p.get_text() for p in doc]  # index i = page i+1
    sections = [(t.strip(), p) for lvl, t, p in doc.get_toc() if lvl == 1]
    contents = "|" + "|".join(ln for p in pages[4:7] for ln in p.splitlines()) + "|"
    rows = [(_title(m[2]), m[3], m[4].replace("|", ""), int(m[5])) for m in _ROW.finditer(contents)]
    report_toc = "|".join(pages[6].splitlines()) + "|"
    report_names = {int(m[2]): m[1].strip()
                    for m in re.finditer(r"\|\d+\.\|([^|]+)\|(\d+)(?=\|)", report_toc)}
    reports_start = next(p for t, p in sections if t == "Industry Reports")
    starts = sorted({p for *_, p in rows} | {p for _, p in sections} | {reports_start + 1})

    cases = []
    for title, diff, firm, start in sorted(rows, key=lambda r: r[3]):
        end = next(s for s in starts if s > start)  # exclusive
        span = list(range(start, end))
        section = max((s for s in sections if s[1] <= start), key=lambda s: s[1])[0]
        ctype = SECTION_TYPES.get(section) or _footer_type(pages[start - 1]) or "unconventional"
        if foot := _FOOTER.search(pages[start - 1]):  # the case's own footer beats contents
            diff, firm = foot[2], foot[3]
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        turns, notes = _dialog(doc, span)
        cases.append(RawCase(f"iimb-p{start:03d}-{slug}", " ".join(title.split()),
                             diff.lower(), " ".join(firm.split()), ctype, span, turns, notes))

    reports = []
    for page, name in sorted(report_names.items()):
        reports.append(RawReport(" ".join(name.split()), page, _clean(pages[page - 1])))
    return cases, reports


if __name__ == "__main__":
    cs, rs = segment()
    for c in cs:
        print(c.pages[0], len(c.pages), c.case_type, c.difficulty, c.firm, "|", c.title)
    print(len(cs), "cases;", len(rs), "reports:", [r.industry for r in rs])
