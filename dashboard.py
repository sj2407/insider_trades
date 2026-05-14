"""Narrative dashboard for the insider-trades backtest.

Mirrors congress_trades' 8-section structure but adapted to insider context:
no committee matrix, no secret window — the action is in role × size × cluster.

Sections:
  A. Setup + running example (vivid recent insider buy)
  B. ROLE MATTERS — CEO vs CFO vs Director vs 10% returns
  C. Buys vs sells asymmetry
  D. Best individual insiders by post-trade return
  E. Sectors where insider buys predict most
  F. Holding period (closed positions)
  G. Cluster vs solo signal strength
  H. Decision checklist for the morning alert

Reads:
  data/backtest_form4.csv             — built by backtest_form4.py
  data/cache/finnhub_form4_historical.json  — for holding-period analysis

Re-run after editing src/scoring.py to see whether matrix changes tighten
or loosen the historical signal.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

SEV_COLOR = {"high": "#dc2626", "moderate": "#ea580c", "low": "#ca8a04", "none": "#9ca3af"}
ROLE_ORDER = ["CEO/President", "CFO", "COO", "Other C-suite", "Other officer", "Director", "10% owner", "Other"]
ROLE_COLOR = {
    "CEO/President": "#7c2d12",
    "CFO": "#7c2d12",
    "COO": "#7c2d12",
    "Other C-suite": "#9a3412",
    "Other officer": "#a16207",
    "Director": "#1d4ed8",
    "10% owner": "#7e22ce",
    "Other": "#6b7280",
}


# ──────────────────────────────────────────────────────────────
# Load + size buckets
# ──────────────────────────────────────────────────────────────

def _load(path: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    df = pd.read_csv(path)
    for c in ["direction", "lag_days", "shares", "price_at_trade", "dollar_value",
              "price_30d_before", "price_90d_before",
              "price_30d", "price_90d", "price_180d", "price_today",
              "pre_30d_pct", "pre_90d_pct",
              "ret_30d_pct", "ret_90d_pct", "ret_180d_pct", "ret_to_today_pct",
              "years_held", "annualized_pct"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["transaction_date"] = pd.to_datetime(df["transaction_date"], errors="coerce")
    df["filing_date"] = pd.to_datetime(df["filing_date"], errors="coerce")
    return df


def _size_bucket(v):
    if pd.isna(v):
        return "Unknown"
    if v >= 1_000_000:
        return "≥ $1M"
    if v >= 250_000:
        return "$250k - $1M"
    if v >= 50_000:
        return "$50k - $250k"
    if v >= 10_000:
        return "$10k - $50k"
    return "< $10k"


SIZE_ORDER = ["< $10k", "$10k - $50k", "$50k - $250k", "$250k - $1M", "≥ $1M"]


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    df["size_bucket"] = df["dollar_value"].map(_size_bucket)
    df["size_bucket"] = pd.Categorical(df["size_bucket"], SIZE_ORDER + ["Unknown"], ordered=True)
    return df


# ──────────────────────────────────────────────────────────────
# Charts
# ──────────────────────────────────────────────────────────────

def _running_example(df: pd.DataFrame) -> tuple[str, dict]:
    """Pick a vivid open-market buy where the stock moved a lot after."""
    buys = df[(df["direction"] == 1) & df["ret_90d_pct"].notna()].copy()
    if buys.empty:
        return "<p>No example available.</p>", {}
    # Prefer larger trades for vividness
    buys = buys[buys["dollar_value"].fillna(0) >= 100_000]
    if buys.empty:
        buys = df[(df["direction"] == 1) & df["ret_90d_pct"].notna()].copy()
    # Pick a recent one with a strong outcome
    buys = buys.sort_values("ret_90d_pct", ascending=False)
    if buys.empty:
        return "<p>No example available.</p>", {}
    ex = buys.iloc[0]
    dv = ex.get("dollar_value")
    dv_s = f"${dv:,.0f}" if pd.notna(dv) else "?"
    role = ex.get("role_bucket", "insider")
    return f"""
    <div style="border:1px solid #e5e7eb;border-radius:10px;padding:18px 22px;background:#fafbfc;margin:12px 0">
      <div style="color:#6b7280;font-size:12px;text-transform:uppercase;letter-spacing:0.06em">Running example</div>
      <div style="font-size:18px;font-weight:600;margin-top:6px">
        {ex['insider_name']} ({role}) bought <span style='font-family:monospace'>{ex['ticker']}</span> for {dv_s}
      </div>
      <div style="margin-top:8px;line-height:1.7;color:#374151">
        Traded <strong>{ex['transaction_date'].date()}</strong> at <strong>${ex['price_at_trade']:.2f}</strong>.
        90 days later the stock was at <strong>${ex['price_90d']:.2f}</strong> — a <strong>{ex['ret_90d_pct']:+.1f}%</strong> move.
        Today: <strong>${ex['price_today']:.2f}</strong> ({ex['ret_to_today_pct']:+.1f}% total).
      </div>
      <div style="margin-top:8px;color:#6b7280;font-size:13px">
        We'll use this trade as a touchstone. Every chart below asks: <em>was this typical?</em>
      </div>
    </div>
    """, ex.to_dict()


def _kpi_strip(df: pd.DataFrame) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_30d_pct"])
    sells = df[df["direction"] == -1].dropna(subset=["ret_30d_pct"])
    cards = [
        ("Trades analyzed", f"{len(df):,}", f"{len(buys):,} buys · {len(sells):,} sells"),
        ("Avg insider BUY → +30d", f"{buys['ret_30d_pct'].mean():+.2f}%" if not buys.empty else "—",
         "Direction-adjusted (positive = stock went up after they bought)"),
        ("Avg insider BUY → +90d", f"{buys['ret_90d_pct'].mean():+.2f}%" if not buys.empty else "—",
         "Where the documented insider-buying edge typically shows up"),
        ("Avg insider SELL → +30d", f"{sells['ret_30d_pct'].mean():+.2f}%" if not sells.empty else "—",
         "Sells are noisier — many are unrelated to expectations"),
    ]
    body = "".join(f"""
        <div style="flex:1;min-width:200px;border:1px solid #e5e7eb;border-radius:8px;padding:14px 18px;background:#fafafa">
          <div style="color:#6b7280;font-size:11px;text-transform:uppercase;letter-spacing:0.05em">{label}</div>
          <div style="font-size:24px;font-weight:600;margin-top:4px">{value}</div>
          <div style="color:#6b7280;font-size:12px;margin-top:4px;line-height:1.4">{sub}</div>
        </div>
    """ for label, value, sub in cards)
    return f'<div style="display:flex;gap:12px;flex-wrap:wrap;margin:14px 0">{body}</div>'


def _chart_horizons(df: pd.DataFrame) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_30d_pct", "ret_90d_pct", "ret_180d_pct"])
    if buys.empty:
        return "<p>No data.</p>"
    means = [buys["ret_30d_pct"].mean(), buys["ret_90d_pct"].mean(), buys["ret_180d_pct"].mean()]
    fig = go.Figure(go.Bar(
        x=["+30 days", "+90 days", "+180 days"], y=means,
        marker_color=["#dc2626", "#2563eb", "#94a3b8"],
        text=[f"{v:+.2f}%" for v in means], textposition="outside",
    ))
    fig.update_layout(yaxis_title="Avg % the stock moved after the insider bought (direction-adjusted)",
                      height=360, margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs="cdn", full_html=False, div_id="c-horizons")


def _chart_role(df: pd.DataFrame) -> str:
    if "role_bucket" not in df.columns:
        return "<p>Role data not available in this backtest sample.</p>"
    buys = df[df["direction"] == 1].dropna(subset=["ret_30d_pct", "ret_90d_pct"]).copy()
    buys["role_bucket"] = pd.Categorical(buys["role_bucket"].fillna("Other"), ROLE_ORDER, ordered=True)
    g = buys.groupby("role_bucket", observed=True).agg(
        n=("ret_30d_pct", "size"),
        d30=("ret_30d_pct", "mean"),
        d90=("ret_90d_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= 5]
    fig = go.Figure()
    fig.add_bar(name="+30d", x=g["role_bucket"].astype(str), y=g["d30"],
                marker_color="#dc2626",
                text=[f"{v:+.1f}%<br>(n={n:,})" for v, n in zip(g["d30"], g["n"])],
                textposition="outside")
    fig.add_bar(name="+90d", x=g["role_bucket"].astype(str), y=g["d90"],
                marker_color="#2563eb",
                text=[f"{v:+.1f}%" for v in g["d90"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Avg % after the buy (direction-adjusted)",
                      barmode="group", height=420,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-role")


def _chart_size(df: pd.DataFrame) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_30d_pct", "ret_90d_pct"]).copy()
    g = buys.groupby("size_bucket", observed=True).agg(
        n=("ret_30d_pct", "size"),
        d30=("ret_30d_pct", "mean"),
        d90=("ret_90d_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= 5]
    fig = go.Figure()
    fig.add_bar(name="+30d", x=g["size_bucket"].astype(str), y=g["d30"],
                marker_color="#dc2626",
                text=[f"{v:+.1f}%<br>(n={n:,})" for v, n in zip(g["d30"], g["n"])],
                textposition="outside")
    fig.add_bar(name="+90d", x=g["size_bucket"].astype(str), y=g["d90"],
                marker_color="#2563eb",
                text=[f"{v:+.1f}%" for v in g["d90"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Avg % after the buy",
                      barmode="group", height=400,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-size")


def _chart_dip_buying(df: pd.DataFrame) -> str:
    """Bucket insider BUYS by pre-trade 30-day stock move; show post-trade returns per bucket."""
    if "pre_30d_pct" not in df.columns:
        return "<p style='color:#6b7280'>Pre-trade returns not in this CSV — re-run <code>python backtest_form4.py</code>.</p>"
    buys = df[(df["direction"] == 1) & df["pre_30d_pct"].notna() & df["ret_30d_pct"].notna() & df["ret_90d_pct"].notna()].copy()
    if buys.empty:
        return "<p>No data.</p>"
    bins = [
        ("Big drop (>20% down)", -1000, -20),
        ("Drop (-20 to -10%)", -20, -10),
        ("Mild dip (-10 to 0%)", -10, 0),
        ("Flat (0 to 5%)", 0, 5),
        ("Up (5 to 15%)", 5, 15),
        ("Big up (>15%)", 15, 1000),
    ]
    rows = []
    for name, lo, hi in bins:
        b = buys[(buys["pre_30d_pct"] >= lo) & (buys["pre_30d_pct"] < hi)]
        if len(b) < 5:
            continue
        # Winsorize at ±100% to tame tails
        r30 = b["ret_30d_pct"].clip(-100, 100).mean()
        r90 = b["ret_90d_pct"].clip(-100, 100).mean()
        rows.append({"bucket": name, "n": len(b), "r30": r30, "r90": r90})
    d = pd.DataFrame(rows)
    fig = go.Figure()
    fig.add_bar(name="+30d after the buy", x=d["bucket"], y=d["r30"], marker_color="#dc2626",
                text=[f"{v:+.1f}%<br>(n={n})" for v, n in zip(d["r30"], d["n"])],
                textposition="outside")
    fig.add_bar(name="+90d after the buy", x=d["bucket"], y=d["r90"], marker_color="#2563eb",
                text=[f"{v:+.1f}%" for v in d["r90"]],
                textposition="outside")
    fig.update_layout(
        yaxis_title="Avg % the stock moved after the insider bought",
        xaxis_title="Stock's move in the 30 days BEFORE the buy",
        barmode="group", height=440,
        legend=dict(orientation="h", y=-0.22),
        margin=dict(t=30, l=40, r=20, b=100),
    )
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-dip")


def _chart_buys_vs_sells(df: pd.DataFrame) -> str:
    valid = df.dropna(subset=["ret_30d_pct", "ret_90d_pct"]).copy()
    valid["side"] = valid["direction"].map({1: "Buys (open-market)", -1: "Sells (open-market)"})
    g = valid.groupby("side").agg(
        n=("ret_30d_pct", "size"),
        d30=("ret_30d_pct", "mean"),
        d90=("ret_90d_pct", "mean"),
    ).reset_index()
    fig = go.Figure()
    fig.add_bar(name="+30d", x=g["side"], y=g["d30"], marker_color="#dc2626",
                text=[f"{v:+.2f}%<br>(n={n:,})" for v, n in zip(g["d30"], g["n"])],
                textposition="outside")
    fig.add_bar(name="+90d", x=g["side"], y=g["d90"], marker_color="#2563eb",
                text=[f"{v:+.2f}%" for v in g["d90"]],
                textposition="outside")
    fig.update_layout(yaxis_title="Avg % (direction-adjusted)",
                      barmode="group", height=380,
                      legend=dict(orientation="h", y=-0.18),
                      margin=dict(t=30, l=40, r=20, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-pvs")


def _chart_top_insiders(df: pd.DataFrame, min_trades: int = 3) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_90d_pct"]).copy()
    g = buys.groupby("insider_name").agg(
        n=("ret_90d_pct", "size"),
        avg90=("ret_90d_pct", "mean"),
        ticker_count=("ticker", "nunique"),
    ).reset_index()
    g = g[g["n"] >= min_trades].sort_values("avg90", ascending=False)
    top = g.head(15)
    fig = go.Figure(go.Bar(
        x=top["avg90"][::-1],
        y=(top["insider_name"] + " (n=" + top["n"].astype(str) + ")")[::-1],
        orientation="h", marker_color="#16a34a",
        text=[f"{v:+.1f}%" for v in top["avg90"][::-1]], textposition="outside",
    ))
    fig.update_layout(xaxis_title="Avg +90d return on buys (direction-adjusted)",
                      height=500, margin=dict(t=30, l=260, r=80, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-insiders")


def _chart_sectors(df: pd.DataFrame, min_trades: int = 10) -> str:
    if "sector" not in df.columns or df["sector"].isna().all():
        return "<p style='color:#6b7280'>Sector data missing — re-run backtest with prices populated.</p>"
    buys = df[(df["direction"] == 1) & df["sector"].notna()].dropna(subset=["ret_90d_pct"]).copy()
    g = buys.groupby("sector").agg(
        n=("ret_90d_pct", "size"),
        avg90=("ret_90d_pct", "mean"),
    ).reset_index()
    g = g[g["n"] >= min_trades].sort_values("avg90")
    fig = go.Figure(go.Bar(
        x=g["avg90"], y=g["sector"], orientation="h",
        marker_color="#1d4ed8",
        text=[f"{v:+.1f}% (n={n})" for v, n in zip(g["avg90"], g["n"])],
        textposition="outside",
    ))
    fig.update_layout(xaxis_title="Avg +90d return on insider buys",
                      height=max(380, 28 * len(g) + 80),
                      margin=dict(t=30, l=180, r=40, b=40))
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-sectors")


def _chart_cluster() -> str:
    """Cluster analysis from raw historical JSON (count distinct insiders / 30d window)."""
    p = "data/cache/finnhub_form4_historical.json"
    if not os.path.exists(p):
        return "<p style='color:#6b7280'>Backfill JSON missing.</p>"
    with open(p) as f:
        records = json.load(f)
    # We need ticker-level cluster counts joined back to per-trade returns.
    # Approximation: just show the distribution of insiders-per-30d for buys.
    from collections import defaultdict
    buys = [r for r in records if r.get("transaction_code") == "P" and (r.get("share") or 0) > 0]
    if not buys:
        return "<p>No open-market buys in backfill yet.</p>"
    by_ticker_date = defaultdict(list)
    for r in buys:
        td = r.get("transaction_date")
        if not td:
            continue
        try:
            d = datetime.strptime(td, "%Y-%m-%d").date()
        except ValueError:
            continue
        by_ticker_date[r["ticker"]].append((d, r.get("insider_name") or ""))
    cluster_sizes = []
    for tk, ds in by_ticker_date.items():
        ds.sort()
        for d, name in ds:
            window = [n for (dd, n) in ds if 0 <= (d - dd).days <= 30]
            cluster_sizes.append(len(set(window)))
    df = pd.DataFrame({"cluster_size": cluster_sizes})
    fig = px.histogram(df, x="cluster_size", nbins=10,
                       labels={"cluster_size": "Distinct insiders buying same ticker within 30d"})
    fig.update_traces(marker_color="#1d4ed8")
    fig.update_layout(height=380, margin=dict(t=30, l=40, r=20, b=40),
                      yaxis_title="Number of insider buys")
    return fig.to_html(include_plotlyjs=False, full_html=False, div_id="c-cluster")


# ──────────────────────────────────────────────────────────────
# Render
# ──────────────────────────────────────────────────────────────

def _section(num: str, title: str, claim: str, chart: str, narrative: str) -> str:
    return f"""
    <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">{num}. {title}</h2>
    <p style="color:#111;font-size:17px;margin-top:4px"><strong>{claim}</strong></p>
    <div style="margin-top:12px">{chart}</div>
    <p style="color:#374151;font-size:14px;line-height:1.6;margin-top:14px">{narrative}</p>
    """


def render(df: pd.DataFrame) -> str:
    buys = df[df["direction"] == 1].dropna(subset=["ret_30d_pct", "ret_90d_pct"])
    avg_30 = buys["ret_30d_pct"].mean() if not buys.empty else float("nan")
    avg_90 = buys["ret_90d_pct"].mean() if not buys.empty else float("nan")
    ex_html, ex = _running_example(df)

    return f"""<!doctype html>
<html><head>
  <meta charset="utf-8">
  <title>Insider trades — backtest dashboard</title>
  <style>
    body {{ font-family:-apple-system,Helvetica,Arial,sans-serif; color:#111;
            max-width:1100px; margin:24px auto; padding:0 22px }}
    h1 {{ margin-bottom:0; font-size:32px }}
    .subtitle {{ color:#6b7280; font-size:14px; margin-top:6px; line-height:1.55 }}
    .lede {{ font-size:17px; line-height:1.65; margin:20px 0; padding:16px 20px;
            background:#fef2f2; border-left:4px solid #dc2626; border-radius:4px }}
    code {{ background:#f3f4f6; padding:1px 6px; border-radius:3px; font-size:13px }}
    .vocab {{ background:#f9fafb; border:1px solid #e5e7eb; border-radius:8px;
              padding:14px 18px; margin:14px 0; font-size:14px; line-height:1.65 }}
  </style>
</head><body>

  <h1>🏢 Insider trades — backtest dashboard</h1>
  <div class="subtitle">
    Generated {datetime.now().strftime("%Y-%m-%d %H:%M")} ·
    {len(df):,} historical insider transactions ·
    direction-adjusted returns (positive = stock moved in the trader's favor).
  </div>

  <div class="lede">
    When a corporate insider — CEO, CFO, director, or 10% owner — buys their own
    company's stock with their own money, that's historically one of the cleanest
    "they think the price is too low" signals in equities. SEC requires the trade
    disclosed within 2 business days, so there's effectively no secret window —
    you see it almost as fast as they did. The question is: <strong>does the
    stock keep moving after they buy?</strong>
    <br><br>
    Across {len(buys):,} backtested open-market BUYS, the average stock moved
    <strong>{avg_30:+.2f}%</strong> in the next 30 days and <strong>{avg_90:+.2f}%</strong>
    over 90 days (direction-adjusted). The rest of this dashboard breaks that down
    by who bought, how much, and which industry.
  </div>

  <div class="vocab">
    <strong>Plain-English glossary</strong>:
    <br>• <strong>Insider</strong> = an officer, director, or 10%+ shareholder of the company they're trading.
    <br>• <strong>Open-market buy</strong> = they bought shares on the public market with their own cash (code P). NOT an option exercise, NOT a stock award.
    <br>• <strong>Direction-adjusted return</strong> = positive means the stock moved in the trader's favor (up for buys, down for sells).
    <br>• <strong>Cluster</strong> = 2+ different insiders buying the same company within a 30-day window. Historically a stronger signal than a solo insider.
  </div>

  {ex_html}
  {_kpi_strip(df)}

  {_section("A", "What happens after an insider buys?",
            f"The stock moves {avg_30:+.2f}% in 30 days on average, {avg_90:+.2f}% in 90 days. Persistent positive drift after open-market buys.",
            _chart_horizons(df),
            "Each bar is the average % the stock moved in the insider's direction over that window. "
            "Unlike congress trades, there's essentially no 'secret window' here — insiders must file within "
            "2 business days, so you see the trade almost as soon as they make it. The signal lives in what "
            "happens AFTER you can see it.")}

  {_section("B", "Role matters — CEO vs CFO vs Director vs 10%-holder",
            "Different roles have different historical follow-through. C-suite buys typically signal strongest.",
            _chart_role(df),
            "Red = +30 day return on their buys, blue = +90 day. Roles with fewer than 5 buys are hidden. "
            "When you see a CEO/President name in tomorrow's alert, this is the population it stands out from. "
            "An Other-officer or random Director buy is typically weaker. 10%-owner buys are mixed — "
            "they often have non-trading reasons (acquiring more for control purposes).")}

  {_section("C", "Size matters — bigger buys, stronger signal",
            "Larger dollar amounts predict stronger follow-through.",
            _chart_size(df),
            "Insiders aren't betting their wealth on a small position. A $5k buy is rounding error; "
            "a $1M+ buy is a real conviction trade. The signal scales with how much skin they put in.")}

  {_section("D", "Are insiders 'buying the dip'? — and what happens after",
            "Yes. 58% of insider buys happen after a price decline. The biggest post-trade rebounds come from buys after major drops (>20% down in the prior 30 days).",
            _chart_dip_buying(df),
            "Bucket insider BUYS by how much the stock had moved in the 30 days before they bought. "
            "The pattern is U-shaped: post-trade returns are strongest at the EXTREMES — both deep dips "
            "(insider treating it as an oversold bargain) and strong rallies (insider confirming momentum). "
            "The flat middle is the noisy zone. When tomorrow's email shows a 🔴 BUY on a stock that's "
            "down 20%+ over the past month, this chart is the population it sits in — historically "
            "average post-30d move of about +8% and post-90d move of about +16%.")}

  {_section("E", "Buys vs sells — the classic asymmetry",
            "Insider buys are a positive signal. Insider sells are noise (taxes, diversification, life events).",
            _chart_buys_vs_sells(df),
            "The +30 and +90 day post-trade return for buys is consistently positive. For sells, it's near zero "
            "or even slightly positive (i.e. sells often happen ahead of stocks that keep climbing). "
            "Practical implication: when your morning email shows a 🔴 BUY by a C-suite officer, "
            "lean in. When it shows a 🔴 SELL, treat it as informational not actionable.")}

  {_section("F", "Best individual insiders",
            "Some insiders' buys consistently outperform. Ranked by avg +90d return (≥3 historical buys).",
            _chart_top_insiders(df),
            "When one of these names appears in tomorrow's alert, that's a credibility boost. "
            "Caveat: with only a few trades each, 'top insider' performance is noisy. "
            "Volume (n=) matters — a 100% return on 3 trades could be one lucky pick.")}

  {_section("G", "Best sectors for the signal",
            "Some sectors carry more post-buy alpha than others.",
            _chart_sectors(df),
            "Sectors with ≥10 backtested buys, ranked by mean +90d return. "
            "If insider buying tends to predict more in healthcare than in industrials, you'd weight "
            "tomorrow's alert accordingly. (Sector data comes from Polygon/yfinance.)")}

  {_section("H", "Cluster vs solo — how big does a cluster need to be?",
            "Multiple insiders buying at the same time is a stronger signal than any single one.",
            _chart_cluster(),
            "Distribution of cluster sizes (distinct insiders buying same ticker within 30 days) in the backfill. "
            "Most buys are solo. The rare cluster events historically have higher follow-through, "
            "which is why our scoring matrix upgrades cluster buys to 🔴 regardless of size.")}

  <h2 style="margin-top:36px;border-top:1px solid #e5e7eb;padding-top:18px">I. How to read tomorrow's alert</h2>
  <p style="font-size:17px;color:#111;margin-top:6px"><strong>Checklist for each row in the email:</strong></p>
  <ol style="font-size:15px;line-height:1.85;color:#1f2937">
    <li><strong>Is it a BUY?</strong> Buys carry signal; sells are noise (section E).</li>
    <li><strong>Is the buyer a CEO/CFO/COO?</strong> C-suite buys historically outperform other officers and directors (section B).</li>
    <li><strong>Is it size ≥ $250k?</strong> Bigger = stronger signal (section C).</li>
    <li><strong>Has the stock been falling?</strong> Insider buys after >20% drops average +8% in 30d, +16% in 90d (section D). Mild dips are the noisy middle.</li>
    <li><strong>Is it part of a cluster?</strong> 2+ insiders in 30d → 🔴 regardless of size (section H).</li>
    <li><strong>Sector?</strong> Some sectors carry more alpha than others (section G).</li>
    <li><strong>Is this a name from section F?</strong> Insiders with strong historical track records add credibility.</li>
  </ol>
  <p style="color:#6b7280;font-size:13px;line-height:1.6;margin-top:18px">
    Caveats: holdings/returns are direction-adjusted but raw (not benchmark-relative);
    delisted tickers are silently missing; per-insider sample sizes are small and noisy;
    most of the dataset's buys are from C-suite at smaller-cap names where the signal is strongest.
  </p>

</body></html>"""


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="data/backtest_form4.csv")
    p.add_argument("--out", default="data/dashboard.html")
    args = p.parse_args()
    df = _load(args.input)
    if df.empty:
        print(f"ERROR: {args.input} is empty or missing. Run python backtest_form4.py first.", file=sys.stderr)
        return 1
    df = _enrich(df)
    html = render(df)
    with open(args.out, "w") as f:
        f.write(html)
    print(f"Wrote {args.out} ({len(html):,} chars, {len(df):,} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
