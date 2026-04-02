# Swing Screener Project — State Document

**Last updated:** 2026-04-01 (session 2)
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener (branch: v2)
**Railway:** https://web-production-e3025.up.railway.app (seed vault / file mirror only)

---

## RULES — READ THESE FIRST

1. **NEVER proceed with work until user gives EXPLICIT go-ahead.** Wait for "go", "yes", "do it".
2. **If you need credentials, access, tokens — STOP and ask immediately.**
3. **Push all work to GitHub before ending a chat.**
4. **Don't repeat context the user already knows.** Be concise.
5. **NEVER dump large data (CSV, JSON) into context.** Process via scripts.
6. **GitHub token for bash git push:** Stored in Claude memory. Use bash `git push`.
7. **Before ANY TA work — READ `ta_knowledge.md` FIRST.**
8. **All data from local files.** SQLite (`data/scanperfect.db`), OHLCV pickle, local cache JSONs. Never Railway. OHLCV uses EODHD bulk + yfinance gap fill. yfinance also used for fundamentals and earnings dates.
9. **Break work into small tasks.** Update docs when finishing tasks that alter them.
10. **READ THE ACTUAL CODE before making claims.** Verify, don't guess.
11. **At session start:** Read the full codebase before proposing anything.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~11,500 tickers nightly (Common Stock + ETF on NYSE/NASDAQ/NYSE ARCA/BATS, sourced from EODHD), finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## CURRENT STATE (2026-04-01)

### Expression Engine V2 — four-step plan:
- **Step 1 (OHLCV caches):** ✅ Complete — daily/weekly/monthly caches built and nightly append working
  - Daily: 11,523 tickers, 11,294 with today's bar (229 illiquid/delisted behind)
  - Weekly: 11,469 tickers
  - Monthly: 11,239 tickers
  - Nightly append: EODHD bulk (~95% in seconds) + yfinance gap fill (~5% in ~30s)
  - EODHD handles: universe sync (IPOs/delistings), bulk splits detection, full historical backfill
  - yfinance handles: same-day bars not yet published by EODHD, fundamentals, earnings dates
- **Step 2 (expression cache full rebuild):** ✅ Complete — 11,201 tickers, 0 failures, 111 GB, 124 min. Float16 storage + 6yr window (2020-01-02). HTF look-ahead bias FIXED via partial candle engine.
- **Step 3 (universe matrix):** ✅ Complete — 11,201 tickers × 15,805 expressions, 1.35 GB, 148s. Reads last bar from expr cache .npz files.
- **Step 4 (expression cache incremental append):** Infrastructure DONE (2026-04-01). `_append_one_ticker` worker, `.append`/`.append_dates` binary storage, `load_ticker_cache` vstack, `signal_filter._load_ticker_npz` updated. Validated 50/50 tickers zero mismatches. Currently still runs `_compute_ticker_full` internally (~100 min). Next: replace with forward-propagation phases 0-4 to reach ~13 min target.
- **Market cache:** ✅ EODHD migration DONE (2026-04-01). 272 instruments (268 fetched successfully, 4 FRED series still down). ~227 US ETFs read from daily OHLCV pickle, indices/crypto/breadth from EODHD, futures from yfinance, 4 FRED macro series remaining. 5 derived instruments computed locally (NYMO_CALC, NYUD_CALC, NDXADP_CALC, T10Y2Y_CALC, T10Y3M_CALC). Stooq replaced by EODHD.

### DTSS (Double Top Short Sell) — first setup:
- **Examples:** 66 (65 with valid scan bars in clusters, out of ~365 winner clusters)
- **Causative (Phase 2):** Complete — 87 signal conditions + 100 refinement conditions, 182 combined, 78% WR, median winner 6.4 ADR
- **Correlative (Phase 3):** Complete — EV Grinder inc 1-6 done. 1,816 pre / 1,940 post features. D1→D10 spread +45pp WR. RMSE 0.090
- **Profit (Phase 4):** ✅ Inc 1-4 done (835 1-stage, 7703 2-stage, ~12 min)
- **Live Watchlist (Phase 5):** Not built

### Native Desktop UI (Phase 6):
- PySide6 app (`scanperfect.py`) — IN PROGRESS
- Pipeline flowchart with 7 color-coded nodes, two feedback loops, unlock progression
- Animated card expansion with Run/Stop/Log for grind nodes
- Replaces browser-based HTML UI (server.py + app/*.html)
- Next: expand DO nodes (Examples, Vetting) into full workspaces within the flowchart

---

## ARCHITECTURE

Everything runs locally on Dan's desktop (i5-12600k, 32GB RAM, Windows).

```
scanperfect.py             # PySide6 native desktop app (THE interface)
audit.py                   # Code auditor — separate Claude instance reviews every change

local_runner/              # Grinder system
├── nightly.py             # 10-step nightly pipeline (5:00pm ET)
├── pyramid_grinder.py     # Signal grind + refinement grind (--blackout)
├── cache_builder.py       # OHLCV caches (daily + HTF) — EODHD bulk + yfinance hybrid
├── expr_cache_builder.py  # Expression series cache (~111 GB, float16, 6yr)
├── market_cache_builder.py # 272 instrument expression series (EODHD + pickle + yfinance + FRED)
├── file_mirror.py         # Railway backup mirror
└── cache/                 # All local caches + grind output JSONs

scripts/                   # Analysis scripts
├── ev_grinder.py          # EV grinder (Phase 3) — COMPLETE
├── exit_grinder.py        # Exit condition optimizer
├── entry_candle_scorer.py # Vetting sort utility
├── seed_vault.py          # Backup/restore to Railway
├── fetch_fundamentals.py  # Yahoo Finance fundamentals
└── signal_filter.py       # Signal scan (standalone analysis)

data/                      # Local data
├── scanperfect.db         # SQLite DB (examples, setups, etc.)
├── pipeline_state.json    # Grinder run status
├── pipeline_logs.json     # Grinder log output
└── vetting/               # Vetting decision files

server.py                  # FastAPI (Railway deploy only — seed vault endpoints)
app/                       # HTML UI (LEGACY — replaced by scanperfect.py)
```

### Key docs:
- **`TODO.md`** — task list, pipeline status, immediate work items
- **`LOCALIZE.md`** — local migration (complete) + UI status
- **`UI_FLOW.md`** — PySide6 flowchart UI design
- **`PIPELINE_V2.md`** — pipeline architecture
- **`EV_GRINDER.md`** — EV grinder spec
- **`ANALYSIS_SYSTEM.md`** — repeatable process for building any setup
- **`DATA_CONTRACT.md`** — schema + data flow
- **`DEPENDENCY_MAP.md`** — per-component inputs, outputs, upstream callers, downstream consumers. Check before changing any component.
- **`ta_knowledge.md`** — TA concepts reference
- **`pcf.md`** — TC2000 PCF language reference
- **`Code_Auditor`** — code auditor setup and spec

---

## CODE AUDITOR

`audit.py` — runs a separate Claude Code instance (`claude -p`) that evaluates every code change against spec docs and project-wide rules. Checks four criteria: purpose, spec compliance, regression safety, and code quality. Returns PASS or FAIL with evidence.

Git hooks (local, not in repo):
- **post-commit** — fires on every local commit (Claude Code workflow)
- **post-merge** — fires on every `git pull`. Auto-reverts on FAIL. Paste failure output into Claude chat to fix.

The auditor maps changed files to their spec docs automatically (e.g. `ev_grinder.py` → `EV_GRINDER.md`). Project-wide rules (OHLCV uses EODHD bulk + yfinance gap fill, ProcessPoolExecutor only, del+gc patterns, etc.) are hardcoded in the auditor prompt.

---

## SETUP TYPES

| Setup | Status | Examples | Phase 2 Result |
|-------|--------|----------|----------------|
| **DTSS** | Phase 2+3 complete, Phase 4 done | 68 | 182 conditions, 78% WR, 6.4 ADR median |
| **BRKO** | Phase 3 (EV Grinder running) | 51 | 54.9% WR post-refinement, thin — edge in market context |
| **3-4DB** | Examples loaded | 21 | Not yet ground |
| **HTF** | Scaffolded | None | — |

---

## SUBSCRIPTIONS / TOOLS

- Claude Max — this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — seed vault / file mirror only
- Discord — trading community
- PySide6 — desktop UI framework (installed, in use)
- EODHD — EOD Historical Data API ($19.99/mo). Universe management (exchange symbol list, IPOs, delistings), bulk splits detection, full historical backfill, bulk daily bars. Limits: 100K API calls/day (EOD = 1 call/ticker, bulk = 100 flat). 1,000 HTTP requests/min. Daily counter resets on first request after midnight GMT. Note: bulk endpoint for current day is incomplete until ~6-8 hours after close; yfinance fills the gap. API key in Claude memory.
- yfinance — Free. Same-day OHLCV bars (available immediately after close), fundamentals (Yahoo quoteSummary API), earnings dates (yf.Ticker). Unofficial scraper — rate limits apply, use batch downloads with pauses.
