"""Price + sector + market-cap lookups.

Polygon is the primary source when POLYGON_API_KEY is set; otherwise yfinance.
Per-ticker metadata is cached to data/cache/ticker_meta.json so repeated runs
are fast and avoid hitting API rate limits.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import socket
import sys
import time
from datetime import date
from typing import Optional

import requests
import yfinance as yf

CACHE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cache", "ticker_meta.json")
HISTORY_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "cache", "history")
_HISTORY = {}
_YF_TIMEOUT = float(os.environ.get("YF_TIMEOUT", "12"))
_HISTORY_MAX_AGE_HOURS = float(os.environ.get("HISTORY_MAX_AGE_HOURS", "20"))
socket.setdefaulttimeout(float(os.environ.get("NET_TIMEOUT", "20")))


def _hist_path(ticker: str) -> str:
    safe = "".join(ch if ch.isalnum() else "_" for ch in ticker.upper())
    return os.path.join(HISTORY_DIR, f"{safe}.parquet")


def _load_history_disk(ticker: str):
    p = _hist_path(ticker)
    if not os.path.exists(p):
        return None
    age_h = (time.time() - os.path.getmtime(p)) / 3600.0
    if age_h > _HISTORY_MAX_AGE_HOURS:
        return None
    try:
        import pandas as pd
        return pd.read_parquet(p)
    except Exception:
        return None


def _save_history_disk(ticker: str, df) -> None:
    if df is None or getattr(df, "empty", True):
        return
    os.makedirs(HISTORY_DIR, exist_ok=True)
    try:
        df.to_parquet(_hist_path(ticker))
    except Exception:
        pass


def _log(msg: str) -> None:
    print(f"[prices] {msg}", file=sys.stderr, flush=True)


def _run_with_timeout(fn, label: str, timeout: float):
    """Run fn() in a worker thread; return None on timeout/exception, log either way."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            _log(f"{label} TIMEOUT after {timeout:.0f}s — skipping")
            return None
        except Exception as exc:
            _log(f"{label} ERROR: {type(exc).__name__}: {exc}")
            return None


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
    def _call():
        info = yf.Ticker(ticker).info or {}
        return {
            "sector": info.get("sector") or "",
            "industry": info.get("industry") or "",
            "market_cap": info.get("marketCap") or None,
        }
    return _run_with_timeout(_call, f"yf_meta({ticker})", _YF_TIMEOUT)


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
    disk = _load_history_disk(ticker)
    if disk is not None:
        _HISTORY[ticker] = disk
        return disk
    def _call():
        df = yf.Ticker(ticker).history(period="max", auto_adjust=True)
        if df is None or df.empty:
            return None
        import pandas as pd
        df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
        return df
    df = _run_with_timeout(_call, f"yf_history({ticker})", _YF_TIMEOUT)
    _HISTORY[ticker] = df
    _save_history_disk(ticker, df)
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
