# Swing Screener Project — State Document

**Last updated:** 2026-02-25
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener

---

## RULES — READ THESE FIRST

1. **NEVER proceed with work until user gives EXPLICIT go-ahead.** A question about doing something is NOT permission. Wait for "go", "yes", "do it", etc. This is the #1 rule.
2. **If you need credentials, access, tokens, or anything — STOP and ask immediately.** No workarounds.
3. **Push all work to GitHub before ending a chat.** Sandbox resets between sessions.
4. **Keep chats focused on one task.** Finish one task, push results, wrap up.
5. **Don't repeat context the user already knows.** Be concise.
6. **NEVER dump large data (CSV, JSON) into context.** Process via scripts, not inline.
7. **GitHub token for bash git push:** Stored in Claude project file. Use bash `git push`, NOT MCP push_files (payload limits).
8. **Before ANY TA work — READ `ta_knowledge.md` FIRST.** Non-negotiable.
9. **All OHLCV data from Railway SQLite DB.** Never yfinance. Query the API or use local caches.
10. **Break work into small tasks.** Update `TODO.md` and `ANALYSIS_SYSTEM.md` when finishing tasks.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~4,000 tradable tickers nightly, finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**The system:** Upload example trades for any setup type → the "pyramid grinder" automatically discovers which mathematical conditions best separate those examples from the full market → produces a tight scan that fires ~2-7 signals/day historically across 5 years.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## CURRENT STATE (2026-03-01)

### What's built and working:
- **Pyramid grinder** — 6-tier nested search (D1 → 1wk → 1mo → 6mo → 1yr → 5yr), peak-based scoring, beam=10000 exhaustive search
- **Signal expression library** — 12,175 expressions: 4,017 daily across 29 categories, 80 LSP levels, 44 algo lines, 8,034 HTF (weekly + monthly)
- **Exit expression library** — 6,410 expressions: 446 base (LSP + algo + AVWAP + entry-relative + 12 other categories) + 5,964 boolean aggregations
- **Exit grinder** — brute-forces 6,410 expressions against forward paths, 100% example pass hardcoded, timestamped + latest saves. Single-stage result: 71% median MFE capture.
- **Expression series cache** — 4,119 tickers × 12,175 expressions pre-cached (~50 GB), 14x grinder speedup
- **Nightly pipeline** — auto-triggers 4:30pm ET: Railway append → daily cache → 5yr cache → expr cache → matrix rebuild
- **Backtest runner** — scans 5yr history, generates charts per signal, auto-uploads to Railway
- **ScanPerfect web app** — Railway-deployed frontend with gallery, historical signal visualization, SPY bubble overlay
- **Railway SQLite DB** — 11M+ OHLCV rows, ~4,167 tradable tickers, 5yr daily data

### DTSS (Double Top Short Sell) — first completed setup:
- 20 validated examples (in exit grinder), 26 validated examples (in signal grinder, includes tickers not in 5yr cache)
- Signal grind: **41 conditions, peak 3/day, 264 total signals across 5yr** (multi-pass with algo lines)
- Exit grind (single-stage): `avg_range_atr_10b above 1.0541` — 71% median capture eff, 20/20 pass
- Backtest runner complete, Historical tab with signal prevalence + SPY overlay

### What's next (per TODO.md):
- **Task 3.7: Multi-Stage Exit Grinder** — replace single-condition exit with sequential stages (capital protection → trend riding → trailing). Single-stage hit 71% ceiling; multi-stage should push into excellent range.
- **Task 4: Formation Period Validation** — exit conditions must not fire before entry date
- **Step 7: Market Context** — correlate signal outcomes with market regime
- Future: 3-4DB setup, HTF setup, frontend grinder control

---

## ARCHITECTURE

```
local_runner/          # Desktop grinder system (runs on Dan's machine)
├── agent.py           # Polling agent, nightly auto-rebuild trigger
├── nightly.py         # 5-step nightly pipeline
├── pyramid_grinder.py # The grinder: 6-tier peak-based beam search
├── spiderweb.py       # Phase 1 beam search (used by D1 tier)
├── matrix_builder.py  # Universe + example matrix precomputation
├── brute_expressions.py  # 12,175 expression generator (daily + LSP + algo + HTF)
├── cache_builder.py   # OHLCV caches (daily + 5yr)
├── expr_cache_builder.py # Expression series cache (~21 GB)
└── cache/             # Local caches (matrices, OHLCV, expr series)

scripts/               # Analysis & backtesting scripts
├── expression_engine.py    # Expression computation (point + series)
├── backtest_conditions.py  # Series computation (88 ops, full parity)
├── backtest_runner.py      # Signal scan + chart generation
├── signal_distribution.py  # Signal analyzer
├── fetch_universe.py       # Universe OHLCV fetcher (full + incremental)
└── [profiling, discovery, management engines]

server.py              # Railway FastAPI backend (~90K, 14+ endpoints)
app/index.html         # ScanPerfect frontend (React SPA)
```

### Key docs (in repo):
- **`TODO.md`** — task list, pipeline status, build plan, benchmarks. **Check this first.**
- **`ANALYSIS_SYSTEM.md`** — the repeatable 8-step process for building any setup
- **`ta_knowledge.md`** — TA concepts: extensions, AVWAP, channels, market stages
- **`pcf.md`** — TC2000 PCF language reference

---

## SETUP TYPES

| Setup | Status | Examples | Grind Result |
|-------|--------|----------|-------------|
| **DTSS** (Double Top Short Sell) | ✅ Through Step 6 | 26 validated | 26 conditions, peak 6/day, 201 signals/5yr |
| **3-4DB** (3-4 Day Bounce, short) | Examples loaded | 21 examples | Not yet ground through pyramid |
| **HTF** (High Tight Flag, long) | Scaffolded | None yet | — |

---

## SUBSCRIPTIONS / TOOLS

- Claude Max — this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — app hosting (swing-screener, ttm-metrics-api, ttm-dashboard, discord-bot)
- Discord — trading community

---

## KEY LINKS

- **Railway app:** https://web-production-e3025.up.railway.app
- **Repo:** https://github.com/schultzdanielj-del/swing-screener
