"""Price + sector + market-cap lookups.

Polygon is the primary source when POLYGON_API_KEY is set; otherwise yfinance.
Per-ticker metadata is cached to data/cache/ticker_meta.json so repeated runs
are fast and avoid hitting API rate limits.
"""
from __future__ import annotations

import json
import os
from datetime import date
from typing import Optional

import requests
import yfinance as yf

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cache", "ticker_meta.json")
_HISTORY = {}


def _load_meta() -> dict:
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_meta(meta: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w") as f:
        json.dump(meta, f, indent=2, sort_keys=True)


_META = _load_meta()


def _polygon_key() -> Optional[str]:
    return os.environ.get("POLYGON_API_KEY") or None


def _poly_ticker_meta(ticker: str) -> Optional[dict]:
    key = _polygon_key()
    if not key:
        return None
    url = f"https://api.polygon.io/v3/reference/tickers/{ticker.upper()}?apiKey={key}"
    try:
        r = requests.get(url, timeout=15)
        if r.status_code != 200:
            return None
        d = r.json().get("results") or {}
        return {
            "sector": d.get("sic_description") or "",
            "industry": d.get("type") or "",
            "market_cap": d.get("market_cap") or None,
        }
    except Exception:
        return None


def _yf_meta(ticker: str) -> Optional[dict]:
    try:
        info = yf.Ticker(ticker).info or {}
        return {
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "market_cap": info.get("marketCap") or None,
        }
    except Exception:
        return None


def lookup_ticker_meta(ticker: str) -> dict:
    """Return {sector, industry, market_cap} for a ticker."""
    if not ticker:
        return {"sector": "", "industry": "", "market_cap": None}
    t = ticker.upper().strip()
    if t in _META:
        return _META[t]
    meta = _poly_ticker_meta(t) or _yf_meta(t) or {"sector": "", "industry": "", "market_cap": None}
    _META[t] = meta
    _save_meta(_META)
    return meta


def _history(ticker: str):
    if ticker in _HISTORY:
        return _HISTORY[ticker]
    df = None
    try:
        df = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if df is None or df.empty:
            df = None
        else:
            import pandas as pd
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    except Exception:
        df = None
    _HISTORY[ticker] = df
    return df


def price_on_or_after(ticker: str, target: date, window_days: int = 7) -> Optional[float]:
    if not ticker or not target:
        return None
    df = _history(ticker)
    if df is None or df.empty:
        return None
    import pandas as pd
    target_ts = pd.Timestamp(target)
    sl = df.loc[(df.index >= target_ts) & (df.index <= target_ts + pd.Timedelta(days=window_days))]
    if sl.empty:
        return None
    return float(sl["Close"].iloc[0])


def latest_price(ticker: str) -> Optional[float]:
    df = _history(ticker)
    if df is None or df.empty:
        return None
    return float(df["Close"].iloc[-1])


def latest_price_date(ticker: str) -> Optional[date]:
    df = _history(ticker)
    if df is None or df.empty:
        return None
    return df.index[-1].date()
