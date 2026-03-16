# Localize — Move Everything Local

## Why

Railway adds network latency to every operation. Vetting charts take 50-200ms per signal over the network. Local SQLite reads take microseconds. The pipeline agent polling pattern is unnecessary complexity when the server and agent are on the same machine.

## Architecture After Migration

**Local (your desktop):**
- FastAPI server (server.py) on localhost:8000
- SQLite DB: examples, setups, vetting decisions, earnings, pending reviews
- All caches: 5yr OHLCV, expression cache, market cache, fundamentals
- Pipeline runs directly (no agent polling — server triggers subprocesses)
- UI served from localhost
- AI review via Claude CLI (Max plan, no API cost)
- Start with: `python server.py` (or Windows shortcut/service)

**Railway (seed vault only):**
- File mirror API: receives daily backups + grind result JSONs
- No UI, no server logic, no pipeline coordination
- Endpoint: `POST /api/v2/files` (already exists)
- Claude reads grind results from here during chat sessions

## What Moves Local

| Data | Currently | After | Notes |
|------|-----------|-------|-------|
| Examples | Railway SQLite | Local SQLite | 65 rows, tiny |
| Setups | Railway SQLite | Local SQLite | 3 rows |
| Vetting decisions | Railway JSON files | Local SQLite or JSON | Per-setup files |
| Pending AI reviews | Railway SQLite | Local SQLite | |
| Earnings dates | Railway SQLite | Local cache (nightly refresh) | Part of fundamentals fetch |
| Pipeline state | Railway JSON | Eliminated — direct subprocess calls | |
| Pipeline agent | Polling Railway | Eliminated — server runs steps directly | |
| OHLCV (universe) | Railway SQLite (11M rows) | Already local (5yr cache) | |
| Expression cache | Local only | No change | |
| Market cache | Local only | No change | |
| Grind results | Local + mirrored to Railway | No change | |

## What Stays on Railway

- File mirror (`/api/v2/files`) — receives:
  - Grind result JSONs (refinement, EV, etc.) — for Claude chat access
  - Daily seed vault backup (see below)
- That's it. Railway server shrinks to ~50 lines.

## Seed Vault (Daily Backup)

Pushed to Railway as step 8 of the nightly refresh (4:30pm ET).

Contents:
- `seed/examples.json` — all examples across all setups
- `seed/setups.json` — setup definitions (name, description, direction)
- `seed/earnings.json` — earnings dates cache
- `seed/vetting.json` — all vetting decisions
- `seed/pending.json` — pending AI reviews

Recovery process:
1. Pull seed vault from Railway: `GET /api/v2/files/seed/*`
2. Import into local SQLite
3. Run nightly pipeline to rebuild all caches:
   - OHLCV: re-fetch from Yahoo (~30 min for full universe)
   - Expression cache: rebuild (~2 hrs)
   - Market cache: rebuild (~10 min)
   - Grind results: re-run grinders or pull from Railway file mirror
4. Everything operational

## Migration Steps

### Phase 1 — Local Server
1. Copy server.py to run locally (it already works as a standalone FastAPI app)
2. Point it at a local SQLite DB path instead of Railway volume
3. Create the DB tables (init_db already handles this)
4. Import the 65 examples from Railway (one API call + bulk insert)
5. Import setup definitions
6. Test: `python -m uvicorn server:app --port 8000` → open localhost:8000

### Phase 2 — Eliminate Agent Polling
1. Replace pipeline agent's Railway polling with direct subprocess calls
2. Server.py `run_step` endpoint calls subprocess directly instead of queuing for agent
3. Remove: heartbeat, job queue, agent status, polling loop
4. Pipeline runs are synchronous (or async with local subprocess)

### Phase 3 — Wire Local OHLCV
1. Server reads OHLCV from local 5yr cache instead of universe_ohlcv table
2. Or: populate local SQLite universe_ohlcv from the 5yr cache on startup
3. Vetting charts now load from local disk — microsecond reads

### Phase 4 — Seed Vault
1. Add step 8 to nightly_refresh.bat: push seed vault to Railway
2. Script: dump examples, setups, earnings, vetting to JSON, upload via file mirror API
3. Add recovery script: pull seed vault, import, trigger cache rebuild

### Phase 5 — Slim Railway
1. Strip Railway server.py down to just the file mirror endpoints
2. Remove all pipeline, vetting, examples, agent endpoints
3. Keep: POST/GET/DELETE /api/v2/files, health check
4. Redeploy

## What Doesn't Change

- UI (index.html) — identical, just served from localhost
- All grinder scripts — unchanged, they read local caches
- Expression engine — unchanged
- Nightly refresh steps 1-7 — unchanged
- Grind result mirroring to Railway — unchanged
- Claude CLI AI review — unchanged

## Risks

- **Port conflicts**: localhost:8000 might conflict with other services. Configurable port.
- **Firewall**: Windows might prompt for firewall access on first run. One-time allow.
- **Startup**: Need to start the server before using the UI. Windows Task Scheduler or startup script.
- **DB corruption**: Local SQLite is single-writer. No concurrent access issues since only one server process.
- **Disaster recovery**: Seed vault covers this. Expression cache rebuild is the longest step (~2 hrs).
