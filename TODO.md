# TODO — Swing Screener (2026-03-09)

## V2 Pipeline — The Correct Order

See `PIPELINE_V2.md` for full spec.

**Nightly auto-refresh (4:30pm ET):** OHLCV → caches → expr cache → matrix → earnings → market cache (266 instruments). Fully automated.

**The Vetting Loop (repeat until convergence):**
1. Signal Grind — examples vs universe → conditions
2. Exit Grind — optimal exit condition from example signal bar close forward
3. Scan — apply conditions to 5yr → deduped signals + exit filter → classified signal set (uploads to v2 cycle_signals)
4. Refinement Grind — blackout pyramid_grinder.py then setup_refiner.py. Manual gate.
5. Vet — review winner pile. YES → AI review → approve → examples → loop.

**After Convergence (run once, in order):**
6. Proximity Grind — trim leftward/early signal bars from lose pile. Only safe post-convergence.
7. Trade Exit Grind — exit_grinder.py from entry bar high (real trade management exit). Runs on final signal set.
8. Regime Model — winner/loser ratio vs 266 market instruments (runs on proximity-trimmed set)
9. Health Check — cycle quality, EV, promote/revert

**Convergence:** Full vetting pass produces no new examples.

**Refinement gate:** Discretionary — skip in early cycles with few examples. Enable once example library is large enough for stable refinement conditions.

---

## Current State — DTSS

### Pipeline Audit In Progress (2026-03-09)

Running every pipeline step from scratch, verifying data integrity at each handoff.

**Completed steps:**

| Step | Status | Key Output | Railway Verified |
|------|--------|------------|-----------------|
| 1. Signal Grind | ✅ | 89 conditions, 68 examples pass | Cycle `dtss_signal_grind_20260309_192357`, 89 conditions, is_current=1 |
| 2. Exit Grind | ✅ | `slope_xavgc21_off7_adr14 <= -1.128826` (same as prev — stable) | Exit condition uploaded + verified |
| 3. Scan | ✅ | 1,031 deduped signals (436 WIN / 595 LOSS, 42.3% WR) | 1,031 signals in v2 cycle_signals, verified |
| 4. Refinement Grind | 🔲 NEXT | — | — |
| 5. Proximity Grind | 🔲 | — | — |
| 6. Regime Model | 🔲 | — | — |
| 7. Health Check | 🔲 | — | — |

**Fixes made during audit:**
- `signal_exit_grinder.py`: Added Railway upload (`POST /api/v2/exit_conditions`) with direction mapping and verification
- `signal_filter.py`: Added v2 cycle_signals upload (full classified set with all 1,031 signals including no-exit losers)
- `signal_filter.py`: Removed all v1 vetting endpoint uploads (`/api/vetting/upload-signals`, `/api/vetting/upload-exit`)

**Key numbers this cycle:**
- 71 examples in Railway (68 usable — BRK-B, SMMT, VUZI excluded from cache)
- 89 conditions (D1:24, 1wk:11, 1mo:22, 6mo:13, 1yr:8, 5yr:11)
- Exit: `slope_xavgc21_off7_adr14 <= -1.128826`, median capture eff 0.64, floor 1.9 ADR, median 5.8 ADR
- Scan: 1,395 raw → 1,031 deduped → 844 exit triggered → 436 AUTO_WIN / 595 AUTO_LOSS
- Median ADR threshold for winner classification: 4.2

**Observations:**
- 82 signals matched examples (68 examples × some with multiple signal bars in ±5 bar proximity)
- 187 signals had no exit trigger within 120 bars → AUTO_LOSS
- v1 vetting endpoints still exist in server.py but signal_filter no longer writes to them

### Previous cycle for comparison

Cycle `dtss_20260306_170830`: 94 conditions, 68 examples at grind, 1,111 signals (456 win / 655 loss). Now is_current=0.

---

## V2 Build Status

### ✅ Done
- V2 server.py on v2 branch — all endpoints
- V2 UI: Pipeline (8 steps), Examples, Vetting, Watchlist (placeholder)
- Pipeline agent wiring — run/stop/logs from UI
- AI vetting flow: YES → pending → review_samples.py → approve/reject
- Nightly refresh includes market cache append (266 instruments)
- Cycle versioning + health system
- DB tables: cycles, cycle_signals, cycle_conditions, exit_conditions, health_metrics, regime models
- Grind storage V2 with transactional upload (GRIND_STORAGE.md)
- signal_exit_grinder.py uploads to Railway
- signal_filter.py uploads full classified signal set to v2 cycle_signals

### 🔲 Not yet built / needs work
- **Pipeline audit steps 4-7** — refinement, proximity, regime, health
- **setup_refiner.py** — still uploads to v1 vetting endpoint, needs v2 cycle upload (same fix as signal_filter)
- **Trade exit grind step** — exit_grinder.py (from entry bar high) needs to be wired into pipeline between proximity and regime
- **v1 endpoint cleanup** — old vetting endpoints in server.py can be removed once UI is updated
- Setup Dashboard, Regime Visual, Watchlist page, Nightly live scan

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Notes |
|-------|------|----------|------------|---------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | First grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | After first vet |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | After second vet |
| 4 | 2026-03-03 | 62 | 86 | 168 | After AI vetting |
| 4b | 2026-03-05 | 62 | 87 | 164 | Blackout re-grind |
| 5 | 2026-03-06 | 71 | 94 | 281 | Old cycle |
| 6 | 2026-03-09 | 68 | 89 | 1,031 deduped (436 W / 595 L) | Pipeline audit — fresh from scratch |

---

## Data

- Expression library: 15,805 expressions
- Expression cache: 4,119 tickers × 16,051 expr (64.6 GB)
- Market cache: 245/266 instruments × 15,805 expr (3.96 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 71 DTSS examples (68 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat (v2 branch)
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches. Never yfinance in pipelines.
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
9. V2 only — no V1 patching
10. Read PIPELINE_V2.md before touching any grinder or pipeline_agent.py. No exceptions.
