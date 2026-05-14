"""Email rendering for insider-trade alerts."""
from __future__ import annotations

import html
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import List

from .scoring import SEVERITY_RANK
from .types import Form4Trade

BADGE = {
    "high": ("🔴", "#b91c1c", "Strong"),
    "moderate": ("🟠", "#c2410c", "Some"),
    "low": ("🟡", "#a16207", "Weak"),
    "none": ("⚪", "#525252", "None"),
}

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


def _money(v):
    return f"${v:,.0f}" if v is not None else "—"


def _pct(v):
    if v is None:
        return "—"
    color = "#16a34a" if v >= 0 else "#dc2626"
    return f'<span style="color:{color};font-weight:600">{v:+.1f}%</span>'


def _row(t: Form4Trade) -> str:
    badge, color, label = BADGE.get(t.severity, BADGE["none"])
    role = t.insider.role_bucket
    role_color = ROLE_COLOR.get(role, "#6b7280")
    side = "BUY" if t.is_open_market_buy else ("SELL" if t.is_open_market_sell else t.transaction_code)
    side_color = "#16a34a" if side == "BUY" else ("#dc2626" if side == "SELL" else "#525252")
    pct_since = None
    if t.price and t.price_now and t.price != 0:
        sign = 1 if t.is_open_market_buy else -1
        pct_since = sign * (t.price_now - t.price) / t.price * 100
    cluster_cell = f'<strong>{t.cluster_count}</strong> insiders / 30d' if t.cluster_count > 1 else "solo"
    reasons_html = "<br>".join(html.escape(r) for r in t.reasons[:3]) if t.reasons else "—"
    mcap_str = "—"
    if t.market_cap:
        mcap_str = (f"${t.market_cap/1e9:.1f}B" if t.market_cap >= 1e9 else f"${t.market_cap/1e6:.0f}M")
    return f"""
    <tr>
      <td style="padding:6px 10px;vertical-align:top;color:{color};font-weight:600;white-space:nowrap">{badge} {label}</td>
      <td style="padding:6px 10px;vertical-align:top">
        <strong>{html.escape(t.insider.name.title())}</strong><br>
        <span style="color:{role_color};font-size:12px;font-weight:600">{html.escape(role)}{f" — {html.escape(t.insider.officer_title)}" if t.insider.officer_title else ""}</span>
      </td>
      <td style="padding:6px 10px;vertical-align:top;font-family:monospace;font-weight:600">
        {html.escape(t.issuer_ticker)}<br>
        <span style="color:#6b7280;font-size:11px;font-family:inherit">{html.escape(t.issuer_name[:40])}</span>
      </td>
      <td style="padding:6px 10px;vertical-align:top;color:{side_color};font-weight:600">{side}</td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{f"{int(t.shares):,}" if t.shares else "—"}</td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{_money(t.price)}</td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right;font-weight:600">{_money(t.dollar_value)}</td>
      <td style="padding:6px 10px;vertical-align:top">{t.transaction_date}</td>
      <td style="padding:6px 10px;vertical-align:top">{t.filing_date}</td>
      <td style="padding:6px 10px;vertical-align:top">{cluster_cell}</td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{_money(t.price_now)}<br><span style="color:#6b7280;font-size:11px">{t.price_now_date or ""}</span></td>
      <td style="padding:6px 10px;vertical-align:top;text-align:right">{_pct(pct_since)}</td>
      <td style="padding:6px 10px;vertical-align:top">{html.escape(t.sector or "—")}<br><span style="color:#6b7280;font-size:12px">{mcap_str}</span></td>
      <td style="padding:6px 10px;vertical-align:top;font-size:12px">{reasons_html}</td>
      <td style="padding:6px 10px;vertical-align:top"><a href="{html.escape(t.pdf_url)}">Form 4</a></td>
    </tr>
    """


def render_email_html(trades: List[Form4Trade]) -> str:
    sorted_t = sorted(trades, key=lambda t: (SEVERITY_RANK.get(t.severity, 9), -t.filing_date.toordinal()))
    rows = "".join(_row(t) for t in sorted_t)
    by_sev = {k: 0 for k in BADGE}
    for t in trades:
        if t.severity in by_sev:
            by_sev[t.severity] += 1
    if any(by_sev[k] for k in ("high", "moderate", "low")):
        head = (
            f"<p><strong>{len(trades)} new insider transaction(s)</strong> — "
            f"<span style='color:#b91c1c'>🔴 {by_sev['high']} strong</span> · "
            f"<span style='color:#c2410c'>🟠 {by_sev['moderate']} some</span> · "
            f"<span style='color:#a16207'>🟡 {by_sev['low']} weak</span> · "
            f"<span style='color:#6b7280'>⚪ {by_sev['none']} none</span>.</p>"
        )
    else:
        head = f"<p><strong>{len(trades)} new insider transaction(s)</strong> — none with strong signal today.</p>"
    if not trades:
        rows = '<tr><td colspan="15" style="padding:30px;text-align:center;color:#666">No new insider transactions since the last run.</td></tr>'

    return f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111">
      <h2 style="margin-bottom:4px">🏢 Insider trades — daily digest</h2>
      {head}
      <p style="color:#6b7280;font-size:13px;margin-top:0">
        🔴 Open-market BUY by C-suite OR a cluster (2+ insiders) OR size ≥ $250k.
        🟠 Open-market BUY by Director/officer, mid-size, or large C-suite sells.
        🟡 Smaller buys or C-suite sells. ⚪ Other open-market trades.
        Option exercises, tax-payment sales, gifts and awards are filtered out.
      </p>
      <table style="border-collapse:collapse;border:1px solid #ddd;font-size:13px;width:100%">
        <thead style="background:#f5f5f5">
          <tr>
            <th style="padding:6px 10px;text-align:left">Flag</th>
            <th style="padding:6px 10px;text-align:left">Insider</th>
            <th style="padding:6px 10px;text-align:left">Company / Ticker</th>
            <th style="padding:6px 10px;text-align:left">Side</th>
            <th style="padding:6px 10px;text-align:right">Shares</th>
            <th style="padding:6px 10px;text-align:right">Price</th>
            <th style="padding:6px 10px;text-align:right">Value</th>
            <th style="padding:6px 10px;text-align:left">Trade date</th>
            <th style="padding:6px 10px;text-align:left">Filed</th>
            <th style="padding:6px 10px;text-align:left">Cluster?</th>
            <th style="padding:6px 10px;text-align:right">Price now</th>
            <th style="padding:6px 10px;text-align:right">% since trade</th>
            <th style="padding:6px 10px;text-align:left">Sector / Mkt cap</th>
            <th style="padding:6px 10px;text-align:left">Why flagged</th>
            <th style="padding:6px 10px;text-align:left">Source</th>
          </tr>
        </thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#6b7280;font-size:12px;margin-top:16px">
        Source: SEC EDGAR Form 4 (free, official; insider must file within 2 business days).
        Scoring matrix: <code>src/scoring.py</code>. Prices via Polygon/yfinance.
      </p>
    </body></html>
    """


def send_email(subject: str, html_body: str) -> None:
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    sender = os.environ.get("ALERT_FROM", user)
    recipient = os.environ["ALERT_TO"]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("HTML email — view in an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP(host, port) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(sender, [recipient], msg.as_string())
