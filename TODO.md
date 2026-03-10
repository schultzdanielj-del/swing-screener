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

### Dartboard Grinder — TESTED, NEEDS HYBRID APPROACH (2026-03-10)

**Background:** Pyramid beam search unstable at 69 examples (9.4% Jaccard). Built dartboard grinder — density scoring via Gaussian kernel + Cohen's d weighting. See `DARTBOARD_DESIGN.md`.

**Two test runs completed:**

| Run | Threshold | Signals | Peak/day | Examples passing |
|-----|-----------|---------|----------|-----------------|
| 1 | 0.9158 (target_peak=5) | 304 | 5 | 1/66 (threshold too high) |
| 2 | 0.5948 (min example score) | 53,447 | 518 | 66/66 |

**Root cause:** The dartboard scores examples 0.59–0.92 but the universe also scores heavily in that range. Averaging 500 expression scores washes out discrimination — weak signals average together and everything scores similarly. No clean gap between examples and noise.

**Diagnosis:**
- Binary search on peak target (run 1): late-2021 signal cluster forced threshold so high only 1 example passed
- Threshold = min example score (run 2): lets everything through — 53K signals
- Fewer expressions (top 50 instead of 500): might help but the right N is arbitrary and setup-dependent
- Multiplicative scoring: everything dies (0.9^500 ≈ 0)

**Next step: HYBRID APPROACH**
Use the dartboard's Cohen's d weighting to **select** which expressions matter (deterministic, no beam search instability), then apply them as binary filters like the pyramid does. Dartboard picks the conditions, pyramid-style filtering combines them.

This gives:
- Stable expression selection (dartboard strength — no beam search random walk)
- Tight multiplicative filtering (pyramid strength — a bar must pass ALL conditions)
- Setup-agnostic: Cohen's d threshold adapts to each setup's expression landscape

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
| 7 | 2026-03-10 | 69 | dartboard (target_peak=5) | 304 | Threshold 0.9158, only 1 example passed |
| 8 | 2026-03-10 | 69 | dartboard (min example) | 53,447 | Threshold 0.5948, no discrimination |

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
11. Dartboard grinder: pure scoring doesn't discriminate. Hybrid approach (dartboard selection + pyramid filtering) is next.
