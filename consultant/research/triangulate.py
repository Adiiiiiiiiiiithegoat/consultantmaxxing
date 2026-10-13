"""Triangulation: a key number is 'confirmed' only when >= 2 independent sources (distinct
domains) agree within tolerance; disagreements are kept as a range, never silently resolved."""

import re
from dataclasses import dataclass, field
from statistics import median

from consultant.research.credibility import domain
from consultant.research.extract import to_base
from consultant.research.fact_store import Fact


@dataclass
class Finding:
    metric: str
    entity: str
    period: str
    unit: str
    value: float          # median of per-source values (base units)
    low: float
    high: float
    status: str           # confirmed | single_source | conflict
    fact_ids: list[int] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def period_key(p: str) -> str:
    """'FY2023-24', 'FY 24', '2023-24', 'fiscal 2024' -> '2024' (fiscal year end)."""
    years = re.findall(r"(?:19|20)\d{2}|(?<!\d)\d{2}(?!\d)", p)
    if not years:
        return _key(p)
    y = years[-1]
    return y if len(y) == 4 else "20" + y


def triangulate(facts: list[Fact], tol: float = 0.02) -> list[Finding]:
    groups: dict[tuple, list[tuple[Fact, float, str]]] = {}
    for f in facts:
        if f.value is None:
            continue
        v, unit = to_base(f.value, f.unit)
        groups.setdefault((_key(f.metric), _key(f.entity), period_key(f.period), unit),
                          []).append((f, v, unit))
    out = []
    for (_, _, _, unit), rows in groups.items():
        best: dict[str, tuple[Fact, float]] = {}  # most credible fact per domain
        for f, v, _ in rows:
            d = domain(f.source_url)
            if d not in best or f.credibility > best[d][0].credibility:
                best[d] = (f, v)
        vals = [v for _, v in best.values()]
        lo, hi = min(vals), max(vals)
        agree = hi - lo <= tol * max(abs(hi), 1e-9)
        status = ("confirmed" if len(best) >= 2 else "single_source") if agree else "conflict"
        f0 = rows[0][0]
        out.append(Finding(f0.metric, f0.entity, f0.period, unit, median(vals), lo, hi, status,
                           [f.id for f, _ in best.values() if f.id is not None], list(best)))
    return out
