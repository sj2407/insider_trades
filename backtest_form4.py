"""Backtest using the historical Form 4 dataset.

Reads data/cache/form4_historical.json (produced by backfill_form4.py) and
emits data/backtest_form4.csv with per-trade returns at +30d, +90d, +180d
and to-today (direction-adjusted). Pre-warms per-ticker yfinance histories
so the backtest is fast.

Run:  python backtest_form4.py [--limit N] [--out data/backtest_form4.csv]
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from typing import Optional

from src.prices import _history, latest_price, price_on_or_after

HOUSE_PATH = "data/cache/form4_historical.json"


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


def run(limit: Optional[int], out_path: str) -> None:
    with open(HOUSE_PATH) as f:
        records = json.load(f)
    today = date.today()
    cutoff = today - timedelta(days=180)
    pool = []
    for r in records:
        # Keep open-market buys (P, A=Acquired) and sells (S, D=Disposed) only
        code = r.get("transaction_code", "")
        ad = r.get("acquired_disposed", "")
        is_buy = code == "P" and ad == "A"
        is_sell = code == "S" and ad == "D"
        if not (is_buy or is_sell):
            continue
        tx = _parse_iso(r.get("transaction_date"))
        if not tx or tx > cutoff:
            continue
        if not r.get("ticker"):
            continue
        r["_tx"] = tx
        r["_d"] = 1 if is_buy else -1
        r["_side"] = "BUY" if is_buy else "SELL"
        pool.append(r)
    if limit:
        pool = pool[:limit]

    unique = sorted({r["ticker"] for r in pool})
    print(f"Backtesting {len(pool):,} insider trades across {len(unique):,} unique tickers", flush=True)
    for i, tk in enumerate(unique, 1):
        _history(tk)
        if i % 200 == 0:
            print(f"  prefetched {i}/{len(unique)} tickers", flush=True)

    fieldnames = [
        "accession", "ticker", "insider_name", "role_bucket", "officer_title",
        "transaction_date", "filing_date", "lag_days",
        "side", "direction", "shares", "price_at_trade", "dollar_value",
        "price_30d", "price_90d", "price_180d", "price_today",
        "ret_30d_pct", "ret_90d_pct", "ret_180d_pct", "ret_to_today_pct",
        "years_held", "annualized_pct",
    ]

    sum_30, sum_90, sum_180, n_v = 0.0, 0.0, 0.0, 0
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(pool, 1):
            tx = r["_tx"]
            d = r["_d"]
            tk = r["ticker"]
            # Use yfinance's split-adjusted close as p_trade so it's consistent
            # with the +30/+90/+180 prices (also yfinance). Mixing the raw price
            # field from Finnhub with adjusted future prices produced spurious
            # "returns" measured in millions of percent on split stocks.
            p_trade = price_on_or_after(tk, tx)
            p_30 = price_on_or_after(tk, tx + timedelta(days=30))
            p_90 = price_on_or_after(tk, tx + timedelta(days=90))
            p_180 = price_on_or_after(tk, tx + timedelta(days=180))
            p_today = latest_price(tk)
            fd = _parse_iso(r.get("filing_date"))
            lag = (fd - tx).days if (fd and tx) else None

            def pct(a, b):
                if a is None or b is None or a == 0:
                    return None
                return round(d * (b - a) / a * 100, 3)

            ret_30 = pct(p_trade, p_30)
            ret_90 = pct(p_trade, p_90)
            ret_180 = pct(p_trade, p_180)
            ret_today = pct(p_trade, p_today)

            years = (today - tx).days / 365.25
            ann = None
            if ret_today is not None and years > 0.083:  # >1 month
                # Compound annualization (direction-adjusted: positive ret means right call)
                tot = ret_today / 100
                ann = ((1 + tot) ** (1 / years) - 1) * 100 if (1 + tot) > 0 else None
                if ann is not None:
                    ann = round(ann, 3)

            if ret_30 is not None and ret_90 is not None and ret_180 is not None:
                sum_30 += ret_30
                sum_90 += ret_90
                sum_180 += ret_180
                n_v += 1

            w.writerow({
                "accession": r["accession"],
                "ticker": tk,
                "insider_name": r["insider_name"],
                "role_bucket": r["role_bucket"],
                "officer_title": r.get("officer_title") or "",
                "transaction_date": tx.isoformat(),
                "filing_date": r.get("filing_date") or "",
                "lag_days": lag,
                "side": r["_side"],
                "direction": d,
                "shares": r.get("shares"),
                "price_at_trade": p_trade,
                "dollar_value": r.get("dollar_value"),
                "price_30d": p_30,
                "price_90d": p_90,
                "price_180d": p_180,
                "price_today": p_today,
                "ret_30d_pct": ret_30,
                "ret_90d_pct": ret_90,
                "ret_180d_pct": ret_180,
                "ret_to_today_pct": ret_today,
                "years_held": round(years, 3),
                "annualized_pct": ann,
            })
            if i % 1000 == 0:
                print(f"  ...{i:,}/{len(pool):,}", flush=True)
    print(f"Wrote {out_path}")
    if n_v:
        print(f"Summary on {n_v:,} comparable trades (direction-adjusted):")
        print(f"  +30d : {sum_30/n_v:+.2f}%")
        print(f"  +90d : {sum_90/n_v:+.2f}%")
        print(f"  +180d: {sum_180/n_v:+.2f}%")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default="data/backtest_form4.csv")
    args = p.parse_args()
    if not os.path.exists(HOUSE_PATH):
        print(f"ERROR: {HOUSE_PATH} not found. Run backfill_form4.py first.", file=sys.stderr)
        return 1
    run(args.limit, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
