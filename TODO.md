# TODO — Swing Screener (2026-03-07)

## V2 Pipeline — The Correct Order

See `PIPELINE_V2.md` for full spec.

**Nightly auto-refresh (4:30pm ET):** OHLCV → caches → expr cache → matrix → earnings → market cache (266 instruments). Fully automated.

**The Vetting Loop (repeat until convergence):**
1. Signal Grind — examples vs universe → conditions
2. Exit Grind — optimal exit condition from example entry bar highs
3. Scan — apply conditions to 5yr → deduped signals
4. Exit Filter — apply exit condition → winners (exit triggered) vs losers (no exit)
5. Refinement Grind — (examples + exit-triggered) vs no-exit, blackout. Manual gate.
6. Vet — review winner pile (source toggle: step 4 or step 5). YES → AI review → approve → examples → loop.

**After Convergence (run once):**
7. Regime Model — winner/loser ratio vs 266 market instruments
8. Health Check — cycle quality, EV, promote/revert

**Convergence:** Full vetting pass produces no new examples.

**Refinement gate:** Discretionary — skip step 5 in early cycles with few examples. Enable once example library is large enough for stable refinement conditions. Threshold TBD after 2-3 setup types built.

---

## Current State — DTSS

**72 examples. Converged.** Last vetting pass produced near-zero new examples.

**Proven math:**
- 41% win rate, EV 1.479 (from cycle_health on 68/68 examples)
- Winners: median 5.8 ADR, floor 3.2 ADR
- Losers: under 1 ADR
- Market regime model: win rate lift from 8% (worst decile) to 75% (best decile)
- 1111 signals, 456 wins, 50 independent regime features

**What's done:**
- Grind #5: 71 examples, 94 conditions, 281 filtered signals
- Exit condition: `slope_xavgc21_off7_adr14 <= -1.128826` (median 5.8 ADR capture)
- Market grinder complete: 245 instruments, 50 features, regime scores uploaded
- V2 server deployed on Railway (v2 branch)
- V2 UI: 4 tabs (Pipeline, Examples, Vetting, Watchlist), 8-step sidebar

**What's next for DTSS:**
1. Refinement grind — (examples + exit-triggered) vs no-exit, blackout → cut losers
2. Re-run regime model on refined signal set
3. Health check → promote → live
4. Nightly scan + watchlist

---

## V2 Build Status

### ✅ Done
- V2 server.py on v2 branch — all endpoints
- V2 UI: Pipeline (8 steps), Examples (pending queue + validated), Vetting (source toggle step 4/5, keyboard shortcuts), Watchlist (placeholder)
- Pipeline agent wiring — run/stop/logs from UI
- AI vetting flow: YES → pending → review_samples.py (Claude CLI) → approve/reject in Examples tab
- /api/chart/{setup}/{ticker}/{date} — chart PNG for AI review (universe_ohlcv first, yfinance fallback)
- Nightly refresh includes market cache append (266 instruments)
- Market grinder complete + uploaded
- Cycle health + versioning system
- DB tables: cycles, cycle_signals, cycle_conditions, health_metrics, regime models

### 🔲 Not yet wired (scripts exist, need agent mapping)
- `pyramid_grinder.py` → step signal_grind
- `exit_grinder_single.py` → step exit_grind
- `signal_filter.py` (scan phase) → step scan
- `signal_filter.py` (exit phase) → step exit_filter
- `setup_refiner.py` (blackout + refine) → step refinement_grind
- `market_grinder.py` → step regime
- `cycle_health.py` → step health

### 🔲 Not yet built
- Watchlist page (needs nightly scan output)
- Nightly live scan (apply current conditions to today's bars)

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Notes |
|-------|------|----------|------------|---------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | First grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | After first vet |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | After second vet |
| 4 | 2026-03-03 | 62 | 86 | 168 | After AI vetting |
| 4b | 2026-03-05 | 62 | 87 | 164 | Blackout re-grind |
| 5 | 2026-03-06 | 71 | 94 | 281 | Current |

---

## Data

- Expression library: 15,805 expressions
- Expression cache: 4,119 tickers × 15,805 expr (~50 GB) — **stale, needs rebuild**
- Market cache: 245/266 instruments × 15,805 expr (3.96 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 72 DTSS examples (69 in cache, 3 excluded: BRK-B, SMMT, VUZI)

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
