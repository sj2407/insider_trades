"""SEC EDGAR Form 4 fetcher.

Two entry points:
  - fetch_filings_for_day(d) -> list of (accession, cik, company, filename) tuples
    from the daily form.idx
  - parse_form4_xml(xml_bytes) -> list[Form4Trade]
    parses one Form 4 XML, returning all non-derivative transactions

SEC requires User-Agent identification (set via SEC_USER_AGENT env var)
and rate-limits to 10 requests/second. We respect both.
"""
from __future__ import annotations

import os
import re
import time
from datetime import date, datetime, timedelta
from typing import List, Optional, Tuple
from xml.etree import ElementTree as ET

import requests

from .types import Form4Trade, InsiderProfile

_DAILY_INDEX_TMPL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{qtr}/form.{date}.idx"
# The full SGML submission is at edgar/data/{cik}/{accession}.txt — contains the Form 4 XML inline
# Row in the .idx file uses this format already, we just resolve it to https://www.sec.gov/Archives/{filename}
_FILE_BASE = "https://www.sec.gov/Archives/"

# Rate limit ourselves well under SEC's 10 req/sec ceiling and reuse the
# TCP connection across calls to avoid the connection-storm that triggers
# their rate-limiter and causes ConnectTimeouts.
_MIN_INTERVAL = 0.18
_last_call_ts = 0.0
_session: Optional[requests.Session] = None


def _ua() -> str:
    return os.environ.get("SEC_USER_AGENT") or "insider_trades research soumaya.jameleddine@gmail.com"


def _sess() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": _ua(), "Accept-Encoding": "gzip, deflate"})
    return _session


def _get(url: str, timeout: int = 30, retries: int = 3) -> Optional[requests.Response]:
    global _last_call_ts
    s = _sess()
    for attempt in range(retries):
        wait = _MIN_INTERVAL - (time.time() - _last_call_ts)
        if wait > 0:
            time.sleep(wait)
        try:
            r = s.get(url, timeout=timeout)
            _last_call_ts = time.time()
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            return None
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            _last_call_ts = time.time()
            time.sleep(2 ** attempt)
    return None


def _qtr(d: date) -> int:
    return (d.month - 1) // 3 + 1


_IDX_ROW_RE = re.compile(
    r"^(?P<form>4|4/A)\s{2,}"
    r"(?P<company>.+?)\s{2,}"
    r"(?P<cik>\d+)\s+"
    r"(?P<date>\d{4}-?\d{2}-?\d{2})\s+"
    r"(?P<filename>edgar/data/\d+/(?P<acc>\d{10}-\d{2}-\d{6})\.txt)"
)


def fetch_filings_for_day(d: date) -> List[dict]:
    """Return list of Form 4 filings on day d, each {cik, company, accession, filename, date_filed}."""
    url = _DAILY_INDEX_TMPL.format(year=d.year, qtr=_qtr(d), date=d.strftime("%Y%m%d"))
    r = _get(url)
    if r is None:
        return []
    out = []
    for line in r.text.splitlines():
        m = _IDX_ROW_RE.match(line.rstrip())
        if not m:
            continue
        # Normalize date YYYYMMDD or YYYY-MM-DD → YYYY-MM-DD
        raw_date = m.group("date")
        if "-" not in raw_date:
            raw_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        out.append({
            "form_type": m.group("form"),
            "company": m.group("company").strip(),
            "cik": m.group("cik").lstrip("0") or m.group("cik"),
            "accession": m.group("acc"),
            "date_filed": raw_date,
            "filename": m.group("filename"),
        })
    return out


_XML_TAG_RE = re.compile(rb"<XML>\s*(.*?)\s*</XML>", re.DOTALL | re.IGNORECASE)


def _find_form4_xml(filename: str) -> Optional[bytes]:
    """Fetch the .txt SGML submission and extract the inline Form 4 XML."""
    r = _get(_FILE_BASE + filename)
    if r is None:
        return None
    body = r.content
    # The .txt envelope contains the XML between <XML>...</XML> markers
    m = _XML_TAG_RE.search(body)
    if not m:
        return None
    return m.group(1)


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_text(parent: Optional[ET.Element], path: str) -> Optional[str]:
    """Find by path stripping namespaces."""
    if parent is None:
        return None
    parts = path.split("/")
    cur = parent
    for p in parts:
        found = None
        for child in cur:
            if _strip_ns(child.tag) == p:
                found = child
                break
        if found is None:
            return None
        cur = found
    return (cur.text or "").strip() if cur is not None and cur.text else None


def _wrap_value(parent: Optional[ET.Element], path: str) -> Optional[str]:
    """Many Form 4 fields are wrapped <field><value>X</value></field>; this checks both shapes."""
    if parent is None:
        return None
    # Try direct
    t = _find_text(parent, path)
    if t:
        return t
    # Try path/value
    return _find_text(parent, path + "/value")


def _truthy(s: Optional[str]) -> bool:
    return (s or "").strip() in {"1", "true", "True"}


def parse_form4_xml(xml_bytes: bytes, accession: str, filing_date: date) -> List[Form4Trade]:
    """Parse one Form 4 XML into zero or more Form4Trade rows (non-derivative only)."""
    if not xml_bytes:
        return []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    # Issuer
    issuer = next((c for c in root if _strip_ns(c.tag) == "issuer"), None)
    issuer_cik = _wrap_value(issuer, "issuerCik") or ""
    issuer_name = _wrap_value(issuer, "issuerName") or ""
    issuer_ticker = (_wrap_value(issuer, "issuerTradingSymbol") or "").strip().upper()
    if not issuer_ticker:
        return []

    # First reporting owner (one Form 4 = one filer, by SEC rules; multi-owner cases get separate filings)
    ro = next((c for c in root if _strip_ns(c.tag) == "reportingOwner"), None)
    if ro is None:
        return []
    ro_id = next((c for c in ro if _strip_ns(c.tag) == "reportingOwnerId"), None)
    ro_rel = next((c for c in ro if _strip_ns(c.tag) == "reportingOwnerRelationship"), None)
    insider = InsiderProfile(
        name=_wrap_value(ro_id, "rptOwnerName") or "Unknown",
        cik=_wrap_value(ro_id, "rptOwnerCik") or "",
        is_director=_truthy(_wrap_value(ro_rel, "isDirector")),
        is_officer=_truthy(_wrap_value(ro_rel, "isOfficer")),
        is_ten_percent_owner=_truthy(_wrap_value(ro_rel, "isTenPercentOwner")),
        is_other=_truthy(_wrap_value(ro_rel, "isOther")),
        officer_title=_wrap_value(ro_rel, "officerTitle"),
        other_text=_wrap_value(ro_rel, "otherText"),
    )

    # Non-derivative transactions
    nd_table = next((c for c in root if _strip_ns(c.tag) == "nonDerivativeTable"), None)
    if nd_table is None:
        return []

    trades: List[Form4Trade] = []
    pdf_url = f"https://www.sec.gov/Archives/edgar/data/{int(issuer_cik) if issuer_cik else 0}/{accession.replace('-', '')}/"

    for tx in nd_table:
        if _strip_ns(tx.tag) != "nonDerivativeTransaction":
            # Skip holdings (no transaction)
            continue
        tx_date_s = _wrap_value(tx, "transactionDate")
        coding = next((c for c in tx if _strip_ns(c.tag) == "transactionCoding"), None)
        code = _wrap_value(coding, "transactionCode")
        amounts = next((c for c in tx if _strip_ns(c.tag) == "transactionAmounts"), None)
        shares_s = _wrap_value(amounts, "transactionShares")
        price_s = _wrap_value(amounts, "transactionPricePerShare")
        ad_code = _wrap_value(amounts, "transactionAcquiredDisposedCode")
        post = next((c for c in tx if _strip_ns(c.tag) == "postTransactionAmounts"), None)
        shares_after_s = _wrap_value(post, "sharesOwnedFollowingTransaction")
        own_nature = next((c for c in tx if _strip_ns(c.tag) == "ownershipNature"), None)
        direct_s = _wrap_value(own_nature, "directOrIndirectOwnership")

        try:
            tx_date = datetime.strptime(tx_date_s, "%Y-%m-%d").date() if tx_date_s else filing_date
        except ValueError:
            tx_date = filing_date

        def _f(s):
            try:
                return float(s) if s else None
            except ValueError:
                return None

        trades.append(Form4Trade(
            accession=accession,
            filing_date=filing_date,
            issuer_name=issuer_name,
            issuer_ticker=issuer_ticker,
            issuer_cik=issuer_cik,
            pdf_url=pdf_url,
            insider=insider,
            transaction_date=tx_date,
            transaction_code=(code or "").strip(),
            acquired_disposed=(ad_code or "").strip(),
            shares=_f(shares_s),
            price=_f(price_s),
            shares_owned_after=_f(shares_after_s),
            is_direct=(direct_s == "D") if direct_s else None,
        ))
    return trades


def fetch_and_parse_filing(entry: dict) -> List[Form4Trade]:
    xml = _find_form4_xml(entry["filename"])
    if not xml:
        return []
    try:
        filing_date = datetime.strptime(entry["date_filed"], "%Y-%m-%d").date()
    except ValueError:
        filing_date = date.today()
    return parse_form4_xml(xml, entry["accession"], filing_date)


def fetch_recent_trades(days_back: int = 2) -> List[Form4Trade]:
    """Convenience: fetch Form 4 trades from the last `days_back` business days."""
    out: List[Form4Trade] = []
    today = date.today()
    days = []
    d = today
    while len(days) < days_back + 5:  # walk back a bit to cover weekends/holidays
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
        if len(days) >= days_back:
            break
    for d in days[:days_back]:
        entries = fetch_filings_for_day(d)
        for e in entries:
            out.extend(fetch_and_parse_filing(e))
    return out
