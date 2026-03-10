# TODO — Swing Screener (2026-03-10)

## V2 Pipeline — The Correct Order

See `PIPELINE_V2.md` for full spec.

**Nightly auto-refresh (4:30pm ET):** OHLCV → caches → expr cache → matrix → earnings → market cache (266 instruments) → dartboard universe stats. Fully automated (8 steps).

**The Vetting Loop (repeat until convergence):**
1. Signal Grind — examples vs universe → conditions (two engines: pyramid or dartboard)
2. Exit Grind — optimal exit condition from example signal bar close forward
3. Scan — apply conditions to 5yr → deduped signals + exit filter → classified signal set
4. Refinement Grind — blackout masking. Manual gate.
5. Vet — review winner pile. YES → AI review → approve → examples → loop.

**After Convergence (run once, in order):**
6. Proximity Grind — trim leftward/early signal bars from lose pile.
7. Profit Grind — trade exit from entry bar high forward.
8. Regime Model — winner/loser ratio vs 266 market instruments
9. Health Check — cycle quality, EV, promote/revert

---

## Current State — DTSS

### Dartboard Grinder — FIRST TEST RUN IN PROGRESS (2026-03-10)

Task #3 running on Dan's machine. Waiting for results.

**Background:** Signal count bloat investigation showed the pyramid grinder's beam search is fundamentally unstable at 69 examples (9.4% Jaccard between runs, random-walking not converging). Built dartboard grinder as replacement — density-based scoring using Gaussian kernel + Cohen's d weighting. See `DARTBOARD_DESIGN.md` and `scripts/diagnose_bloat.py`.

**Next steps after results come in:**
1. Evaluate dartboard output vs pyramid (signal count, example scores, overlap)
2. If good: re-run pipeline steps 2-9 with dartboard output
3. Tune top_n and threshold
4. Complete pipeline audit (profit grind → regime → health check)

### Pipeline Audit (2026-03-09) — paused for signal count investigation

Steps 1-6 verified with pyramid grinder. Steps 7-9 remain.

---

## Grind History (DTSS)

| Grind | Date | Examples | Engine | Signals | Notes |
|-------|------|----------|--------|---------|-------|
| 1 | 2026-02-28 | 20 | pyramid | 264 | First grind |
| 2 | 2026-03-03 | 35 | pyramid | 409 | |
| 3 | 2026-03-03 | 48 | pyramid | 489 | |
| 4 | 2026-03-04 | 59 | pyramid | 803 | Inflection point |
| 5 | 2026-03-10 | 68 | pyramid (D1 cap) | 1,218 | |
| 6 | 2026-03-10 | 69 | pyramid | 1,292 | Outlier removal made it worse |
| 7 | 2026-03-10 | 69 | dartboard | ? | FIRST TEST — running |

---

## Data

- Expression library: 15,805 expressions
- Expression cache: 4,119 tickers × 15,805 expr
- Market cache: 245/266 instruments × 15,805 expr
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 69 DTSS examples (BRK-B, SMMT, VUZI excluded from cache)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. Push all work to GitHub before ending chat (v2 branch)
3. Break work into small tasks
4. All OHLCV from Railway DB or local caches. Never yfinance in pipelines.
5. Read ta_knowledge.md before any TA work
6. NEVER dump large data into context
7. Expression cache = single computation path
8. V2 only — no V1 patching
9. Read PIPELINE_V2.md before touching any grinder or pipeline agent
10. Pyramid grinder: 100% example pass rate, non-negotiable
11. Dartboard grinder: low-scoring examples are outlier warnings, not hard gates
