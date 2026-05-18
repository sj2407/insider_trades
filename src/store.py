"""SQLite seen-store keyed by Form 4 transaction UID + clean CSV archive.

`data/trades_history.csv` is the human-reviewable, fully-filtered, deduped
signal archive — only trades that survived every funnel filter land there.
"""
from __future__ import annotations

import csv
import os
import sqlite3
from datetime import date, datetime
from typing import Iterable, List, Optional

from .types import Form4Trade

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seen.sqlite")
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trades_history.csv")

CSV_COLUMNS = [
    "seen_at", "filing_date", "transaction_date",
    "ticker", "issuer",
    "insider_name", "insider_role", "officer_title",
    "transaction_code", "acquired_disposed",
    "shares", "price", "dollar_value",
    "severity", "cluster_count", "reasons",
    "sector", "industry", "market_cap",
    "price_now", "price_now_date",
    "accession", "pdf_url", "uid",
]


def _conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """CREATE TABLE IF NOT EXISTS seen (
            uid TEXT PRIMARY KEY,
            accession TEXT,
            ticker TEXT,
            insider_name TEXT,
            insider_role TEXT,
            tx_code TEXT,
            tx_date TEXT,
            filing_date TEXT,
            shares REAL,
            price REAL,
            severity TEXT,
            seen_at TEXT
        )"""
    )
    return c


def store_is_empty() -> bool:
    c = _conn()
    n = c.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    c.close()
    return n == 0


def filter_new(trades: Iterable[Form4Trade]) -> List[Form4Trade]:
    c = _conn()
    cur = c.cursor()
    new = []
    for t in trades:
        cur.execute("SELECT 1 FROM seen WHERE uid = ?", (t.uid(),))
        if cur.fetchone() is None:
            new.append(t)
    c.close()
    return new


def last_successful_run_date() -> Optional[date]:
    c = _conn()
    row = c.execute("SELECT MAX(seen_at) FROM seen").fetchone()
    c.close()
    if not row or not row[0]:
        return None
    try:
        return datetime.fromisoformat(row[0]).date()
    except ValueError:
        return None


def _csv_existing_uids() -> set:
    if not os.path.exists(CSV_PATH):
        return set()
    uids = set()
    try:
        with open(CSV_PATH, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("uid"):
                    uids.add(row["uid"])
    except Exception:
        pass
    return uids


def _csv_row(t: Form4Trade, today: str) -> dict:
    return {
        "seen_at": today,
        "filing_date": t.filing_date.isoformat(),
        "transaction_date": t.transaction_date.isoformat(),
        "ticker": t.issuer_ticker,
        "issuer": t.issuer_name,
        "insider_name": t.insider.name,
        "insider_role": t.insider.role_bucket,
        "officer_title": t.insider.officer_title or "",
        "transaction_code": t.transaction_code,
        "acquired_disposed": t.acquired_disposed,
        "shares": t.shares if t.shares is not None else "",
        "price": t.price if t.price is not None else "",
        "dollar_value": t.dollar_value if t.dollar_value is not None else "",
        "severity": t.severity,
        "cluster_count": t.cluster_count,
        "reasons": "; ".join(t.reasons),
        "sector": t.sector or "",
        "industry": t.industry or "",
        "market_cap": t.market_cap if t.market_cap is not None else "",
        "price_now": t.price_now if t.price_now is not None else "",
        "price_now_date": t.price_now_date.isoformat() if t.price_now_date else "",
        "accession": t.accession,
        "pdf_url": t.pdf_url,
        "uid": t.uid(),
    }


def append_to_history(trades: Iterable[Form4Trade]) -> int:
    """Append rows to trades_history.csv, skipping any uid already present."""
    trades_list = list(trades)
    if not trades_list:
        return 0
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    existing = _csv_existing_uids()
    write_header = not os.path.exists(CSV_PATH)
    today = date.today().isoformat()
    appended = 0
    with open(CSV_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        if write_header:
            w.writeheader()
        for t in trades_list:
            if t.uid() in existing:
                continue
            w.writerow(_csv_row(t, today))
            existing.add(t.uid())
            appended += 1
    print(f"[store] appended {appended} new row(s) to {os.path.relpath(CSV_PATH)}", flush=True)
    return appended


def mark_seen(trades: Iterable[Form4Trade]) -> None:
    """Update both the SQLite dedup store and the CSV history archive."""
    trades_list = list(trades)
    c = _conn()
    today = date.today().isoformat()
    for t in trades_list:
        c.execute(
            """INSERT OR IGNORE INTO seen VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.uid(), t.accession, t.issuer_ticker,
                t.insider.name, t.insider.role_bucket,
                t.transaction_code,
                t.transaction_date.isoformat(),
                t.filing_date.isoformat(),
                t.shares, t.price, t.severity, today,
            ),
        )
    c.commit()
    c.close()
    append_to_history(trades_list)
