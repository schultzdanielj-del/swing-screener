# TODO — Swing Screener (2026-03-03)

## Current State — RE-GRIND RUNNING OVERNIGHT

Pyramid grinder running overnight with 33 DTSS examples (36 minus BRK-B, SMMT, VUZI not in cache). First run after UI pipeline fixes. Matrix rebuilt from live computation (~45 min) due to fingerprint mismatch bug — now fixed, next run will use cache (~50s).

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | `nightly` | ✅ Works |
| 1 | Optimal Samples | `optimal_samples` | ✅ Works |
| 2 | Signal Brute Forcing | `signal_brute` | ⏳ Running overnight (re-grind with 33 examples) |
| 3 | Sample Expansion | `sample_expansion` | ✅ Wired (prerequisite on Step 2) |
| 4 | MFE Capture | `mfe_capture` | ✅ Script exists |
| 5 | Market Grinder | `market_grind` | ⬜ Placeholder |

### Pipeline Chain (enforced)

```
Step 2: Grind (finds conditions passing ALL examples)
  ↓ prerequisite
Step 3: Filter → Upload to Railway → Vet
  ↓ vetting adds new examples
Step 2: Re-grind with expanded set
```

---

## BUGS FIXED — 2026-03-02/03 Session

### Pipeline data flow (previous session)
1. **signal_filter.py → Railway upload** — POSTs filtered signals + exit grind to Railway after local save
2. **Expression cache fingerprint mismatch in pyramid_grinder** — removed broken `load_cache_expressions` import, uses simple `is_valid()` 
3. **Windows UTF-8 crash** — added `sys.stdout.reconfigure(encoding='utf-8')` to signal_filter.py, exit_grinder.py

### UI/Agent fixes (this session)
4. **Duplicate log lines** — stale closure bug. `logs.length` inside `setInterval` closure always read initial value (0). Every poll sent `?after=0` and got ALL lines back. Fixed with `useRef` in all 4 pages (NightlyPage, StepPage, VettingPage, ExitManagePage).
5. **Zombie jobs blocking runs** — old jobs stuck with status "running" or "claimed" blocked new runs with "Already running" error. No way to unjam from UI. Fixed: run endpoint auto-cleans dead jobs (agent heartbeat > 30s = dead, nuke all jobs). Re-running same step replaces old job. Reset also removes associated jobs.
6. **STOP button non-functional** — server set `stop_requested` but agent never checked. Fixed: agent polls `GET /api/pipeline/stop-check/{step_id}` every 10s during subprocess, terminates process on stop. New server endpoint added.
7. **Agent shows OFFLINE during runs** — heartbeat was in main loop which blocked during subprocess. Fixed: agent sends heartbeat every 10s from inside the subprocess read loop.
8. **Grinder output not streaming** — Python buffers stdout when run as subprocess. Fixed: `PYTHONUNBUFFERED=1` env var on subprocess.
9. **Matrix builder 45-min rebuild every run** — ROOT CAUSE: matrix builder called `is_valid(expressions)` with signal-only list (12,175) but cache was built with signal+exit (12,421). Fingerprint mismatch → fell back to live computation every time. Fixed: `is_valid()` with no args + column mapping so cache subset works. Next run should load in ~50s.

### Still unverified (need live test after overnight grind)
- [ ] Log streaming shows grinder output without duplicates
- [ ] STOP button actually kills subprocess
- [ ] Agent shows ONLINE during runs
- [ ] Matrix loads from cache in ~50s (fingerprint fix)
- [ ] Full chain: grind → filter → upload → vetting UI shows signals

---

## IMMEDIATE — After Overnight Grind

1. Check terminal / `local_runner/cache/pyramid_results_dtss.json` for results
2. Verify 100% example pass rate (non-negotiable)
3. Pull latest code (matrix builder fix)
4. Run Step 3 from UI — signal_filter.py → upload → verify vetting UI has signals
5. Verify matrix loads from cache on next Step 2 run (~50s not 45 min)

---

## Data Locations

```
LOCAL MACHINE:
  local_runner/cache/
    universe_ohlcv_5yr.pkl          — 5yr OHLCV cache
    universe_ohlcv.pkl              — daily OHLCV cache  
    expr_series/                    — expression cache (~50 GB)
    universe_matrix.pkl             — D1 point-value matrix (rebuilt daily)
    pyramid_results_{setup}.json    — latest pyramid grind result
    brute_expressions.json          — expression library
  data/
    signal_exit_grind/
      signal_exit_{setup}.json      — latest exit grind result
    signal_filter/
      filtered_{setup}.json         — latest filter result (auto-uploads to Railway)

RAILWAY SERVER:
  data/
    pipeline_state.json             — step states, job queue
    pipeline_logs.json              — step log history
    signal_filter/filtered_{setup}.json — filtered signals (vetting UI reads)
    signal_exit_grind/signal_exit_{setup}.json — exit grind (vetting display)
    vetting/vetting_{setup}.json    — vetting decisions
  SQLite DB:
    examples, rejected_signals, universe_ohlcv, tradable_universe
```

---

## Architecture

### Agent: `local_runner/agent.py`
- Subprocess runs with `PYTHONUNBUFFERED=1` for real-time output
- Heartbeat every 10s during subprocess (prevents OFFLINE status)
- Polls for stop requests every 10s, terminates process on stop
- Log buffer: posts to Railway every 20 lines

### Server: `server.py`
- Run endpoint auto-cleans dead jobs before checking conflicts
- Agent dead (no heartbeat 30s) → all jobs cleared
- Same step re-run → old job replaced
- Stop-check endpoint: `GET /api/pipeline/stop-check/{step_id}`

### Frontend: `app/index.html`
- Log polling uses `useRef` for after count (not stale closure)
- Reset clears logs + counter

---

## Grinder Rules (NON-NEGOTIABLE)

1. All grinders must use **identical computation methods**
2. All grinders must be **optimized for maximum speed**
3. All grinders must use **full CPU cores**
4. All results must **pass 100% of setup examples** — no exceptions
5. Expression cache is the **single computation path**

---

## Data

- Expression cache: 4,119 tickers × 12,421 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 36 DTSS examples (33 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Build Plan

### Immediate: Verify re-grind + full chain
- [ ] Grind results with 100% pass rate
- [ ] Matrix cache loads in ~50s
- [ ] Filter → upload → vetting UI works

### Phase: AI Vetting Review
- [ ] claude -p reviews YES/NO decisions
- [ ] Wire into "Submit for Audit" button

### Phase: MFE Capture
- [ ] Define scope vs signal_exit_grinder
- [ ] Single-stage vs multi-stage from UI

### Phase: Market Grinder
- [ ] Step 5 implementation

### Phase: Daily Watchlist
- [ ] Nightly scan → today's signals ranked by EV

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
