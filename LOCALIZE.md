# Localize — Migration Complete

**Last updated:** 2026-03-18

Everything runs locally on Dan's desktop. Railway is seed vault only.

---

## Architecture

**Local (desktop):**
- Native PySide6 desktop app (`scanperfect.py`) — no browser, no server process
- SQLite DB (`data/scanperfect.db`): examples, setups, vetting decisions, earnings, pending reviews, rejected signals
- 5yr OHLCV pickle loaded into memory at startup (~4,169 tickers in 0.5s) — all chart data instant
- All caches: expression cache, market cache, fundamentals — local files
- Grind results: local JSON files in `local_runner/cache/` and `data/`
- Pipeline runs as subprocesses launched from PySide6 app via QProcess with auto-chaining
- Start with: `python scanperfect.py` or double-click `ScanPerfect.bat`

**Railway (seed vault only):**
- File mirror API: receives daily backups + grind result JSONs
- No UI, no server logic, no pipeline coordination
- Claude reads grind results from here during chat sessions
- Endpoint: `POST /api/v2/files` (file mirror)

---

## Migration Status — All Complete

**Phase 1 — Local Server** ✅
- `IS_RAILWAY` flag in server.py for Railway deploy vs local mode
- `init_db()` creates all tables locally, seeds 3 setups

**Phase 2 — Eliminate Agent Polling** ✅
- Pipeline subprocesses launched directly via QProcess (PySide6)
- Auto-chaining: sub-steps run sequentially, next starts on success
- `pipeline_agent.py` is LEGACY — no longer used

**Phase 3 — Wire Local OHLCV** ✅
- 5yr pickle loaded into memory at startup (~4,169 tickers in 0.5s)
- All chart data served from in-memory cache — instant browsing

**Phase 4 — Seed Vault** ✅
- `scripts/seed_vault.py`: backs up all SQLite tables + vetting/grind JSON files to Railway
- `scripts/seed_vault.py --restore`: full disaster recovery
- Wired into `nightly.py` as step 9

**Phase 5 — Slim Railway** (low priority)
- Strip Railway server.py to just file mirror endpoints
- Not urgent — Railway works fine as-is

**Phase 6 — Native Desktop UI (PySide6)** ✅ BUILT
- Replaces browser-based HTML UI entirely
- Single file: `scanperfect.py` (~3,600 lines)
- Pipeline flowchart with 7 nodes, two feedback loops, unlock progression
- **Examples workspace:** chart card grid, pending review, add examples, setup description
- **Vetting workspace:** signal list, full candlestick chart, verdict buttons, causative/correlative toggle, ADR/entry/combined sort
- **Grinder panels:** sub-step badges, run/stop/clear, real-time log streaming
- All data from local SQLite + JSON files + in-memory OHLCV pickle

**Localized scripts:**
- `entry_candle_scorer.py` — reads examples from local SQLite (was Railway API)
- `signal_filter.py` — still has Railway refs for some features, core functionality works locally

---

## Seed Vault (Daily Backup)

Runs as step 8 of nightly refresh (4:30pm ET).

Backs up to Railway:
- All SQLite tables (examples, setups, earnings, pending, rejected, etc.)
- Vetting decision files
- Grind result JSONs

Recovery (new machine):
1. Clone repo from GitHub
2. `pip install -r requirements.txt`
3. `python scripts/seed_vault.py --restore`
4. `python local_runner/cache_builder.py --5yr --force` (~30 min)
5. `python local_runner/expr_cache_builder.py --build` (~2 hrs)
6. `python local_runner/nightly.py --force` (~20 min)
7. `python scanperfect.py` — done

---

## What Doesn't Change

- All grinder scripts — unchanged, they read local caches
- Expression engine — unchanged
- Nightly refresh steps 1-8 — unchanged (step 9 = seed vault)
- Grind result mirroring to Railway — unchanged (backup)
