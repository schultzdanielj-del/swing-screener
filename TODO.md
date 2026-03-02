# TODO — Swing Screener (2026-03-02)

## Current State — BROKEN DATA FLOW

The UI steps were reorganized but the under-the-hood wiring is incomplete. Each component saves/reads from different locations, and there's no mechanism to pass data between local machine and Railway server between steps.

### Pipeline Steps (as renamed)

| # | Step | Server ID | What Runs | Status |
|---|------|-----------|-----------|--------|
| 0 | Nightly Refresh | `nightly` | `python local_runner/nightly.py` | ✅ Works |
| 1 | Optimal Samples | `optimal_samples` | Read-only display (manual) | ✅ Works (shows DB counts) |
| 2 | Signal Brute Forcing | `signal_brute` | `pyramid_grinder.py` → `signal_exit_grinder.py` | ⚠️ Runs but needs expr cache |
| 3 | Sample Expansion | `sample_expansion` | `signal_filter.py` → chart vetting UI | ❌ BROKEN — filter writes local, vetting reads Railway |
| 4 | MFE Capture | `mfe_capture` | `exit_grinder.py` | ❌ Script path wrong in agent |
| 5 | Market Grinder | `market_grind` | Not built | ⬜ Placeholder |

---

## DATA FLOW AUDIT — Where Things Break

### Step 2: Signal Brute Forcing
**Runs on:** Local machine (via agent)
**Scripts:** pyramid_grinder.py → signal_exit_grinder.py (back-to-back)

**pyramid_grinder.py WRITES:**
- `local_runner/cache/pyramid_results_{setup}.json` (latest)
- `local_runner/cache/pyramid_{setup}_mp_sig{N}_pk{N}_{timestamp}.json` (timestamped)
- `local_runner/cache/historical_results_{setup}.json` (compat format)

**signal_exit_grinder.py WRITES:**
- `data/signal_exit_grind/signal_exit_{setup}.json` (latest)
- `data/signal_exit_grind/signal_exit_{setup}_{N}ex_{tag}_{timestamp}.json` (timestamped)

**ISSUE:** Results stay on local machine. Nothing uploads to Railway. Step 3 needs these files but if running locally that's fine. But vetting UI on Railway needs the filtered results which come from Step 3.

### Step 3: Sample Expansion
**Runs on:** Local machine (via agent), then vetting in browser (reads Railway)
**Scripts:** signal_filter.py (local) → vetting UI (Railway)

**signal_filter.py READS:**
- `local_runner/cache/pyramid_results_{setup}.json` (from Step 2) ✅ local→local
- `data/signal_exit_grind/signal_exit_{setup}.json` (from Step 2) ✅ local→local
- Railway API `/api/examples/{setup}` for example list ✅ local→Railway
- `local_runner/cache/universe_ohlcv_5yr.pkl` for OHLCV ✅ local
- Expression cache for exit computation ✅ local

**signal_filter.py WRITES:**
- `data/signal_filter/filtered_{setup}.json` (LOCAL only)
- `data/signal_filter/filtered_{setup}_{N}sig_{timestamp}.json` (LOCAL timestamped)

**Vetting UI READS:**
- Railway: `data/signal_filter/filtered_{setup}.json` ← ❌ FILE IS ON LOCAL MACHINE, NOT RAILWAY
- Railway: `data/vetting/vetting_{setup}.json` (decisions)

**Upload endpoints EXIST but are NEVER CALLED:**
- `POST /api/vetting/{setup}/upload-signals` — accepts filtered JSON
- `POST /api/vetting/{setup}/upload-exit` — accepts exit grind JSON

**FIX NEEDED:** signal_filter.py must upload results to Railway after saving locally. OR the agent must upload after signal_filter completes.

### Step 4: MFE Capture
**Runs on:** Local machine (via agent)
**Agent mapping:** `"mfe_capture": [["python", "scripts/exit_grinder.py", "--setup", "{setup}"]]`

**ISSUE:** `scripts/exit_grinder.py` doesn't exist. The actual scripts are:
- `scripts/signal_exit_grinder.py` — cache-compatible signal exit (already runs in Step 2)
- A separate trade management exit optimizer may need to be built

**FIX NEEDED:** Determine what MFE Capture actually runs. If it's the same signal_exit_grinder, it's redundant with Step 2. If it's a separate trade management exit optimizer, that script needs to be built.

---

## COMPLETE FIX LIST

### P0 — Critical (pipeline can't function)

1. **signal_filter.py must upload to Railway after saving locally**
   - After `save_results()`, POST filtered JSON to `/api/vetting/{setup}/upload-signals`
   - This bridges local→Railway gap so vetting UI can read signals

2. **Fix MFE Capture agent script path**
   - `scripts/exit_grinder.py` doesn't exist
   - Either point to correct script or mark as "not built yet"

3. **Expression cache detection on first run**
   - pyramid_grinder crashes with RuntimeError if cache not found
   - The cache IS built but the error message is confusing — need better detection/messaging

### P1 — Important (UX/reliability)

4. **Agent should upload grind results to Railway after Step 2 completes**
   - Upload pyramid_results and signal_exit to Railway endpoints
   - Enables Railway-hosted pipeline status to show actual results

5. **"Reload Samples" button in Step 3 needs to:**
   - Queue `sample_expansion` step via agent (runs signal_filter.py)
   - signal_filter.py uploads results to Railway
   - Vetting UI refreshes with new signals

6. **Vetting decisions need to sync back**
   - Currently saved to Railway `data/vetting/vetting_{setup}.json`
   - When user marks YES, example is created in Railway DB ✅ (this works)
   - When user marks NO, rejected_signal is created in Railway DB ✅ (this works)

### P2 — Quality of life

7. **Remove terminal command display from Step 2 info panel** (index.html StepPage still shows it)
8. **Pipeline steps should be runnable in any order** (currently prerequisites block some)
9. **Step status should reflect actual data availability** not just "did the step run"

---

## Data Locations Summary

```
LOCAL MACHINE (Dan's PC):
  local_runner/cache/
    universe_ohlcv_5yr.pkl          — 5yr OHLCV cache
    universe_ohlcv.pkl              — daily OHLCV cache
    expr_series/                    — expression cache (~50 GB)
    pyramid_results_{setup}.json    — latest pyramid grind result
    historical_results_{setup}.json — compat format
    brute_expressions.json          — expression library
    classification.json             — ETF classifier
  data/
    signal_exit_grind/
      signal_exit_{setup}.json      — latest exit grind result
    signal_filter/
      filtered_{setup}.json         — latest filter result (NEEDS UPLOAD TO RAILWAY)

RAILWAY SERVER:
  data/
    pipeline_state.json             — step states, job queue
    pipeline_logs.json              — step log history
    signal_filter/
      filtered_{setup}.json         — filtered signals (READ BY VETTING UI)
    signal_exit_grind/
      signal_exit_{setup}.json      — exit grind (for vetting display)
    vetting/
      vetting_{setup}.json          — vetting decisions
  SQLite DB:
    examples                        — validated optimal samples
    rejected_signals                — rejected signals from vetting
    universe_ohlcv                  — 11M+ OHLCV rows
    tradable_universe               — ~4,167 tickers
```

---

## Architecture (as built)

### Frontend: `app/index.html` (unified SPA)

**Rail navigation:** Nightly Refresh | Setup Analysis | Daily Watchlist

**Setup Analysis steps:**
1. **Optimal Samples** — gallery with mini chart toggle, rejected signals list, stats
2. **Signal Brute Forcing** — runs pyramid_grinder.py then signal_exit_grinder.py back-to-back
3. **Sample Expansion** — signal filter + chart vetting merged. Full candlestick charts with EMAs/SMAs, earnings overlay, YES/NO/MAYBE verdicts, "Reload Samples" button
4. **MFE Capture** — single-stage vs multi-stage toggle, finds best exit for max MFE capture
5. **Market Grinder** — placeholder (clusters outcomes vs market regime)

**Key features:**
- Agent status dot (green=online, red=offline) with click-to-copy start command when offline
- Heartbeat: 10s interval, 20s timeout, 5s UI poll
- Live log streaming for all grinder steps
- Setup selector (DTSS / 3-4DB)
- Keyboard shortcuts in vetting (↑↓ nav, 1/2/3 for yes/maybe/no)

### Backend: `server.py`

**Pipeline endpoints:**
- `GET /api/pipeline/steps` — step states + vetting stats + agent status
- `POST /api/pipeline/run/{step_id}` — queue job for agent
- `POST /api/pipeline/stop` — stop running job
- `POST /api/pipeline/reset/{step_id}` — reset step state
- Stale job cleanup: removes jobs with IDs not in current PIPELINE_STEPS

**Vetting endpoints:**
- `GET /api/vetting/{setup}/signals` — filtered signals for chart vetting
- `GET /api/vetting/{setup}/ohlcv/{ticker}` — OHLCV centered on signal date
- `POST /api/vetting/{setup}/decide` — saves verdict, creates example or rejected_signal
- `POST /api/vetting/{setup}/upload-signals` — upload filtered JSON from desktop
- `POST /api/vetting/{setup}/upload-exit` — upload exit grind from desktop
- `GET /api/vetting/earnings/{ticker}` — Yahoo Finance earnings dates
- `GET /api/vetting/{setup}/rejected` — all rejected signals

### Agent: `local_runner/agent.py`

**Pipeline step scripts:**
```python
PIPELINE_STEP_SCRIPTS = {
    "signal_brute":     [pyramid_grinder, signal_exit_grinder],
    "sample_expansion": [signal_filter],
    "mfe_capture":      [exit_grinder],  # ← BROKEN: script doesn't exist
}
```

- Multi-command step support: runs scripts sequentially, fails on first error
- Streams logs to Railway in batches of 20 lines
- Heartbeat every 10s
- UTF-8 subprocess output on Windows

---

## Grinder Rules (NON-NEGOTIABLE)

1. All grinders must use the **exact same computation methods** as each other
2. All grinders must be **optimized for maximum speed** (no bottlenecks)
3. All grinders must use **as many CPU cores as possible**
4. All conditional results must **pass 100% of setup examples** — no exceptions
5. Any violation of these rules **invalidates results**
6. Expression cache is the **single computation path** — no fallback to live compute

---

## Data

- Expression cache: 4,119 tickers × 12,175 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tradable tickers
- 36 DTSS optimal samples (23 original + 14 from vetting pass 1, minus 1 removed)
- 8 rejected signals in `rejected_signals` DB table

---

## Build Plan — What's Left

### Immediate: Fix Pipeline Data Flow ⬜
- [ ] signal_filter.py: add upload to Railway after local save
- [ ] Fix MFE Capture script path in agent (or mark not built)
- [ ] Verify Step 2 → Step 3 data handoff works end-to-end
- [ ] Verify "Reload Samples" triggers filter + upload + UI refresh
- [ ] Test full pipeline: grind → filter → upload → vet → example creation

### Phase: AI Vetting Review ⬜
- [ ] `scripts/ai_vet_review.py` — claude -p reviews YES/NO decisions
- [ ] Wire into "Submit for Audit" button

### Phase: MFE Capture Backend ⬜
- [ ] Define what this step actually does (vs signal_exit_grinder in Step 2)
- [ ] Build or wire correct script
- [ ] Single-stage vs multi-stage from UI

### Phase: Market Grinder ⬜
- [ ] Step 5 implementation
- [ ] Blocked by: enough vetted examples with outcomes

### Phase: Daily Watchlist ⬜
- [ ] Nightly scan → today's signals ranked by EV
- [ ] Blocked by: market grinder

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% of examples must pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
