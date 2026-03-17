# Localize — Migration Complete

**Last updated:** 2026-03-17

Everything runs locally on Dan's desktop. Railway is seed vault only.

---

## Architecture

**Local (desktop):**
- Native PySide6 desktop app (`scanperfect.py`) — no browser, no server process
- SQLite DB (`data/scanperfect.db`): examples, setups, vetting decisions, earnings, pending reviews
- All caches: 5yr OHLCV pickle (in-memory), expression cache, market cache, fundamentals
- Grind results: local JSON files in `local_runner/cache/`
- Pipeline runs as subprocesses launched directly from the PySide6 app via QProcess
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
- Pipeline subprocesses launched directly via QProcess (PySide6) or threading (server.py)
- `pipeline_agent.py` is LEGACY — no longer used

**Phase 3 — Wire Local OHLCV** ✅
- 5yr pickle loaded into memory at startup (~4,169 tickers in 0.5s)
- All chart data served from in-memory cache

**Phase 4 — Seed Vault** ✅
- `scripts/seed_vault.py`: backs up all SQLite tables + vetting/grind JSON files to Railway
- `scripts/seed_vault.py --restore`: full disaster recovery
- Wired into `nightly.py` as step 9

**Phase 5 — Slim Railway** (low priority)
- Strip Railway server.py to just file mirror endpoints
- Not urgent — Railway works fine as-is

**Phase 6 — Native Desktop UI (PySide6)** ✅ IN PROGRESS
- Replaces browser-based HTML UI entirely
- Single file: `scanperfect.py`
- Pipeline flowchart with 7 nodes, two feedback loops, unlock progression
- Color-coded cards: red (Examples), orange (Vetting), yellow (Scan Tuning), blue (grinds), green (Summary)
- Cards expand in place with animated grow to show detail panels (Run/Stop/Log)
- No tabs — the flowchart IS the interface, each node expands to become its workspace
- Reads directly from SQLite + local cache files — no server, no HTTP, no browser

---

## Seed Vault (Daily Backup)

Runs as step 9 of nightly refresh (4:30pm ET).

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
