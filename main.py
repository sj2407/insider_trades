"""Daily orchestrator for the insider-trades alert.

Behavior:
1. Fetch Form 4 filings for the lookback window (default last 2 business days)
2. Parse, score, enrich with prices + market-cap
3. Drop trades on tickers under MIN_MARKET_CAP (Russell-3000-ish filter)
4. Drop "noise" severity (option exercises, awards, gifts, tax-payment sales)
5. Diff against SQLite seen-store
6. Render email, mark seen

Run modes:
  python main.py                  — production run
  python main.py --dry-run        — writes data/preview.html instead of sending
  python main.py --preview-recent 30days  — show last 30 days of activity for inspection
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from typing import List

from dotenv import load_dotenv

from src.fetch_form4 import fetch_and_parse_filing, fetch_filings_for_day
from src.notify import render_email_html, send_email
from src.prices import latest_price, latest_price_date, lookup_ticker_meta
from src.scoring import apply_scoring
from src.store import filter_new, mark_seen, store_is_empty
from src.types import Form4Trade


def _business_days_back(n: int) -> List[date]:
    out, d = [], date.today()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def _fetch_window(days_back: int, max_per_day: int | None = None) -> List[Form4Trade]:
    trades = []
    for d in _business_days_back(days_back):
        entries = fetch_filings_for_day(d)
        if max_per_day:
            entries = entries[:max_per_day]
        print(f"  {d.isoformat()}: {len(entries)} Form 4 filings", flush=True)
        for i, e in enumerate(entries, 1):
            trades.extend(fetch_and_parse_filing(e))
            if i % 100 == 0:
                print(f"    parsed {i}/{len(entries)}", flush=True)
    return trades


def _enrich(trades: List[Form4Trade]) -> None:
    """Attach sector + market cap + latest price to each trade."""
    print(f"  enriching {len(trades)} trades with ticker metadata + prices…", flush=True)
    for t in trades:
        meta = lookup_ticker_meta(t.issuer_ticker)
        t.sector = meta.get("sector", "")
        t.industry = meta.get("industry", "")
        t.market_cap = meta.get("market_cap")
        t.price_now = latest_price(t.issuer_ticker)
        t.price_now_date = latest_price_date(t.issuer_ticker)


def run(lookback_days: int, dry_run: bool, preview_days: int, min_market_cap: float,
        max_per_day: int | None = None) -> int:
    bootstrap = store_is_empty()
    if bootstrap and not preview_days:
        print("BOOTSTRAP: seen-store empty. Marking everything seen, no email this run.", flush=True)

    days = preview_days if preview_days else lookback_days
    print(f"Fetching Form 4 filings for the last {days} business day(s)…", flush=True)
    trades = _fetch_window(days, max_per_day=max_per_day)
    print(f"  parsed: {len(trades)} transactions", flush=True)

    # Filter out noise codes and zero-value/zero-shares rows
    trades = [t for t in trades if not t.is_noise]
    print(f"  open-market only (drop noise): {len(trades)}", flush=True)

    # Score (sets severity, populates cluster_count)
    apply_scoring(trades)

    # Enrich with prices + market cap (after scoring so we don't waste calls on noise)
    _enrich(trades)

    # Apply market cap floor (Russell-3000-ish)
    pre = len(trades)
    trades = [
        t for t in trades
        if t.market_cap is None or t.market_cap >= min_market_cap
    ]
    print(f"  market cap ≥ ${min_market_cap:,.0f}: {len(trades)} (dropped {pre - len(trades)})", flush=True)

    if bootstrap and not preview_days:
        mark_seen(trades)
        print(f"Marked {len(trades)} trades as seen. Exiting.", flush=True)
        return 0

    if preview_days:
        new_trades = trades
        print(f"  preview mode: showing all {len(new_trades)} trades", flush=True)
    else:
        new_trades = filter_new(trades)
        print(f"  new (not yet seen): {len(new_trades)}", flush=True)

    if not new_trades and not preview_days:
        print("No new disclosures — skipping email.", flush=True)
        return 0

    flagged = [t for t in new_trades if t.severity in {"high", "moderate", "low"}]
    high = sum(1 for t in new_trades if t.severity == "high")
    subject = (
        f"🏢 Insider trades — {len(new_trades)} new"
        f"{f' ({len(flagged)} flagged, {high} strong)' if flagged else ' (none flagged)'}"
        f" — {date.today().isoformat()}"
    )
    body = render_email_html(new_trades)

    if dry_run or preview_days:
        out = os.path.join(os.path.dirname(__file__), "data", "preview.html")
        with open(out, "w") as f:
            f.write(body)
        print(f"DRY RUN — email preview written to {out}", flush=True)
        return 0

    send_email(subject, body)
    mark_seen(new_trades)
    print(f"Sent email with {len(new_trades)} trades.", flush=True)
    return 0


def main() -> int:
    load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--lookback-days", type=int, default=2,
                   help="Business days back to scan (default 2 — covers a missed run)")
    p.add_argument("--preview-recent", type=int, default=0, metavar="DAYS",
                   help="Bypass seen-filter and show transactions from the last N business days. Implies --dry-run.")
    p.add_argument("--min-market-cap", type=float, default=None,
                   help="Override MIN_MARKET_CAP env var")
    p.add_argument("--max-per-day", type=int, default=None,
                   help="Cap how many filings per day are fetched (for testing)")
    args = p.parse_args()
    min_cap = args.min_market_cap if args.min_market_cap is not None else float(os.environ.get("MIN_MARKET_CAP", "500000000"))
    return run(args.lookback_days, args.dry_run, args.preview_recent, min_cap, args.max_per_day)


if __name__ == "__main__":
    sys.exit(main())
