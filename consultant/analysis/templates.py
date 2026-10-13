"""Analysis templates: small, pure, known-answer-tested functions the Analyst calls inside the
sandbox. Every number in a deliverable comes from code like this, never from prose."""

from collections.abc import Callable, Sequence
from itertools import product
from math import prod


# ---- market sizing ------------------------------------------------------------------------
def top_down(total_market: float, *shares: float) -> float:
    """Total market x successive filters (e.g. urban share, target-income share, penetration)."""
    return total_market * prod(shares)


def bottom_up(units: float, price: float, frequency_per_year: float = 1.0) -> float:
    """Customers/units x price x purchase frequency."""
    return units * price * frequency_per_year


# ---- break-even & unit economics ----------------------------------------------------------
def break_even_units(fixed_costs: float, price: float, variable_cost: float) -> float:
    margin = price - variable_cost
    if margin <= 0:
        raise ValueError("price must exceed variable cost to break even")
    return fixed_costs / margin


def break_even_revenue(fixed_costs: float, contribution_margin_ratio: float) -> float:
    if not 0 < contribution_margin_ratio <= 1:
        raise ValueError("contribution margin ratio must be in (0, 1]")
    return fixed_costs / contribution_margin_ratio


def unit_economics(price: float, variable_cost: float, cac: float, orders_per_year: float,
                   years: float, churn_per_year: float = 0.0) -> dict:
    """Contribution per order, simple LTV (with churn-adjusted lifetime cap), LTV/CAC,
    CAC payback in years."""
    contribution = price - variable_cost
    annual = contribution * orders_per_year
    lifetime = min(years, 1 / churn_per_year) if churn_per_year > 0 else years
    ltv = annual * lifetime
    return {"contribution_per_order": contribution, "annual_contribution": annual,
            "lifetime_years": lifetime, "ltv": ltv,
            "ltv_to_cac": ltv / cac if cac else float("inf"),
            "cac_payback_years": cac / annual if annual > 0 else float("inf")}


# ---- price-volume-mix & margin bridge ----------------------------------------------------
def price_volume_mix(base: dict[str, tuple[float, float]],
                     current: dict[str, tuple[float, float]]) -> dict:
    """Revenue change decomposed into price, volume and mix effects.
    base/current: product -> (units, price). Products must match.
    volume = (total units change) x base average price
    mix    = sum(current units x base price) - total current units x base avg price
    price  = sum(current units x (current price - base price))"""
    if set(base) != set(current):
        raise ValueError("base and current must cover the same products")
    rev0 = sum(u * p for u, p in base.values())
    rev1 = sum(u * p for u, p in current.values())
    units0 = sum(u for u, _ in base.values())
    units1 = sum(u for u, _ in current.values())
    avg0 = rev0 / units0
    volume = (units1 - units0) * avg0
    mix = sum(current[k][0] * base[k][1] for k in base) - units1 * avg0
    price = sum(current[k][0] * (current[k][1] - base[k][1]) for k in base)
    return {"base_revenue": rev0, "current_revenue": rev1, "volume": volume, "mix": mix,
            "price": price, "check": rev0 + volume + mix + price - rev1}


def margin_bridge(base: dict[str, float], current: dict[str, float]) -> dict:
    """Walk from base to current profit line by line. Dicts: line -> amount, with revenue
    positive and costs negative (so profit = sum of lines)."""
    lines = sorted(set(base) | set(current))
    steps = {k: current.get(k, 0.0) - base.get(k, 0.0) for k in lines}
    p0, p1 = sum(base.values()), sum(current.values())
    return {"base_profit": p0, "current_profit": p1, "steps": steps,
            "check": p0 + sum(steps.values()) - p1}


# ---- scenarios & sensitivity --------------------------------------------------------------
def sensitivity(f: Callable[..., float], base: dict[str, float],
                ranges: dict[str, Sequence[float]]) -> dict[str, list[tuple[float, float]]]:
    """One-at-a-time: for each input, f evaluated across its range with others at base."""
    return {k: [(v, f(**{**base, k: v})) for v in vals] for k, vals in ranges.items()}


def scenario_table(f: Callable[..., float], scenarios: dict[str, dict[str, float]],
                   base: dict[str, float]) -> dict[str, float]:
    return {name: f(**{**base, **overrides}) for name, overrides in scenarios.items()}


def grid(f: Callable[..., float], base: dict[str, float], a: str, a_vals: Sequence[float],
         b: str, b_vals: Sequence[float]) -> list[list[float]]:
    """Two-way sensitivity table: rows = a values, columns = b values."""
    out = {(x, y): f(**{**base, a: x, b: y}) for x, y in product(a_vals, b_vals)}
    return [[out[(x, y)] for y in b_vals] for x in a_vals]


# ---- investment appraisal -----------------------------------------------------------------
def npv(rate: float, cashflows: Sequence[float]) -> float:
    """cashflows[0] at t=0 (usually negative investment)."""
    return sum(cf / (1 + rate) ** t for t, cf in enumerate(cashflows))


def irr(cashflows: Sequence[float], lo: float = -0.99, hi: float = 10.0,
        tol: float = 1e-10) -> float:
    """Bisection on NPV; requires a sign change between lo and hi."""
    f_lo, f_hi = npv(lo, cashflows), npv(hi, cashflows)
    if f_lo * f_hi > 0:
        raise ValueError("IRR not bracketed: cash flows need a sign change")
    for _ in range(300):
        mid = (lo + hi) / 2
        f_mid = npv(mid, cashflows)
        if abs(f_mid) < tol:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def payback_period(cashflows: Sequence[float]) -> float | None:
    """Years until cumulative cash flow turns non-negative (linear within the year)."""
    cum = 0.0
    for t, cf in enumerate(cashflows):
        if cum + cf >= 0 and t > 0:
            return t - 1 + (-cum / cf if cf else 0.0)
        cum += cf
    return None


def cagr(start: float, end: float, years: float) -> float:
    return (end / start) ** (1 / years) - 1


def present_value_from_cagr(future_value: float, rate: float, years: float) -> float:
    """Back out today's size from a forecast + CAGR: PV = FV / (1 + r)^n."""
    return future_value / (1 + rate) ** years
