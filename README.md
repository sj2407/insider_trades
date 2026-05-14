# insider_trades

Daily alert when corporate insiders (CEOs, CFOs, COOs, directors, 10% owners)
trade their own company's stock on the open market — scoped to companies above
$100M market cap (Russell-3000-ish), scored by signal strength.

Sibling project to [congress_trades](https://github.com/sj2407/congress_trades).
Same architecture, different data source (SEC EDGAR Form 4), different signal.

## What it does

- Pulls **SEC EDGAR Form 4** filings daily (free, official, T+2 lag)
- Parses each filing's XML, extracts: who, what role, transaction code, shares, price, exact dollar value
- Filters out noise (option exercises, awards, tax-payment sales, gifts)
- Scores each transaction:
  - 🔴 **Strong** — open-market BUY by C-suite, cluster buy (2+ insiders / 30d), or buy ≥ $250k
  - 🟠 **Some** — open-market BUY by director/officer, mid-size, or large C-suite sell
  - 🟡 **Weak** — small buys or routine C-suite sells
  - ⚪ **None** — sells by directors / 10% owners, very small buys
- Drops trades on companies below $500M market cap (configurable via `MIN_MARKET_CAP`)
- Emails new transactions only (SQLite seen-store for idempotence)
- Includes prices at trade date, current price, % move since trade, cluster status

## Repo layout

```
src/
├── fetch_form4.py     SEC EDGAR daily-index + Form 4 XML parser
├── types.py           Trade dataclass with role classifier
├── scoring.py         Severity matrix + cluster detection
├── prices.py          Polygon → yfinance price/sector/market-cap lookups
├── notify.py          Email rendering
└── store.py           SQLite seen-store
main.py                Daily orchestrator
backfill_form4.py      Historical backfill from EDGAR daily indexes (slow, canonical)
backfill_finnhub.py    Historical backfill via Finnhub (fast, requires API key)
backtest_form4.py      Compute post-trade returns at +30d, +90d, +180d, today
dashboard.py           8-section narrative HTML dashboard
```

## Local setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill in SEC_USER_AGENT, POLYGON_API_KEY, FINNHUB_API_KEY
python main.py --dry-run --max-per-day 100  # quick smoke test
python main.py --preview-recent 2           # render last 2 business days
```

## Backfill + backtest workflow

```bash
# Option 1: fast (Finnhub, 500 tickers, ~10 min)
python backfill_finnhub.py --from 2023-01-01 --limit 500

# Option 2: canonical (EDGAR, ALL filings, slow — hours)
python backfill_form4.py --years 2024,2025 --workers 1

# Then:
python backtest_form4.py
python dashboard.py
open data/dashboard.html
```

## Scoring matrix (`src/scoring.py`)

Hand-tunable. Edit the constants `HIGH_BUY_USD`, `SOME_BUY_USD`, `CLUSTER_DAYS`,
`CLUSTER_THRESHOLD` and the per-rule classification logic to refine.

After editing, re-run `python dashboard.py` to see whether the historical signal
gap widens (good) or narrows (loosened too much).

## Daily scheduling

Runs as Anthropic Claude Code scheduled task `insider-trades-daily` at 20:00 ET
daily. The cloud session clones this repo each run, parses fresh Form 4s, emails
new flagged transactions via the gmail MCP, and commits state back to the repo
so tomorrow's run inherits prior state.

## Caveats

- Form 4 only captures stock transactions by **insiders**. 10b5-1 pre-planned
  sales are NOT separately tagged in the data — they look like ordinary sells.
- The `role_bucket` field comes from the Form 4 XML's `reportingOwnerRelationship`
  + a free-text `officerTitle`. Title text is messy; "Chief Stuff Officer" is a
  thing. Classification is best-effort.
- Sector + market-cap data comes from Polygon (or yfinance fallback). Some
  ticker metadata is missing on small-caps; those trades will have `sector="Unknown"`.
- The cluster signal only fires when we've seen multiple distinct insiders buying
  the same company in the last 30 days. Smaller companies with one or two
  insiders rarely cluster, so the signal is concentrated in mid/large-cap names.
