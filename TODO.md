# TODO — Swing Screener (2026-03-11)

## V2 Pipeline — The Correct Order

See `PIPELINE_V2.md` for full spec.

**Nightly auto-refresh (4:30pm ET):** OHLCV → caches → expr cache → matrix → earnings → market cache (266 instruments). Fully automated.

**The Vetting Loop (repeat until convergence):**
1. Signal Grind — examples vs universe → conditions (**pyramid grinder is the official engine**)
2. Exit Grind — optimal exit condition from example signal bar close forward
3. Scan — apply conditions to 5yr → deduped signals + exit filter → classified signal set
4. Refinement Grind — winners vs losers beam search → combine conditions → re-scan → re-classify. Manual gate.
5. Vet — review winner pile. YES → AI review → approve → examples → loop.

**After Convergence (run once, in order):**
6. Proximity Grind — beam search to trim losers → combine all conditions → re-scan → re-classify.
7. Profit Grind — trade exit from entry bar high forward.
8. Regime Model — winner/loser ratio vs 266 market instruments
9. Health Check — cycle quality, EV, promote/revert

---

## Current State — DTSS

### Active Grind: pyramid_dtss_mp_sig1218_pk14_20260310_003848

- **Cycle ID:** dtss_signal_grind_20260310_043849 (activated on Railway)
- **Engine:** Pyramid grinder with D1 cap at 15
- **Params:** beam=10000, depth=100, d1_depth=15, peak_target=3, multi_pass=True
- **Examples:** 68 (66 with valid scan bars — BRK-B, SMMT, VUZI excluded from cache)
- **Conditions:** 87 (D1:15, 1wk:14, 1mo:22, 6mo:16, 1yr:10, 5yr:10)
- **Signals:** 1,218 raw → 893 deduped

### Signal Filter Results (run against 1,218 grind)

- 893 deduped → 727 exit triggered → 483 filtered (>= 1.8 ADR)
- **AUTO_WIN: 380** (examples: 79, exit_filter: 301)
- **AUTO_LOSS: 513**
- **Win rate: 42.6%** (380/893)
- **Median ADR threshold: 4.4**
- Exit condition: `slope_xavgc21_off7_adr14 <= -1.128826`

### Step 4 (Refinement Grind): ✅ FIXED (2026-03-11)

**What it does:**
1. Gathers raw signal clusters (`raw_signal_clusters_{setup}.json`) → classifies via ceiling+exit race → winners/losers
2. Loads signal grind conditions from pyramid result + exit condition (all local)
3. Beam search: winners as must-pass, losers as universe to filter
4. Combines signal + refinement conditions
5. Re-scans full universe with combined conditions (parallel)
6. Re-deduplicates → new deduped + sacrificial sets
7. Applies exit + classifies → new winner/loser piles
8. Saves locally + uploads via file_mirror + grind_uploader

**Output:** `local_runner/cache/refinement_{setup}_*.json` with `all_conditions`, `winner_signals`, `loser_signals`, `sacrificial_signals`, `exit_condition`

### Proximity Grind (Step 6): ✅ FIXED (2026-03-11)

Same pattern as refinement grinder:
1. Reads refinement grinder local output (all_conditions, signals, exit_condition)
2. Beam search: finds proximity conditions trimming losers
3. Combines signal + refinement + proximity conditions
4. Re-scans full universe with combined conditions
5. Re-deduplicates + classifies → final signal set
6. Saves locally + uploads via file_mirror + grind_uploader

**Output:** `data/proximity_grind/proximity_{setup}_*.json` with `all_conditions`, `winner_signals`, `loser_signals`, `sacrificial_signals`

### Experimental Grinders — SHELVED

Three alternative grinder approaches were tested and all failed to improve on the pyramid:

1. **Dartboard (pure scoring):** Gaussian kernel + Cohen's d. 53K signals or 1/66 examples. Additive averaging washes out discrimination.
2. **Hybrid top-N Cohen's d:** Top 200 expressions by Cohen's d, binary filtering. 28,609 signals. Correlated booleans don't filter.
3. **Hybrid greedy marginal:** Iterative greedy selection (pick condition that kills most surviving bars). 100 conditions, 33,277 signals. Diminishing returns — can't get below ~200K raw bars.

**Root cause:** With 66 examples, min/max bounding boxes are fundamentally too wide for binary filtering alone to reach <1000 signals. The pyramid beam search reaches tighter results (1,218) because it finds specific combinatorial corridors, even though those corridors shift between runs (instability).

**Decision:** Pyramid grinder with D1 cap at 15 is the official step 1 engine. The instability between runs is acceptable — individual runs produce usable signal sets. Files: `hybrid_grinder.py` and `dartboard_grinder.py` preserved but not in use.

---

## Pipeline Audit (2026-03-10)

| Step | Script | Status | Notes |
|------|--------|--------|-------|
| 1. Signal Grind | `pyramid_grinder.py` | ✅ Working | D1 cap=15 produces best results |
| 2. Exit Grind | `signal_exit_grinder.py` | ✅ Working | One issue: picks pyramid file by mtime (revert problem) |
| 3. Scan | `signal_filter.py` | ✅ Working | Minor: classification uses signal median not example median |
| 4. Refinement Grind | `pyramid_grinder.py --blackout` | ✅ FIXED | Winners-vs-losers beam search + re-scan + re-classify |
| 5. Vet | UI + manual | ⏸ Waiting | |
| 6. Proximity Grind | `proximity_grinder.py` | ✅ FIXED | Reads refinement output, beam search + re-scan + re-classify |
| 7. Profit Grind | `profit_grinder.py` | ✅ Built | Has been run (68ex, 4.0 ADR) |
| 8. Regime Model | `market_grinder.py` | ⏸ Not built | |
| 9. Health Check | `cycle_health.py` | ⏸ Not built | |

### Signal Filter Classification Issue (minor)

Spec says AUTO_WIN threshold = "derived from sample median" (example exit distances). Code computes median from ALL exit-triggered signals' move_adr instead. Impact: win rate slightly inflated. Fix: use example median (5.0 ADR) not signal median (4.4 ADR).

---

## Grind History (DTSS)

| Date | Examples | Engine | Conditions | Deduped Signals | Peak/day | Notes |
|------|----------|--------|------------|-----------------|----------|-------|
| Feb 27 | 20 | pyramid | 41 | 339 | 3 | |
| Feb 28 | 20 | pyramid | 41 | 264 | 3 | |
| Mar 2 | 33 | pyramid | 32 | 2,500 | 15 | |
| Mar 3 | 35 | pyramid | 53 | 409 | 3 | |
| Mar 3 | 45 | pyramid | 76 | 610 | 6 | |
| Mar 3 | 48 | pyramid | 80 | 489 | 5 | |
| Mar 4 | 59 | pyramid | 86 | 803 | 11 | |
| Mar 6 | 68 | pyramid | 94 | 1,691 | 20 | |
| Mar 9 | 68 | pyramid | 89 | 1,395 | 16 | |
| **Mar 10** | **68** | **pyramid (D1 cap=15)** | **87** | **1,218** | **14** | **← ACTIVE** |
| Mar 10 | 66 | pyramid (D1 cap=15) | 82 | 1,292 | 16 | |
| Mar 10 | 68 | pyramid (uncapped) | 68 | 5,615 | 53 | D1 over-locked |
| Mar 10 | 66 | hybrid top-N d>0.5 | 200 | 28,609 | 743 | Shelved |
| Mar 10 | 66 | hybrid greedy | 100 | 33,277 | 380 | Shelved |

---

## Data

- Expression library: 15,805 expressions
- Expression cache: 4,119 tickers × 15,805 expr
- Market cache: 245/266 instruments × 15,805 expr
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- 69 DTSS examples (BRK-B, SMMT, VUZI excluded from cache → 66 usable)

---

## Immediate Next

1. Run refinement grinder on DTSS (step 4)
2. Run proximity grinder on DTSS (step 6)
3. Continue to step 7 (profit grind) and step 8 (regime model)

---

## Rules

1. NEVER proceed without explicit go-ahead
2. Push all work to GitHub before ending chat (v2 branch)
3. Break work into small tasks
4. All OHLCV from Railway DB or local caches. Never yfinance.
5. Read ta_knowledge.md before any TA work
6. NEVER dump large data into context
7. Expression cache = single computation path
8. V2 only — no V1 patching
9. Read PIPELINE_V2.md before touching any grinder or pipeline code
10. 100% example pass rate on all grinders, non-negotiable
11. Pyramid grinder is the official step 1 engine
12. READ THE CODE before making claims about what it does
