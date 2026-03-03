# TODO — Swing Screener (2026-03-02)

## Current State — PIPELINE DATA FLOW FIXED, RE-GRIND NEEDED

Pipeline wiring between local machine and Railway is fixed. The immediate blocker is that 10/36 DTSS examples fail the current 41 conditions — examples added through vetting weren't part of the original grind. A full re-grind (Step 2) with all 36 examples is required before the filter (Step 3) will produce valid results.

### Pipeline Steps

| # | Step | Server ID | What Runs | Status |
|---|------|-----------|-----------|--------|
| 0 | Nightly Refresh | `nightly` | `python local_runner/nightly.py` | ✅ Works |
| 1 | Optimal Samples | `optimal_samples` | Read-only display (manual) | ✅ Works |
| 2 | Signal Brute Forcing | `signal_brute` | `pyramid_grinder.py` → `signal_exit_grinder.py` | ⚠️ NEEDS RE-RUN (36 examples, only 23 pass current conditions) |
| 3 | Sample Expansion | `sample_expansion` | `signal_filter.py` → chart vetting UI | ✅ Wired (uploads to Railway, live logs, prerequisite on Step 2) |
| 4 | MFE Capture | `mfe_capture` | `exit_grinder.py` | ✅ Script exists (trade mgmt exit optimizer) |
| 5 | Market Grinder | `market_grind` | Not built | ⬜ Placeholder |

### Pipeline Chain (enforced by prerequisites)

```
Step 2: Grind (finds conditions passing ALL examples)
  ↓ prerequisite
Step 3: Filter (scans universe with those conditions) → Upload to Railway → Vet
  ↓ vetting adds new examples
Step 2: Re-grind (with expanded example set)
  ... repeat
```

Steps cannot be run out of order. Adding examples through vetting invalidates the current grind conditions.

---

## COMPLETED — Pipeline Data Flow Fixes (2026-03-02)

### What was fixed:

1. **signal_filter.py → Railway upload**: After saving locally, POSTs filtered signals + exit grind to Railway via `/api/vetting/{setup}/upload-signals` and `/upload-exit`. Vetting UI can now read results. Graceful failure with manual fallback.

2. **Expression cache fingerprint mismatch**: pyramid_grinder was validating with `generate_all()` (signal-only) but cache was built with `_load_expressions()` (signal + generic exit). Fixed: uses `load_cache_expressions()` for fingerprint check. Better diagnostic error on mismatch.

3. **MFE Capture script path**: `scripts/exit_grinder.py` exists (924 lines). Agent path was correct. False alarm in original audit.

4. **Windows UTF-8 crash**: signal_filter.py and exit_grinder.py lacked `sys.stdout.reconfigure(encoding='utf-8')` for Windows cp1252. Unicode chars crashed the upload. Fixed.

5. **Sample Expansion UI**: Added RELOAD button (top header bar), live log overlay on chart area while running, auto-refresh signals on completion, prerequisite enforcement with clear messaging.

---

## IMMEDIATE — Re-Grind DTSS with 36 Examples

### Problem
Current grind used fewer examples. Vetting added 13 new examples (36 total). The 41 conditions from the old grind don't pass 10 of the new examples:

**Failing examples (conditions failed):**
- ACHR: 3/41 failed
- AIR: 1/41
- DELL: 2/41
- HLIT: 1/41
- HNRG: 1/41
- ISRG: 5/41
- MPC: 1/41
- PACB: 2/41
- PSIX: 6/41
- UTSL: 2/41

**Not in cache (expected):** BRK-B, SMMT, VUZI

### What needs to happen
1. Run Step 2 (Signal Brute Forcing) from UI or agent — pyramid_grinder.py with all 36 examples
2. Grinder must find conditions where 100% of examples pass (non-negotiable)
3. Then run Step 3 (Sample Expansion) to filter + upload + vet new signals

---

## Data Locations

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
      filtered_{setup}.json         — latest filter result (auto-uploads to Railway)

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

## Architecture

### Frontend: `app/index.html` (unified SPA)

**Setup Analysis steps:**
1. **Optimal Samples** — gallery with mini chart toggle, rejected signals list, stats
2. **Signal Brute Forcing** — runs pyramid_grinder.py then signal_exit_grinder.py, live log streaming
3. **Sample Expansion** — signal filter + chart vetting. RELOAD button in header, live log overlay, auto-upload to Railway, auto-refresh on completion. Prerequisite: Step 2 must complete first.
4. **MFE Capture** — single-stage vs multi-stage toggle, exit optimizer
5. **Market Grinder** — placeholder

### Backend: `server.py`

**Pipeline endpoints:**
- `GET /api/pipeline/steps` — step states + vetting stats + agent status
- `POST /api/pipeline/run/{step_id}` — queue job (checks prerequisites)
- `POST /api/pipeline/stop` — stop running job
- `POST /api/pipeline/reset/{step_id}` — reset step state

**Vetting endpoints:**
- `GET /api/vetting/{setup}/signals` — filtered signals for chart vetting
- `POST /api/vetting/{setup}/decide` — saves verdict, creates example or rejected_signal
- `POST /api/vetting/{setup}/upload-signals` — upload filtered JSON from desktop
- `POST /api/vetting/{setup}/upload-exit` — upload exit grind from desktop

### Agent: `local_runner/agent.py`

```python
PIPELINE_STEP_SCRIPTS = {
    "signal_brute":     [pyramid_grinder, signal_exit_grinder],
    "sample_expansion": [signal_filter],  # auto-uploads to Railway
    "mfe_capture":      [exit_grinder],   # trade management exit optimizer
}
```

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

- Expression cache: 4,119 tickers x 12,421 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 36 DTSS optimal samples (needs re-grind to find conditions passing all 36)
- 8 rejected signals in `rejected_signals` DB table

---

## Build Plan

### Immediate: Re-Grind DTSS
- [ ] Run pyramid_grinder.py with all 36 examples
- [ ] Verify 100% example pass rate
- [ ] Run signal_exit_grinder.py
- [ ] Run signal_filter.py -> verify upload to Railway
- [ ] Vet new signals in UI

### Phase: AI Vetting Review
- [ ] `scripts/ai_vet_review.py` -- claude -p reviews YES/NO decisions
- [ ] Wire into "Submit for Audit" button

### Phase: MFE Capture Backend
- [ ] Define scope (vs signal_exit_grinder in Step 2)
- [ ] Single-stage vs multi-stage from UI

### Phase: Market Grinder
- [ ] Step 5 implementation
- [ ] Blocked by: enough vetted examples with outcomes

### Phase: Daily Watchlist
- [ ] Nightly scan -> today's signals ranked by EV
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
