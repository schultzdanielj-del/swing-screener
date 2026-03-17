# Swing Screener Project — State Document

**Last updated:** 2026-03-16
**GitHub repo:** https://github.com/schultzdanielj-del/swing-screener (branch: v2)
**Railway app:** https://web-production-e3025.up.railway.app (seed vault / file mirror only)

---

## RULES — READ THESE FIRST

1. **NEVER proceed with work until user gives EXPLICIT go-ahead.** A question about doing something is NOT permission. Wait for "go", "yes", "do it", etc. This is the #1 rule.
2. **If you need credentials, access, tokens, or anything — STOP and ask immediately.** No workarounds.
3. **Push all work to GitHub before ending a chat.** Sandbox resets between sessions.
4. **Keep chats focused on one task.** Finish one task, push results, wrap up.
5. **Don't repeat context the user already knows.** Be concise.
6. **NEVER dump large data (CSV, JSON) into context.** Process via scripts, not inline.
7. **GitHub token for bash git push:** Stored in Claude memory. Use bash `git push`, NOT MCP push_files (payload limits).
8. **Before ANY TA work — READ `ta_knowledge.md` FIRST.** Non-negotiable.
9. **All OHLCV data from local caches (5yr pickle, expr cache).** Never yfinance. Railway DB is legacy.
10. **Break work into small tasks.** Update `TODO.md` and `ANALYSIS_SYSTEM.md` when finishing tasks.
11. **READ THE ACTUAL CODE before making claims about what scripts do.** Verify, don't guess.
12. **At session start:** Read the full codebase before proposing anything. With 1M context window, no need for Dan to specify which files — read everything.

---

## WHAT THIS PROJECT IS

Automated swing trade screener. Screens ~4,000 tradable tickers nightly, finds the handful that match validated setup patterns with mathematically optimal conditions. Qullamaggie-style, 3-day to multi-week holds.

**The system:** Upload example trades for any setup type → the pyramid grinder discovers which mathematical conditions best separate those examples from the full market → refinement grind eliminates losing signals → EV grinder scores every signal by expected value → nightly watchlist ranks the best opportunities.

**Cost:** $0 additional — runs on existing Claude Max + TC2000 + Railway + GitHub.

---

## CURRENT STATE (2026-03-16)

### Pipeline Architecture

```
Phase 1 — Sample Gathering (Vetting)
Phase 2 — Causative Filtering (Signal Grind → Exit Grind → Refinement Grind)
Phase 3 — Correlative Scoring (EV Grinder) ← IN PROGRESS (Inc 5 next)
Phase 4 — Profit Optimization
Phase 5 — Live Watchlist
```

### DTSS (Double Top Short Sell) — first setup:
- **Phase 1:** 68 examples (65 with valid scan bars in clusters)
- **Phase 2a - Signal Grind:** 87 conditions, 1,218 raw → 893 deduped signals, 42.6% WR
- **Phase 2b - Exit Grind:** `slope_xavgc21_off7_adr14 <= -1.128826`
- **Phase 2c - Refinement Grind:** 100 refinement conditions, 182 combined, 426/528 losing clusters eliminated, 78% WR, median winner 6.4 ADR
- **Phase 3:** EV Grinder in progress — Inc 1-4 complete, Inc 5 (scoring curves) next

### Local Migration (completed 2026-03-16):
- Everything runs locally on Dan's desktop — no Railway dependency for compute or UI
- Local SQLite DB (`data/scanperfect.db`) with all tables
- 5yr OHLCV loaded into memory from pickle (~4,169 tickers in 0.5s)
- Pipeline runs as direct subprocesses (no agent polling)
- Seed vault backs up everything to Railway nightly (step 9)
- See `LOCALIZE.md` for full details

### What's next:
- **Phase 6: Native Desktop UI** — PySide6 app replacing browser-based HTML UI (see LOCALIZE.md)
- **EV Grinder Inc 5** — scoring curves + quartile assignment + signal scoring
- **Vet winner pile** — review 365 winners, add examples, loop

---

## ARCHITECTURE

```
scanperfect.py         # Desktop app launcher (PySide6 — PLANNED, currently browser-based)
server.py              # FastAPI backend (local mode + Railway deploy)

local_runner/          # Desktop grinder system (runs on Dan's machine)
├── nightly.py         # 9-step nightly pipeline (including seed vault backup)
├── pyramid_grinder.py # Signal grind (step 1) + refinement grind (--blackout)
├── spiderweb.py       # Phase 1 beam search (used by D1 tier)
├── matrix_builder.py  # Universe + example matrix precomputation
├── brute_expressions.py  # 15,805 expression generator
├── cache_builder.py   # OHLCV caches (daily + 5yr) — pulls from Railway
├── expr_cache_builder.py # Expression series cache (~21 GB)
├── market_cache_builder.py # Market instrument cache (256 instruments)
├── grind_uploader.py  # Railway cycle upload
├── file_mirror.py     # Railway file mirror
├── pipeline_agent.py  # LEGACY — replaced by direct subprocess in server.py
└── cache/             # Local caches (matrices, OHLCV, expr series)

scripts/               # Analysis & backtesting scripts
├── ev_grinder.py      # EV grinder (Phase 3) — IN PROGRESS
├── expression_engine.py    # Expression computation (point + series)
├── backtest_conditions.py  # Series computation (88 ops, full parity)
├── signal_filter.py        # Signal scan + exit filter + classification
├── signal_exit_grinder.py  # Exit condition optimizer
├── exit_grinder.py         # Exit expression search
├── seed_vault.py           # Backup/restore to Railway file mirror
├── import_from_railway.py  # One-time migration from Railway → local DB
├── fetch_fundamentals.py   # Yahoo Finance fundamentals fetch
├── cycle_health.py         # Health check metrics
├── fetch_universe.py       # Universe OHLCV fetcher
└── [profiling, discovery, management engines]

app/                   # Browser UI (being replaced by PySide6 native app)
├── index.html         # Main UI (Pipeline + Examples tabs)
├── pipeline.html      # Pipeline control tab
└── vetting.html       # Chart vetting tab

data/                  # Local data (gitignored: scanperfect.db, pipeline state)
├── scanperfect.db     # Local SQLite DB (all tables)
├── signal_filter/     # Signal filter outputs
├── vetting/           # Vetting decision files
└── [grind output archives]

archive/               # Shelved code, docs, and data (preserved, not in use)
```

### Key docs (in repo):
- **`TODO.md`** — task list, pipeline status, immediate work items. **Check this first.**
- **`LOCALIZE.md`** — local migration plan + Phase 6 UI rebuild plan
- **`PIPELINE_V2.md`** — authoritative pipeline architecture
- **`EV_GRINDER.md`** — EV grinder spec
- **`ANALYSIS_SYSTEM.md`** — repeatable process for building any setup
- **`DATA_CONTRACT.md`** — SQLite schema, input/output per step
- **`GRIND_STORAGE.md`** — how grind results are stored and uploaded
- **`ta_knowledge.md`** — TA concepts: extensions, AVWAP, channels, market stages
- **`pcf.md`** — TC2000 PCF language reference

---

## SETUP TYPES

| Setup | Status | Examples | Phase 2 Result |
|-------|--------|----------|----------------|
| **DTSS** (Double Top Short Sell) | Phase 2 complete, Phase 3 in progress | 68 (65 valid) | 182 conditions, 78% WR, median winner 6.4 ADR |
| **3-4DB** (3-4 Day Bounce, short) | Examples loaded | 21 examples | Not yet ground |
| **HTF** (High Tight Flag, long) | Scaffolded | None yet | — |

---

## SUBSCRIPTIONS / TOOLS

- Claude Max — this + other projects
- TC2000 — scanning platform
- GitHub — code hosting
- Railway — seed vault / file mirror only (no longer serves UI or compute)
- Discord — trading community
- PySide6 — desktop UI framework (installed, build pending)

---

## KEY LINKS

- **Repo:** https://github.com/schultzdanielj-del/swing-screener (branch: v2)
- **Railway (file mirror):** https://web-production-e3025.up.railway.app
