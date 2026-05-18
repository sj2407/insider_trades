"""Rebuild today's digest email from data/trades_history.csv (no re-fetching)
and send it via SMTP. Used to add the new recap section without paying for
another SEC + yfinance pass."""
from __future__ import annotations

import csv
import os
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(__file__))

from src.notify import render_email_html, send_email
from src.types import Form4Trade, InsiderProfile

CSV_PATH = os.path.join(os.path.dirname(__file__), "data", "trades_history.csv")


def _ofloat(s):
    try:
        return float(s) if s not in ("", None) else None
    except ValueError:
        return None


def _odate(s):
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def _build_trade_from_row(r: dict) -> Form4Trade:
    role = r.get("insider_role", "")
    # Approximate the InsiderProfile flags from the role bucket
    is_csuite = role in {"CEO/President", "CFO", "COO", "Other C-suite"}
    is_dir = role == "Director"
    is_10 = role == "10% owner"
    profile = InsiderProfile(
        name=r.get("insider_name", ""),
        cik="",
        is_director=is_dir,
        is_officer=is_csuite or role == "Other officer",
        is_ten_percent_owner=is_10,
        officer_title=r.get("officer_title") or None,
    )
    shares = _ofloat(r.get("shares")) or 0.0
    price = _ofloat(r.get("price")) or 0.0
    code = r.get("transaction_code", "")
    ad = r.get("acquired_disposed", "")
    if not ad:
        ad = "A" if code == "P" else ("D" if code == "S" else "")
    t = Form4Trade(
        accession=r.get("accession", ""),
        filing_date=_odate(r.get("filing_date")) or date.today(),
        issuer_name=r.get("issuer", ""),
        issuer_ticker=r.get("ticker", ""),
        issuer_cik="",
        pdf_url=r.get("pdf_url", ""),
        insider=profile,
        transaction_date=_odate(r.get("transaction_date")) or date.today(),
        transaction_code=code,
        acquired_disposed=ad,
        shares=shares,
        price=price,
    )
    t.sector = r.get("sector", "") or None
    t.industry = r.get("industry", "") or None
    t.market_cap = _ofloat(r.get("market_cap"))
    t.price_now = _ofloat(r.get("price_now"))
    t.price_now_date = _odate(r.get("price_now_date"))
    t.severity = r.get("severity", "none")
    t.cluster_count = int(_ofloat(r.get("cluster_count")) or 1)
    t.reasons = [s.strip() for s in (r.get("reasons", "") or "").split(";") if s.strip()]
    return t


def main():
    if not os.path.exists(CSV_PATH):
        print(f"no CSV at {CSV_PATH}")
        return 1

    today = date.today().isoformat()
    rows = []
    with open(CSV_PATH, newline="") as f:
        for r in csv.DictReader(f):
            if r.get("seen_at") == today:
                rows.append(r)
    print(f"loaded {len(rows)} rows from today's batch")

    trades = [_build_trade_from_row(r) for r in rows]
    high = sum(1 for t in trades if t.severity == "high")
    moderate = sum(1 for t in trades if t.severity == "moderate")

    subject = f"🏢 Insider trades — {high} strong, {moderate} moderate (with recap) — {today}"
    body = render_email_html(trades)
    out = os.path.join(os.path.dirname(__file__), "data", "preview.html")
    with open(out, "w") as f:
        f.write(body)
    print(f"preview written to {out} ({os.path.getsize(out):,} bytes)")
    send_email(subject, body)
    print(f"SENT — subject: {subject}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
