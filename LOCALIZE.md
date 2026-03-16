# Localize — Move Everything Local

## Why

Railway adds network latency to every operation. Vetting charts take 50-200ms per signal over the network. Local SQLite reads take microseconds. The pipeline agent polling pattern is unnecessary complexity when the server and agent are on the same machine.

## Architecture After Migration

**Local (your desktop):**
- Native PySide6 desktop app (`scanperfect.py`) — no browser, no server process
- SQLite DB (`data/scanperfect.db`): examples, setups, vetting decisions, earnings, pending reviews, grind cycles, conditions, signals, regime model, watchlist
- All caches: 5yr OHLCV pickle (in-memory), expression cache, market cache, fundamentals
- Pipeline runs as subprocesses launched directly from the app
- Start with: double-click `ScanPerfect.bat`

**Railway (seed vault only):**
- File mirror API: receives daily backups + grind result JSONs
- No UI, no server logic, no pipeline coordination
- Endpoint: `POST /api/v2/files` (already exists)
- Claude reads grind results from here during chat sessions

## Current Status

### Completed

**Phase 1 — Local Server** ✅
- `IS_RAILWAY` flag: DB defaults to `data/` locally, Railway volume path when deployed
- `init_db()` creates all 21 tables locally, seeds 3 setups
- `scripts/import_from_railway.py`: one-time migration of examples, pending, rejected, earnings

**Phase 2 — Eliminate Agent Polling** ✅
- Server launches pipeline subprocesses directly via background threads
- `STEP_COMMANDS` with `{setup}` placeholder for multi-setup support
- Agent status always "online" in local mode (server IS the agent)
- Stop endpoint terminates running subprocess

**Phase 3 — Wire Local OHLCV** ✅
- 5yr pickle loaded into memory at startup (~4,169 tickers in 0.5s)
- `_get_ohlcv()`, `_get_all_tickers()`, `_has_bar()` helpers
- All `universe_ohlcv` SQL queries replaced with in-memory cache reads locally
- yfinance fallback only on Railway

**Phase 4 — Seed Vault** ✅
- `scripts/seed_vault.py`: backs up all 14 SQLite tables + vetting JSON files to Railway
- `scripts/seed_vault.py --restore`: full disaster recovery from Railway
- Wired into `nightly.py` as step 9 (9 steps total)
- Covers everything needed for full rebuild: examples, setups, earnings, pending, rejected, grind cycles, conditions, signals, exit conditions, health, regime model, scores, watchlist, signal filter files, vetting decisions

### Next

**Phase 6 — Native Desktop UI (PySide6)**

Replace the browser-based HTML/JS UI with a native PySide6 desktop application. This eliminates the FastAPI server entirely — the app reads directly from SQLite + pickle cache.

Build approach:
1. Load all three HTML files (index.html, vetting.html, pipeline.html) + server.py endpoints into context
2. Rebuild the identical UI in PySide6 as a single `scanperfect.py`
3. Use QtCharts for interactive candlestick charts (zoom, pan, click-to-select entry date)
4. Read data directly from SQLite + in-memory OHLCV cache — no HTTP layer

PySide6 was chosen because:
- Prebuilt wheels for Python 3.14 on Windows (`pip install pyside6`)
- Built-in QtCharts for professional interactive trading charts
- Native window — no browser, no server process, no background services
- This is what professional trading terminals are built with
- Single dependency, zero compilation

Tabs to replicate:
- **Pipeline** — step status cards, run/stop buttons, log viewer
- **Examples** — grid of chart thumbnails with ADR moves, add/delete, bulk import
- **Vetting** — full-screen chart with signal navigation, yes/no/skip workflow, earnings overlay, keyboard shortcuts
- **Watchlist** — ranked signal list with scores (once EV grinder is complete)

After this phase, `server.py` is no longer needed for the local UI. It remains only for Railway deployment (file mirror).

**Phase 5 — Slim Railway** (low priority, do anytime)
1. Strip Railway server.py down to just the file mirror endpoints
2. Remove all pipeline, vetting, examples, agent endpoints
3. Keep: POST/GET/DELETE /api/v2/files, health check
4. Redeploy

## Seed Vault (Daily Backup)

Pushed to Railway as step 9 of the nightly refresh (4:30pm ET).

Contents (14 SQLite tables):
- `seed/examples.json` — all examples across all setups
- `seed/setups.json` — setup definitions
- `seed/earnings_dates.json` — earnings dates cache
- `seed/pending_examples.json` — pending AI reviews
- `seed/rejected_signals.json` — rejected signals
- `seed/grind_cycles.json` — grind cycle records (which is current)
- `seed/cycle_conditions.json` — scan conditions (the live scan)
- `seed/cycle_signals.json` — all classified signals
- `seed/cycle_sacrificial_signals.json` — sacrificial signals
- `seed/exit_conditions.json` — exit rule per setup
- `seed/cycle_health.json` — health metrics
- `seed/regime_model.json` — EV scoring model
- `seed/signal_regime_scores.json` — per-signal scores
- `seed/nightly_watchlist.json` — latest watchlist output

Plus JSON files:
- `seed/files/signal_filter/filtered_*.json` — vetting signal lists
- `seed/files/vetting/vetting_*.json` — vetting decisions
- `seed/files/setup_refiner/refined_*.json` — setup refiner outputs

Recovery (desktop explodes → new machine):
1. Clone repo from GitHub — 5 min
2. `pip install -r requirements.txt` — 2 min
3. `python scripts/seed_vault.py --restore` — 1 min
4. `python local_runner/cache_builder.py --5yr --force` — 30 min
5. `python local_runner/expr_cache_builder.py --build` — 2 hrs
6. `python local_runner/nightly.py --force` — 20 min
7. `ScanPerfect.bat` — done, exact same scan running

## What Doesn't Change

- All grinder scripts — unchanged, they read local caches
- Expression engine — unchanged
- Nightly refresh steps 1-8 — unchanged (step 9 = seed vault, new)
- Grind result mirroring to Railway — unchanged
- Claude CLI AI review — unchanged

## Risks

- **DB corruption**: Local SQLite is single-writer. No concurrent access issues since only one app process.
- **Disaster recovery**: Seed vault covers this. Expression cache rebuild is the longest step (~2 hrs).
- **PySide6 updates**: Qt releases are stable; breaking changes rare between minor versions.

## Claude Session Note

This migration touches server.py, nightly.py, pipeline_agent.py, cache_builder.py, and the UI files. Every phase involves disconnecting something from Railway and reconnecting it locally. Claude needs to read the full codebase at session start — server.py, nightly.py, pipeline_agent.py, cache_builder.py, the UI HTML files, and this doc — before proposing any changes. Without full context, it's too easy to break a dependency chain that isn't obvious from a single file.

For the Phase 6 UI rebuild: Claude must load all three HTML files (index.html ~56KB, vetting.html ~28KB, pipeline.html ~22KB) plus the server.py endpoints they call to understand every interaction, data flow, and visual layout before writing any PySide6 code.
