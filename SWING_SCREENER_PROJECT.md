# Swing Screener Project — State Document

**Last updated:** 2026-02-18
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
- Persistent deletes implemented (git push from Railway via GITHUB_TOKEN env var)
- Deployed on Railway: **https://web-production-e3025.up.railway.app**

**KNOWN ISSUE — Persistent deletes broken:**
- UI deletes update entry_dates.json but DON'T delete CSV/chart files from repo
- 4 orphans (INOD, PLTR, SEI, TTMI) cleaned manually on 2026-02-18
- 4 MORE deletions didn't persist to entry_dates.json — entry_dates.json has 19, should be 15
- NEXT CHAT: Hit Railway `/api/examples` to get actual 15 tickers, sync repo, fix delete code

**What's next:**
- Fix persistent delete bug (delete CSVs + charts + extension data, not just entry_dates.json)
- Sync repo to match the 15 tickers actually in the app
- Validate 50% measured move hypothesis (best-profit 3-4DBs bounce ~50% of prior swing)
- Feature 2: Upload screenshot flow (TC2000 chart → extract ticker/date → auto-load)

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
- 19 examples with pinned entry dates and signal day analysis
- 9 PCF conditions, 100% pass rate (zero false negatives)
- Examples: CLSK, PATH, LEU, FLNC, REAL, OPEN, AEVA, IREN, BITF, HIVE, QS, PL, NQ, TSSI, ONDS, RR, OKLO, APP, BE

### 2. DTSS — Double Top Short Sell (SHORT setup)
- Failed breakout at resistance, short at the rejection
- Description scaffolded in repo, no examples yet

### 3. HTF — High Tight Flag (LONG setup)
- Tight consolidation after a strong run, breakout long
- Description scaffolded in repo, no examples yet

---

## ARCHITECTURE

**Backend:** FastAPI (Python) — `server.py`
- `/api/ohlcv?ticker=X&date=Y` — fetches OHLCV + indicators via yfinance
- `/api/setups` — lists setup types
- `/api/examples/{setup_type}` — lists saved examples
- `/api/conditions/{setup_type}` — returns PCF conditions

**Frontend:** Single-page React app — `app/index.html`
- Dashboard, Examples, Add Example, Conditions, Validate pages
- Interactive candlestick chart (HTML5 Canvas)
- Dark theme matching TC2000 chart preferences

**Hosting:** Railway — `web-production-e3025.up.railway.app`

---

## REPO STRUCTURE

```
swing-screener/
├── server.py                # FastAPI backend
├── Procfile                 # Railway deployment
├── requirements.txt         # fastapi, uvicorn, yfinance, pandas
├── SWING_SCREENER_PROJECT.md # This file — project state
├── config.yaml              # Chart config (MAs, dark theme, etc.)
├── data/
│   └── ohlcv/
│       └── 3-4db/           # 21 CSV files + entry_dates.json + signal_day_analysis.json
├── setup_library/
│   ├── 3-4db/
│   │   ├── description.md
│   │   ├── conditions.json
│   │   └── examples/
│   ├── dtss/
│   │   └── description.md
│   └── htf/
│       └── description.md
├── app/
│   └── index.html           # React frontend (single file)
├── scripts/
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
