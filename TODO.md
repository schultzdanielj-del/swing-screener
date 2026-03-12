# TODO — Swing Screener (2026-03-12)

## Pipeline (6 steps)

```
Vetting Loop (repeat until convergence):
  Step 1: Signal Grind         → examples vs universe → candidate conditions
  Step 2: Exit Grind           → optimal exit condition from example entry bars
  Step 3: Refinement Grind     → scan universe, cluster, classify, beam search winners vs losers
  Step 4: Vet                  → review winner pile, add examples, loop back to step 1

After Convergence:
  Step 5: Regime Model         → winner/loser ratio vs 266 market instruments
  Step 6: Health Check         → cycle quality, EV, promote / revert / live-ready
```

Nightly auto-refresh (4:30pm ET): OHLCV → caches → expr cache → matrix → earnings → market cache. Fully automated.

---

## DTSS — Current State

### Step 1 (Signal Grind): ✅ Done
- Pyramid grinder, D1 cap=15, beam=10000, depth=100
- 68 examples (66 with valid scan bars)
- 87 conditions → 1,218 raw → 893 deduped signals
- File: `pyramid_dtss_mp_sig1218_pk14_20260310_003848.json`

### Step 2 (Exit Grind): ✅ Done
- Exit condition: `slope_xavgc21_off7_adr14 <= -1.128826`
- File: `signal_exit_grind/exit_grind_dtss_*.json`

### Step 3 (Refinement Grind): ✅ Done (2026-03-12)
- Cluster-aware beam search engine (new as of today)
- 893 clusters: 365 WIN, 528 LOSS, 325 leftward bars
- 100 refinement conditions, depth capped at 100
- 426/528 losing clusters eliminated (80.7%)
- All 365 winners pass all conditions
- 182 combined conditions (87 signal + 100 refinement, 5 overlap)
- **Pre-regime win rate: 78%** (365 winners / 467 total signals)
- File: `refinement_dtss_cl102_pk5_20260312_150704.json`

### Step 4 (Vet): ⏳ Next
- Review the 365 winner pile for new examples

### Step 5 (Regime Model): Not built
- Run on pre-refinement piles (full 893 clusters, 365 WIN / 528 LOSS)
- Needs enough losers to find patterns — don't use post-refinement data
- Script: `scripts/market_grinder.py` (exists but needs wiring to new pipeline)

### Step 6 (Health Check): Not built
- Script: `scripts/cycle_health.py` (exists but needs update)

---

## Immediate Tasks

1. **Depth progression output** — save level-by-level best path and cluster survival count in refinement JSON. Allows post-hoc condition threshold tuning without re-running the grinder.
2. **Update PIPELINE_V2.md** — refinement grinder spec says "not yet built" for cluster-aware engine. Now built and working. Remove proximity grind and profit grind.
3. **Vet the winner pile** (Step 4) — review 365 winners, add new examples, decide if another loop is needed.

---

## Pipeline Status

| Step | Script | Status |
|------|--------|--------|
| 1. Signal Grind | `pyramid_grinder.py` | ✅ Working |
| 2. Exit Grind | `signal_exit_grinder.py` | ✅ Working |
| 3. Refinement Grind | `pyramid_grinder.py --blackout` | ✅ Working (cluster-aware) |
| 4. Vet | UI + manual | ⏳ Next |
| 5. Regime Model | `market_grinder.py` | ⏸ Not wired |
| 6. Health Check | `cycle_health.py` | ⏸ Not wired |

---

## Key Design Decisions

- **Pyramid with D1 cap=15 is the official step 1 engine.** Experimental grinders (dartboard, hybrid) all tested and failed. Files preserved but shelved.
- **Beam search instability is accepted.** ~9.4% Jaccard similarity between runs, but individual runs produce usable signal sets.
- **Proximity grinder is obsolete.** Its job (trim losers via beam search) is now handled by the refinement grinder's cluster-aware engine.
- **Profit grinder removed from pipeline.** Trade exit optimization deferred.
- **Regime model should run on pre-refinement data.** Post-refinement has too few losers (102) for the model to learn from. Pre-refinement has the full 528 losers.
- **Refinement depth is an overfitting risk.** 100 conditions on 365 winners is aggressive. Depth progression output will allow post-hoc threshold tuning. The regime model is the real overfitting filter.
- **No re-scan, no re-classify in refinement.** Phase 1 classification (ceiling+exit race) is truth. The beam search filters signals by whole-cluster elimination only.
- **All grinders must produce 100% example pass rate.** Any result where an example fails is invalid.
- **Silent pipeline failures are dangerous.** The system produces plausible-looking wrong numbers with no errors. Verify everything empirically.

---

## Nightly Auto-Refresh (4:30pm ET)

7 steps, fully automated via agent:
1. Railway OHLCV append
2. Daily cache rebuild
3. 5yr cache rebuild
4. Expression cache append
5. Matrix rebuild
6. Earnings dates update
7. Market cache append (266 instruments)

---

## Infrastructure

- **Repo:** `schultzdanielj-del/swing-screener`, branch `v2`
- **Railway:** `https://web-production-e3025.up.railway.app`
- **Expression cache:** 16,051 expressions, ~21 GB, `EXPR_CACHE_WORKERS=8`
- **5yr OHLCV cache:** ~4,167 tickers
- **File mirror:** All grind results uploaded to Railway via `file_mirror.py`
- **Tickers not in cache:** BRK-B, SMMT, VUZI (not in 5yr). SERV, SOUN (<50 bars, not in expr cache).

---

## Shelved / Legacy

- `dartboard_grinder.py` — additive scoring washes out discrimination
- `hybrid_grinder.py` — correlated booleans don't filter
- `proximity_grinder.py` — replaced by refinement grinder
- `profit_grinder.py` — removed from pipeline
- `setup_refiner.py` — legacy, unused
- `signal_filter.py` classified output — replaced by `raw_signal_clusters_{setup}.json`
