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
from src.scoring import SEVERITY_RANK, apply_scoring
from src.store import filter_new, mark_seen, store_is_empty
from src.types import Form4Trade


UNTRADABLE_TICKERS = {"NONE", "N/A", "NA", ""}
MIN_CSUITE_SELL_USD = float(os.environ.get("MIN_CSUITE_SELL_USD", "1000000"))
MAX_EMAIL_ROWS = int(os.environ.get("MAX_EMAIL_ROWS", "300"))
DEFAULT_EMAIL_SEVERITY = os.environ.get("EMAIL_SEVERITY_MIN", "low")


def _normalize_ticker(t: Form4Trade) -> None:
    sym = t.issuer_ticker.upper().strip()
    if "/" in sym:
        parts = [p.strip() for p in sym.split("/") if p.strip()]
        us = [p for p in parts if p.isalpha() and len(p) <= 5]
        sym = us[-1] if us else parts[-1]
    t.issuer_ticker = sym


def _keep_at_funnel_top(t: Form4Trade) -> bool:
    _normalize_ticker(t)
    if t.issuer_ticker in UNTRADABLE_TICKERS:
        return False
    if t.insider.is_c_suite:
        return True
    if t.insider.is_ten_percent_owner and t.is_open_market_buy:
        return True
    return False


def _collapse_multi_entity_dupes(trades):
    seen, out = set(), []
    for t in trades:
        k = (t.issuer_ticker, t.transaction_date, t.shares, t.price, t.transaction_code)
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _drop_small_csuite_sells(trades, min_usd: float):
    if min_usd <= 0:
        return trades
    return [
        t for t in trades
        if not (t.insider.is_c_suite and t.is_open_market_sell and (t.dollar_value or 0) < min_usd)
    ]


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
        kept = dropped = 0
        for i, e in enumerate(entries, 1):
            for t in fetch_and_parse_filing(e):
                if _keep_at_funnel_top(t):
                    trades.append(t)
                    kept += 1
                else:
                    dropped += 1
            if i % 200 == 0:
                print(f"    parsed {i}/{len(entries)}  kept={kept}", flush=True)
        print(f"  {d.isoformat()}: kept {kept} (dropped {dropped} non-signal)", flush=True)
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
    print(f"  parsed: {len(trades)} signal-bearing transactions", flush=True)

    pre = len(trades)
    trades = _collapse_multi_entity_dupes(trades)
    print(f"  after collapsing multi-entity dupes: {len(trades)} (cut {pre - len(trades)})", flush=True)

    trades = [t for t in trades if not t.is_noise]
    print(f"  open-market only (drop noise): {len(trades)}", flush=True)

    pre = len(trades)
    trades = _drop_small_csuite_sells(trades, MIN_CSUITE_SELL_USD)
    print(f"  after dropping C-suite sells < ${MIN_CSUITE_SELL_USD:,.0f}: {len(trades)} (cut {pre - len(trades)})", flush=True)

    apply_scoring(trades)
    _enrich(trades)

    # Public companies only: require a market cap and apply floor
    pre = len(trades)
    trades = [t for t in trades if t.market_cap is not None and t.market_cap >= min_market_cap]
    print(f"  public + market cap ≥ ${min_market_cap:,.0f}: {len(trades)} (dropped {pre - len(trades)})", flush=True)

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

    threshold = SEVERITY_RANK.get(DEFAULT_EMAIL_SEVERITY, SEVERITY_RANK["low"])
    emailable = [t for t in new_trades if SEVERITY_RANK.get(t.severity, 99) <= threshold]
    high = sum(1 for t in new_trades if t.severity == "high")
    moderate = sum(1 for t in new_trades if t.severity == "moderate")
    print(
        f"  severity high={high} moderate={moderate}  → emailable: {len(emailable)}",
        flush=True,
    )

    if not emailable and not preview_days:
        print("No emailable trades — skipping email but marking all seen.", flush=True)
        if not dry_run:
            mark_seen(new_trades)
        return 0

    flood = len(emailable) > MAX_EMAIL_ROWS
    if flood:
        print(f"  flood guard: capping {len(emailable)} → top {MAX_EMAIL_ROWS}", flush=True)
        emailable = sorted(emailable, key=lambda t: (SEVERITY_RANK.get(t.severity, 99), -(t.dollar_value or 0)))[:MAX_EMAIL_ROWS]

    bits = []
    if high: bits.append(f"{high} strong")
    if moderate: bits.append(f"{moderate} moderate")
    label = ", ".join(bits) if bits else "no flagged"
    flood_tag = f" (top {MAX_EMAIL_ROWS} of {len(new_trades)} new)" if flood else ""
    subject = f"🏢 Insider trades — {label}{flood_tag} — {date.today().isoformat()}"
    body = render_email_html(emailable)

    out = os.path.join(os.path.dirname(__file__), "data", "preview.html")
    with open(out, "w") as f:
        f.write(body)

    if dry_run or preview_days:
        # Dry-run: preview is written and state is still committed (CSV + SQLite).
        # The only thing skipped is the email send.
        if not preview_days:
            mark_seen(new_trades)
        print(f"DRY RUN — preview at {out} · state committed · email NOT sent", flush=True)
        return 0

    send_email(subject, body)
    mark_seen(new_trades)
    print(f"Sent email with {len(emailable)} trades; marked {len(new_trades)} seen + archived.", flush=True)
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
