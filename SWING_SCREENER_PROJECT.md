# Swing Screener Project — State Document

**Last updated:** 2026-03-07
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener
**Active branch:** `v2`

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
9. **All OHLCV data from Railway SQLite DB or local caches.** Never yfinance in pipelines.
10. **Break work into small tasks.** Update `TODO.md` and `SWING_SCREENER_PROJECT.md` when finishing tasks.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~4,000 tradable tickers nightly, finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**The system:** Upload example trades for any setup type → the "pyramid grinder" automatically discovers which mathematical conditions best separate those examples from the full market → produces a tight scan that fires ~1-2 signals/week historically across 5 years.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## CURRENT STATE (2026-03-07)

### Architecture: V2 — fully deployed and running

**Decision (2026-03-06):** V1 is archived. All work happens on the `v2` branch. V2 eliminates file-based handoffs — all state lives in Railway SQLite with cycle versioning.

**V1 originals archived at:** `archive/v1/server.py`, `archive/v1/index.html`

### What's built and working:

**Backend (`server.py`) — ~910 lines, V2-only, deployed to Railway:**
- All DB tables (V1 + V2 combined schema in `init_db()`)
- Pipeline/agent endpoints (steps, run, stop, logs, heartbeat)
- Examples CRUD (`/api/examples/{setup}`)
- Full vetting endpoints (upload-signals, GET signals, OHLCV, decide, earnings, rejected)
- Full pending/AI review endpoints (get, approve, reject, review, reset-reviews, approve-all, backfill)
- Universe OHLCV insert (`/api/universe/insert-ohlcv`)
- All V2 cycle management endpoints (`/api/v2/*`)

**Frontend (`app/index.html`) — fresh shell, V1 vetting copied verbatim:**
- Rail nav + sidebar for setup analysis
- `VettingChart` — candlestick chart, EMA/SMA overlays, earnings overlay, scroll/zoom
- `VettingPage` — full vetting UI: Step 2/Step 4 source toggle, YES/NO/MAYBE, keyboard shortcuts (↑↓/1/2/3), log streaming, agent status, RELOAD button
- `ExamplesPage` — Optimal Samples gallery, Pending/AI tab (AI verdict display, approve-all, approve/reject per item), Rejected tab, mini charts
- `CycleHealthPage` — fully wired to `/api/v2/*` — health panel, cycle selector, revert button, all metrics live
- `NightlyPage` — pipeline step display + log streaming
- `WatchlistPage` — placeholder (blocked on Market Grinder)

**V2 scripts (local, run on Dan's machine):**
- `scripts/grind_upload.py` — uploads pyramid grinder output to Railway as versioned cycle
- `scripts/scan_signals.py` — scans 5yr via expr cache, writes `cycle_signals` to Railway
- `scripts/cycle_health.py` — computes all health metrics, uploads `cycle_health` row, auto-promotes

**Expression library — 15,805 total expressions:**
- 4,017 daily + 80 LSP + 4,017 weekly + 4,017 monthly + 3,630 extension_structure
- Cache: ~50 GB, 4,119 tickers — **rebuilt 2026-03-07, current**
- `EXPR_CACHE_WORKERS=8` for builds

### DTSS current state:
- **71 validated examples** (3 excluded from cache: BRK-B, SMMT, VUZI)
- Grind #5: 94 conditions, 281 filtered signals over 5yr (~1/week forward)
- Exit condition locked: `slope_xavgc21_off7_adr14 ≤ -1.128826`, floor 3.2 ADR, median 5.8 ADR, avg 38 bars
- Grind output lives locally at `local_runner/cache/pyramid_results_dtss.json`
- **Active V2 cycle: `dtss_20260306_170830`** — PROMOTE, 68/68 examples passing, 41% win rate, EV 1.479
- Note: `n_examples_at_grind=68` (excludes BRK-B, SMMT, VUZI from the 71 total)

### scan_signals.py — is_example matching note:
Signals are matched to examples using proximity: for each example, the scanned signal
for that ticker closest to `entry_date` within ±7 calendar days is tagged `is_example=1`.
This handles the grinder's variable signal-to-entry offset correctly.

### What's next:
1. **Market Grinder (Step 5)** — V2-native, operates on `cycle_signals` table, design TBD

---

## ARCHITECTURE

```
local_runner/          # Desktop grinder system (runs on Dan's machine)
├── agent.py           # Polling agent, nightly auto-rebuild trigger
├── nightly.py         # 5-step nightly pipeline
├── pyramid_grinder.py # 6-tier peak-based beam search
├── spiderweb.py       # Phase 1 beam search (D1 tier)
├── matrix_builder.py  # Universe + example matrix precomputation
├── brute_expressions.py  # 15,805 expression generator
├── cache_builder.py   # OHLCV caches (daily + 5yr)
├── expr_cache_builder.py # Expression series cache (~50 GB)
└── cache/             # Local caches (matrices, OHLCV, expr series)
                       # ⚠ pyramid_results_dtss.json lives here (grind #5)

scripts/               # Analysis & pipeline scripts
├── expression_engine.py    # Expression computation
├── backtest_conditions.py  # Series computation (88 ops)
├── grind_upload.py         # V2: upload grinder output to Railway
├── scan_signals.py         # V2: scan 5yr via expr cache → cycle_signals
├── cycle_health.py         # V2: compute + upload health metrics
├── signal_filter.py        # V1 signal filter (still used by agent)
├── profit_grinder.py       # Single-stage exit grinder
├── multistage_exit_grinder.py  # Multi-stage exit grinder
├── setup_refiner.py        # Blackout condition pruner
└── [other analysis scripts]

server.py              # Railway FastAPI backend (~910 lines, V2-only)
app/index.html         # ScanPerfect frontend (React SPA, V2 shell)
archive/v1/            # V1 originals (read-only reference)
```

### Key docs (in repo):
- **`TODO.md`** — task list. **Check this first.**
- **`ANALYSIS_SYSTEM.md`** — repeatable pipeline process for building any setup
- **`DATA_CONTRACT.md`** — V2 DB schema and API contracts
- **`PIPELINE_V2.md`** — V2 architecture design
- **`ta_knowledge.md`** — TA concepts: extensions, AVWAP, channels, market stages
- **`pcf.md`** — TC2000 PCF language reference

---

## SETUP TYPES

| Setup | Status | Examples | Grind Result |
|-------|--------|----------|-------------|
| **DTSS** (Double Top Short Sell) | ✅ V2 pipeline live | 71 validated | Grind #5: 94 conditions, active cycle dtss_20260306_170830 |
| **3-4DB** (3-4 Day Bounce, short) | Examples loaded | ~21 examples | Not yet ground |
| **HTF** (High Tight Flag, long) | Scaffolded | None yet | — |

---

## KEY LINKS

- **Railway app:** https://web-production-e3025.up.railway.app
- **Repo:** https://github.com/schultzdanielj-del/swing-screener
- **Branch:** `v2`
