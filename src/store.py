"""SQLite seen-store keyed by Form 4 transaction UID."""
from __future__ import annotations

import os
import sqlite3
from datetime import date
from typing import Iterable, List

from .types import Form4Trade

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "seen.sqlite")


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


def mark_seen(trades: Iterable[Form4Trade]) -> None:
    c = _conn()
    today = date.today().isoformat()
    for t in trades:
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
