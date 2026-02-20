# Swing Screener Project — State Document

**Last updated:** 2026-02-19
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener

---

## RULES — READ THESE FIRST

1. **NEVER proceed with work until user gives EXPLICIT go-ahead.** A question about doing something is NOT permission. Wait for "go", "yes", "do it", etc. This is the #1 rule.
2. **If you need credentials, access, tokens, or anything to make work smooth — STOP and ask immediately.** Don't try workarounds. Don't fight tooling. Just ask.
3. **Push all work to GitHub before ending a chat.** The sandbox resets between sessions. Anything not pushed is lost.
4. **Update this state doc and push it at the end of every chat.** This is the source of truth for the next chat.
5. **Keep chats focused on one task.** Don't try to do 5 things in one chat. Finish one task, push results, wrap up.
6. **Don't repeat context the user already knows.** Be concise. Don't recap the whole project history every time.
7. **NEVER dump large data (CSV, JSON) into context.** Read files to check structure only (head/tail). Process data via scripts, not inline.
8. **GitHub token for bash git push:** Stored in Claude project file (not in repo). Use for cloning and pushing via bash terminal, NOT the MCP push_files tool which has payload limits that cause failures on larger pushes.

---

## CURRENT STATUS

**What's done:**
- App renamed **ScanPerfect** — gallery grid with pre-rendered charts, 3-column layout
- Charts centered on entry date (30 before / 30 after), crosshair at open price
- Extension data: 5yr % from 50/200 SMA for all tickers, stored in `data/extension/`
- View toggles: D1 / 50 SMA % / 200 SMA %, At Entry / After time views
- Add inline (ticker + date), editable entry dates, delete functionality
- Deployed on Railway: **https://web-production-e3025.up.railway.app**
- **SQLite on Railway persistent volume** — all data in DB, no more flat files
- DB constraint fixed: UNIQUE(setup_type, ticker, entry_date) — supports multiple examples per ticker
- All API endpoints use example ID (not ticker) for routing
- Repair endpoint: POST /api/repair-data — re-fetches missing OHLCV/extension data
- **14 validated 3-4DB examples** (REAL removed), 18 scan conditions
- ARM 11/3/2025 added as clean 3-4DB example
- MARA 10/27/2025 evaluated — fails 4/18 conditions, skipped
- BB 11/6/2025 rejected — not a 3-4DB (prolonged consolidation)

**Universe data fetch — IN PROGRESS:**
- Fetching 5yr daily OHLCV for 11,668 tickers (full NYSE + NASDAQ + ETFs)
- Stored in `universe_ohlcv` table (separate from examples)
- Bulletproof: resumable, auto-retry, batched commits, progress tracking
- API endpoints: POST /api/universe/fetch, GET /api/universe/status, POST /api/universe/load-file
- Batch size 40, 8s delay — estimated 1-2 hours total
- Ticker list loaded via POST /api/universe/load-file (reads universe_tickers.txt from repo)
- File at repo root (NOT data/) because Railway volume mount shadows data/

**What's next:**
- Verify universe fetch completed successfully
- Build backtesting: run 18 scan conditions against full universe × 5 years
- Build daily refresh script (append 1 day of data, ~6 min for full market)
- TA brainstorming with full market data (find examples, test hypotheses)
- Add DTSS and HTF setup examples
- Validate 50% measured move hypothesis across more examples

---

## PROJECT PIVOT — CURRENT DIRECTION

**Original approach:** Vision-based chart screening using Claude API to match charts against a setup library. **Abandoned** because it requires API spend ($29-291/month on top of Max subscription).

**Current approach:** Build precise TC2000 PCF code scans that replicate what the user's eye does — so the scan output itself is tight enough to manually vet a small handful of charts. No additional cost. Plus a web app to manage examples, validate conditions, and iterate on setups.

---

## THE PROBLEM

User scans ~200 tickers nightly from TC2000, manually reviews every chart, categorizes as:
- **Actionable** — ready for next-day entry
- **NMS** — Need More Sideways (developing, not ready yet)

This takes 1-2 hours nightly. The real goal is screening 1000+ tickers down to ~10 candidates automatically.

**Trading style:** Qullamaggie-style swing trading, 3-day to multi-week holds, 50-70% win rate potential.

---

## SETUP TYPES (3 defined so far)

### 1. 3-4DB — 3-4 Day Bounce (SHORT setup) — PCF COMPLETE
- Stock pulls back after a move, bounces weakly for 3-4 days, then fails
- 14 examples with pinned entry dates and signal day analysis (REAL removed)
- 18 PCF conditions, all 100% pass rate (except #5 at 93%)
- Examples: AEVA, ARM, BE, BITF, CLSK, HIVE, IREN, LEU, OKLO, ONDS, OPEN, PATH, PL, QS, RR

### 2. DTSS — Double Top Short Sell (SHORT setup)
- Failed breakout at resistance, short at the rejection
- Description scaffolded in repo, no examples yet

### 3. HTF — High Tight Flag (LONG setup)
- Tight consolidation after a strong run, breakout long
- Description scaffolded in repo, no examples yet

---

## ARCHITECTURE

**Backend:** FastAPI (Python) — `server.py` (~650 lines)
- SQLite on Railway persistent volume (/app/data/scanperfect.db)
- Tables: examples, ohlcv, extension, conditions, signal_analysis
- Universe tables: universe_ohlcv (5yr market data), universe_tickers, universe_fetch_status
- All API endpoints use example ID for routing (not ticker)
- `/api/examples/{setup_type}` — lists saved examples
- `/api/conditions/{setup_type}` — returns PCF conditions
- `/api/universe/fetch` — kick off background universe data fetch
- `/api/universe/status` — check fetch progress
- `/api/universe/load-file` — load tickers from bundled file

**Frontend:** Single-page React app — `app/index.html`
- Dashboard, Examples, Add Example, Conditions, Validate pages
- Interactive candlestick chart (HTML5 Canvas)
- Dark theme matching TC2000 chart preferences

**Hosting:** Railway — `web-production-e3025.up.railway.app`
- Volume mounted at /app/data for SQLite DB
- NOTE: Volume mount shadows repo's data/ directory

---

## REPO STRUCTURE

```
swing-screener/
├── server.py                # FastAPI backend (~650 lines)
├── Procfile                 # Railway deployment
├── requirements.txt         # fastapi, uvicorn, yfinance, pandas
├── SWING_SCREENER_PROJECT.md # This file — project state
├── config.yaml              # Chart config (MAs, dark theme, etc.)
├── universe_tickers.txt     # 11,668 tickers (NYSE + NASDAQ + ETFs) — at root, NOT data/
├── data/                    # NOTE: shadowed by Railway volume mount at /app/data
│   └── ohlcv/
│       └── 3-4db/           # Legacy flat files (data now in SQLite)
├── setup_library/
│   ├── 3-4db/
│   │   ├── description.md
│   │   ├── conditions.json  # 18 scan conditions
│   │   └── examples/
│   ├── dtss/
│   │   └── description.md
│   └── htf/
│       └── description.md
├── app/
│   └── index.html           # React frontend (single file)
├── scripts/
│   ├── __init__.py
│   ├── fetch_universe.py    # Bulletproof universe OHLCV fetcher
│   └── run_nightly.py       # CLI entry point (old)
└── src/                     # Old vision pipeline code (to be archived)
```

---

## CHART PREFERENCES

- Dark background (custom nightclouds style)
- Green (#26A69A) up candles, Red (#EF5350) down candles
- 8 EMA (light blue), 21 EMA (tan), 50 SMA (yellow), 200 SMA (red)
- Volume bars displayed
- AVWAP: hot pink, anchored to highest value at current day
- 120 day lookback

---

## EXISTING SUBSCRIPTIONS / TOOLS

- Claude Max ($200/month, $140 CAD) — used for this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — app hosting (swing-screener + other projects: ttm-metrics-api, ttm-dashboard, discord-bot)
- Discord — trading community

---

## BACKTEST RESULTS (as of 2026-02-20)

Full 5-year backtest of 18 PCF conditions across 4,167 tradable tickers complete.

**Raw results:** 1,217 signals from 444 unique tickers (2022-02-17 to 2026-02-19)
**Clean results:** 802 signals from 292 unique tickers (filtered biotech, leveraged/inverse ETFs)

### Filters applied:
- Removed 110 biotech tickers (by industry classification via yfinance)
- Removed 40 ETFs + all leveraged/inverse products
- Sector/industry data stored in `ticker_sectors` table

### Signals by year (clean):
| Year | Signals | Unique Tickers |
|------|---------|----------------|
| 2022 | 57 | 30 |
| 2023 | 92 | 50 |
| 2024 | 206 | 91 |
| 2025 | 359 | 161 |
| 2026 YTD | 88 | 50 |

### Database tables:
- `scan_backtest` — raw signals (all 18 conditions pass)
- `scan_backtest_clean` — filtered signals (no biotech/leveraged)
- `ticker_sectors` — sector, industry, is_etf flag for signal tickers
- `backtest_status` — progress tracking for background runs

### API endpoints:
- `GET /api/backtest/summary` — yearly breakdown + filter stats
- `GET /api/backtest/results?clean=true&limit=500` — browse signals
- `GET /api/backtest/results?ticker=APP` — filter by ticker
- `GET /api/backtest/results?date_from=2025-10-01&date_to=2025-11-01` — filter by date range
- `POST /api/backtest/run` — re-run full backtest (background task, ~30-60 min)
