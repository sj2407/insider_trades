"""Shared dataclasses for the insider-trade pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import List, Optional


@dataclass
class InsiderProfile:
    """The reporting person on a Form 4."""
    name: str
    cik: str
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    is_other: bool = False
    officer_title: Optional[str] = None
    other_text: Optional[str] = None

    @property
    def role_bucket(self) -> str:
        """Coarse role categorization used for the dashboard breakdown."""
        title = (self.officer_title or "").upper()
        if self.is_officer and any(k in title for k in ("CEO", "CHIEF EXECUTIVE", "PRESIDENT")):
            return "CEO/President"
        if self.is_officer and any(k in title for k in ("CFO", "CHIEF FINANCIAL", "TREASURER")):
            return "CFO"
        if self.is_officer and any(k in title for k in ("COO", "CHIEF OPERATING")):
            return "COO"
        if self.is_officer and "CHIEF" in title:
            return "Other C-suite"
        if self.is_officer:
            return "Other officer"
        if self.is_director:
            return "Director"
        if self.is_ten_percent_owner:
            return "10% owner"
        return "Other"

    @property
    def is_c_suite(self) -> bool:
        return self.role_bucket in {"CEO/President", "CFO", "COO", "Other C-suite"}


@dataclass
class Form4Trade:
    """One non-derivative stock transaction within a Form 4."""
    # Filing-level
    accession: str
    filing_date: date
    issuer_name: str
    issuer_ticker: str
    issuer_cik: str
    pdf_url: str

    # Reporting person
    insider: InsiderProfile

    # Transaction
    transaction_date: date
    transaction_code: str            # P, S, M, A, F, G, V, etc.
    acquired_disposed: str           # A or D
    shares: Optional[float] = None
    price: Optional[float] = None
    shares_owned_after: Optional[float] = None
    is_direct: Optional[bool] = None

    # Enriched at runtime
    sector: Optional[str] = None
    industry: Optional[str] = None
    market_cap: Optional[float] = None
    price_now: Optional[float] = None
    price_now_date: Optional[date] = None

    # Scoring (filled in by src.scoring)
    severity: str = "none"           # high / moderate / low / none / noise
    reasons: List[str] = field(default_factory=list)
    cluster_count: int = 1           # number of distinct insiders buying same issuer in last 30d
    cluster_window_days: int = 30

    @property
    def dollar_value(self) -> Optional[float]:
        if self.shares is not None and self.price is not None:
            return abs(self.shares) * self.price
        return None

    @property
    def is_open_market_buy(self) -> bool:
        return self.transaction_code == "P" and self.acquired_disposed == "A"

    @property
    def is_open_market_sell(self) -> bool:
        return self.transaction_code == "S" and self.acquired_disposed == "D"

    @property
    def is_noise(self) -> bool:
        """Codes we filter out by default: option exercises, awards, tax sales, gifts."""
        return self.transaction_code in {"M", "A", "F", "G", "I", "K", "W", "Z", "U", "X"}

    def uid(self) -> str:
        return f"{self.accession}|{self.insider.cik}|{self.transaction_date.isoformat()}|{self.transaction_code}|{self.shares}|{self.price}"
