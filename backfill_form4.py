"""Historical backfill of Form 4 filings.

For each requested year, walks every business day's daily-index, parses all
Form 4 XMLs in parallel, and writes data/cache/form4_{year}.json plus a
combined data/cache/form4_historical.json.

Run:  python backfill_form4.py --years 2023,2024,2025 [--workers 12]
      python backfill_form4.py --combine-only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import List

from src.fetch_form4 import fetch_and_parse_filing, fetch_filings_for_day

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")


def _ensure_dir():
    os.makedirs(CACHE_DIR, exist_ok=True)


def _business_days_in_year(year: int) -> List[date]:
    out = []
    d = date(year, 1, 1)
    end = date(year + 1, 1, 1)
    while d < end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _parse_one(entry: dict) -> List[dict]:
    trades = fetch_and_parse_filing(entry)
    return [
        {
            "accession": t.accession,
            "filing_date": t.filing_date.isoformat(),
            "ticker": t.issuer_ticker,
            "issuer_cik": t.issuer_cik,
            "issuer_name": t.issuer_name,
            "insider_name": t.insider.name,
            "insider_cik": t.insider.cik,
            "role_bucket": t.insider.role_bucket,
            "officer_title": t.insider.officer_title,
            "is_director": t.insider.is_director,
            "is_officer": t.insider.is_officer,
            "is_ten_percent_owner": t.insider.is_ten_percent_owner,
            "transaction_date": t.transaction_date.isoformat(),
            "transaction_code": t.transaction_code,
            "acquired_disposed": t.acquired_disposed,
            "shares": t.shares,
            "price": t.price,
            "shares_owned_after": t.shares_owned_after,
            "dollar_value": t.dollar_value,
        }
        for t in trades
    ]


def backfill_year(year: int, workers: int) -> int:
    _ensure_dir()
    out_path = os.path.join(CACHE_DIR, f"form4_{year}.json")
    if os.path.exists(out_path):
        with open(out_path) as f:
            existing = json.load(f)
        print(f"[{year}] cached: {len(existing):,} trades", flush=True)
        return len(existing)

    days = _business_days_in_year(year)
    print(f"[{year}] {len(days)} business days to scan", flush=True)
    all_entries = []
    for i, d in enumerate(days, 1):
        entries = fetch_filings_for_day(d)
        all_entries.extend(entries)
        if i % 25 == 0:
            print(f"[{year}]   indexed {i}/{len(days)} days, {len(all_entries):,} filings so far", flush=True)
    print(f"[{year}] total: {len(all_entries):,} Form 4 filings", flush=True)

    all_trades: List[dict] = []
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_parse_one, e): e for e in all_entries}
        done = 0
        for fut in as_completed(futures):
            try:
                all_trades.extend(fut.result())
            except Exception:
                pass
            done += 1
            if done % 500 == 0:
                elapsed = time.time() - start
                rate = done / elapsed if elapsed else 0
                rem = (len(all_entries) - done) / rate if rate else 0
                print(f"[{year}]   parsed {done:,}/{len(all_entries):,}  ({rate:.1f}/s, ~{rem/60:.1f} min left)", flush=True)

    with open(out_path, "w") as f:
        json.dump(all_trades, f)
    print(f"[{year}] wrote {out_path}: {len(all_trades):,} transactions", flush=True)
    return len(all_trades)


def combine(years: List[int]) -> str:
    _ensure_dir()
    out_path = os.path.join(CACHE_DIR, "form4_historical.json")
    merged = []
    for y in years:
        p = os.path.join(CACHE_DIR, f"form4_{y}.json")
        if not os.path.exists(p):
            continue
        with open(p) as f:
            merged.extend(json.load(f))
    with open(out_path, "w") as f:
        json.dump(merged, f)
    return out_path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--years", default="2023,2024,2025")
    p.add_argument("--workers", type=int, default=12)
    p.add_argument("--combine-only", action="store_true")
    args = p.parse_args()
    years = [int(y) for y in args.years.split(",") if y.strip()]
    if not args.combine_only:
        for y in years:
            backfill_year(y, args.workers)
    path = combine(years)
    with open(path) as f:
        all_trades = json.load(f)
    print()
    print(f"=== Combined: {len(all_trades):,} transactions across {len(years)} years ===")
    if all_trades:
        codes = {}
        for t in all_trades:
            codes[t["transaction_code"]] = codes.get(t["transaction_code"], 0) + 1
        for c, n in sorted(codes.items(), key=lambda x: -x[1])[:10]:
            print(f"  code {c}: {n:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
