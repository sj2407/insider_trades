"""Severity scoring for Form 4 trades.

Replaces the committee-conflict matrix from congress_trades. Insider trading is
about HOW MEANINGFUL the trade is (the role of the filer, the size, whether it
was open-market vs option exercise, and whether multiple insiders are buying
the same company at once — i.e. a "cluster buy").

Rules (in priority order, first match wins for severity but reasons accumulate):

  🔴 high
    - Open-market BUY by CEO/President/CFO/COO
    - Cluster: 2+ different insiders making open-market buys in the same company within 30 days
    - Any open-market BUY ≥ $250k (any role)

  🟠 moderate (some)
    - Open-market BUY by Director, other officer, or 10%-owner
    - Open-market BUY ≥ $50k (any role)
    - Open-market SELL by C-suite ≥ $1M

  🟡 low (weak)
    - Open-market SELL by C-suite (any size below $1M)
    - Open-market BUY < $50k

  ⚪ none
    - Open-market SELL by Director / other officer / 10%-owner
    - Small open-market BUY < $25k

  noise (filtered out — not emailed)
    - Option exercises (M), tax-payment sales (F), gifts (G), inheritance (W),
      automatic dividend reinvestment (I/K/U/X), awards (A), voluntary disclosure (Z)
"""
from __future__ import annotations

from datetime import timedelta
from typing import Iterable, List

from .types import Form4Trade

HIGH_BUY_USD = 250_000
SOME_BUY_USD = 50_000
LOW_BUY_USD = 25_000
HIGH_SELL_USD = 1_000_000
CLUSTER_DAYS = 30
CLUSTER_THRESHOLD = 2  # ≥ this many distinct insiders in window → cluster


def _classify_single(t: Form4Trade) -> tuple[str, List[str]]:
    reasons: List[str] = []
    if t.is_noise:
        return "noise", [f"transaction code {t.transaction_code} (not open-market)"]

    dv = t.dollar_value
    role = t.insider.role_bucket
    is_csuite = t.insider.is_c_suite

    if t.is_open_market_buy:
        if is_csuite:
            reasons.append(f"open-market buy by {role}")
            sev = "high"
        else:
            reasons.append(f"open-market buy by {role}")
            sev = "moderate"
        if dv is not None:
            if dv >= HIGH_BUY_USD:
                reasons.append(f"size ${dv:,.0f} ≥ ${HIGH_BUY_USD:,}")
                sev = "high"
            elif dv >= SOME_BUY_USD and sev == "low":
                sev = "moderate"
            elif dv < LOW_BUY_USD:
                reasons.append(f"size ${dv:,.0f} < ${LOW_BUY_USD:,}")
                if sev != "high":
                    sev = "none"
        return sev, reasons

    if t.is_open_market_sell:
        if is_csuite:
            if dv is not None and dv >= HIGH_SELL_USD:
                reasons.append(f"large C-suite sell ${dv:,.0f}")
                return "moderate", reasons
            reasons.append(f"open-market sell by {role}")
            return "low", reasons
        reasons.append(f"open-market sell by {role}")
        return "none", reasons

    # Other codes that aren't classified as noise (rare)
    return "low", [f"unusual code {t.transaction_code}"]


def apply_scoring(trades: Iterable[Form4Trade]) -> List[Form4Trade]:
    """Mutates each trade's .severity, .reasons, .cluster_count and returns the list.

    Cluster detection: for each (issuer, transaction_date), count how many
    distinct insider CIKs made an open-market BUY within ±CLUSTER_DAYS.
    """
    trades_list = list(trades)
    # First pass: per-trade severity
    for t in trades_list:
        sev, reasons = _classify_single(t)
        t.severity = sev
        t.reasons = reasons

    # Cluster detection across the whole input list
    # Map issuer_ticker -> [(date, insider_cik) for open-market buys only]
    buys_by_issuer: dict[str, list] = {}
    for t in trades_list:
        if t.is_open_market_buy:
            buys_by_issuer.setdefault(t.issuer_ticker, []).append((t.transaction_date, t.insider.cik, t))
    for issuer, buys in buys_by_issuer.items():
        for d, cik, t in buys:
            window = [b for b in buys if abs((b[0] - d).days) <= CLUSTER_DAYS]
            distinct_insiders = {b[1] for b in window}
            t.cluster_count = len(distinct_insiders)
            if t.cluster_count >= CLUSTER_THRESHOLD and t.severity != "noise":
                t.reasons.append(f"cluster: {t.cluster_count} distinct insiders buying within {CLUSTER_DAYS}d")
                t.severity = "high"
    return trades_list


SEVERITY_RANK = {"high": 0, "moderate": 1, "low": 2, "none": 3, "noise": 4}
