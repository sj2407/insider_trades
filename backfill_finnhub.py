"""Historical Form 4 backfill via Finnhub's pre-aggregated insider-transactions endpoint.

Much faster than scraping EDGAR daily indexes: 60 req/min on the free tier,
and one request per ticker returns all transactions across the time range.

Output: data/cache/finnhub_form4_historical.json with the same shape as
the EDGAR backfill, so the same backtest_form4.py works against it.

Run:  python backfill_finnhub.py [--from 2023-01-01] [--to 2025-12-31]
                                 [--universe russell3000.txt | --polygon-universe]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from typing import List, Optional

import requests

CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "cache")


def _finnhub_key() -> str:
    k = os.environ.get("FINNHUB_API_KEY")
    if not k:
        sys.exit("ERROR: FINNHUB_API_KEY not set in env")
    return k


def _polygon_key() -> Optional[str]:
    return os.environ.get("POLYGON_API_KEY") or None


def load_universe(path: Optional[str], use_polygon: bool, min_market_cap: float) -> List[str]:
    """Return list of tickers. Priority:
    1. --universe path (newline-delimited tickers)
    2. --polygon-universe (Polygon /v3/reference/tickers, filter to active stocks > min_market_cap)
    3. fallback to the SEC company_tickers.json (no market cap)
    """
    if path and os.path.exists(path):
        with open(path) as f:
            return [t.strip().upper() for t in f if t.strip()]
    if use_polygon:
        key = _polygon_key()
        if not key:
            sys.exit("ERROR: POLYGON_API_KEY not set, can't build universe")
        tickers = []
        url = f"https://api.polygon.io/v3/reference/tickers?market=stocks&active=true&limit=1000&apiKey={key}"
        while url:
            r = requests.get(url, timeout=30)
            d = r.json()
            for t in d.get("results", []):
                if t.get("market") != "stocks":
                    continue
                # Polygon's reference endpoint may not include market_cap on the
                # list view — we filter post-hoc per ticker if needed.
                tickers.append(t.get("ticker", "").upper())
            next_url = d.get("next_url")
            url = (next_url + f"&apiKey={key}") if next_url else None
            time.sleep(0.05)
        return [t for t in tickers if t]
    # SEC fallback
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers={"User-Agent": os.environ.get("SEC_USER_AGENT", "research insider@x.com")},
                     timeout=30)
    data = r.json()
    return list({v["ticker"].upper() for v in data.values()})


def fetch_finnhub_insiders(ticker: str, frm: str, to: str) -> List[dict]:
    """One request per ticker. Returns list of normalized records."""
    key = _finnhub_key()
    url = f"https://finnhub.io/api/v1/stock/insider-transactions?symbol={ticker}&from={frm}&to={to}&token={key}"
    try:
        r = requests.get(url, timeout=20)
        if r.status_code != 200:
            return []
        data = r.json().get("data") or []
    except Exception:
        return []

    out = []
    for x in data:
        # Finnhub fields:
        #   name, share, change, filingDate, transactionDate, transactionCode,
        #   transactionPrice, symbol, position (a free-text title)
        out.append({
            "ticker": ticker,
            "insider_name": x.get("name") or "",
            "officer_title": x.get("position") or "",
            "filing_date": x.get("filingDate") or "",
            "transaction_date": x.get("transactionDate") or "",
            "transaction_code": x.get("transactionCode") or "",
            "shares": x.get("share"),
            "share_change": x.get("change"),
            "price": x.get("transactionPrice"),
            # Derive role bucket from the title text
            "role_bucket": _classify_role(x.get("position") or ""),
            "acquired_disposed": "A" if (x.get("change") or 0) > 0 else "D",
            "dollar_value": (
                abs(x.get("share") or 0) * (x.get("transactionPrice") or 0)
                if x.get("share") and x.get("transactionPrice") else None
            ),
            "accession": x.get("filingDate", "") + "_" + ticker + "_" + (x.get("name") or "")[:20],
            "is_director": "director" in (x.get("position") or "").lower(),
            "is_officer": any(k in (x.get("position") or "").upper() for k in ["CEO","CFO","COO","PRESIDENT","CHIEF","OFFICER","VP","VICE PRESIDENT"]),
            "is_ten_percent_owner": "10%" in (x.get("position") or ""),
        })
    return out


def _classify_role(title: str) -> str:
    t = title.upper()
    if any(k in t for k in ("CEO", "CHIEF EXECUTIVE", "PRESIDENT & CEO")):
        return "CEO/President"
    if any(k in t for k in ("CFO", "CHIEF FINANCIAL", "TREASURER")):
        return "CFO"
    if any(k in t for k in ("COO", "CHIEF OPERATING")):
        return "COO"
    if "CHIEF" in t:
        return "Other C-suite"
    if any(k in t for k in ("DIRECTOR",)):
        return "Director"
    if "10%" in t:
        return "10% owner"
    if any(k in t for k in ("OFFICER", "VP", "VICE PRESIDENT", "PRESIDENT")):
        return "Other officer"
    return "Other"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", default="2023-01-01")
    p.add_argument("--to", default=date.today().isoformat())
    p.add_argument("--universe", default=None,
                   help="Path to newline-delimited ticker file")
    p.add_argument("--polygon-universe", action="store_true",
                   help="Use Polygon to enumerate all active US stocks")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap on tickers (for testing)")
    p.add_argument("--out", default=os.path.join(CACHE_DIR, "finnhub_form4_historical.json"))
    args = p.parse_args()

    os.makedirs(CACHE_DIR, exist_ok=True)
    print("Loading universe…", flush=True)
    tickers = load_universe(args.universe, args.polygon_universe, 0)
    if args.limit:
        tickers = tickers[: args.limit]
    print(f"  {len(tickers):,} tickers", flush=True)

    all_records: List[dict] = []
    start = time.time()
    last_save = time.time()
    for i, tk in enumerate(tickers, 1):
        recs = fetch_finnhub_insiders(tk, args.frm, args.to)
        all_records.extend(recs)
        time.sleep(1.05)  # 60 req/min ceiling, leave headroom
        if i % 50 == 0:
            elapsed = time.time() - start
            rate = i / elapsed
            rem = (len(tickers) - i) / rate if rate else 0
            print(f"  {i:,}/{len(tickers):,} ({rate:.1f}/s, ~{rem/60:.1f} min left) — {len(all_records):,} records so far", flush=True)
        # Save every 5 min so a crash mid-run doesn't lose progress
        if time.time() - last_save > 300:
            with open(args.out + ".partial", "w") as f:
                json.dump(all_records, f)
            last_save = time.time()

    with open(args.out, "w") as f:
        json.dump(all_records, f)
    if os.path.exists(args.out + ".partial"):
        os.remove(args.out + ".partial")
    print(f"Wrote {args.out}: {len(all_records):,} transactions across {len(tickers):,} tickers")
    return 0


if __name__ == "__main__":
    sys.exit(main())
