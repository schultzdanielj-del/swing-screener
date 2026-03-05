# TODO — Swing Screener (2026-03-05)

## Current State

**Step 4 COMPLETE.** Signal conditions, exit condition, and blackout re-grind all finalized.

Final DTSS configuration:
- **Signal conditions:** 87 conditions from blackout re-grind (62 examples, 59 resolved)
- **Exit condition:** `slope_xavgc21_off7_adr14 below -1.1142` — 70% median capture, 24% floor, 20.7 avg bars
- **Refiner output:** 164 raw → 132 deduped → 44 with exit → 42 filtered signals across 5yr
- **Condition pruning:** Built and functional (single-pass LOO, ~61s) but not applied — 87 conditions kept as-is. Pruning to 12 conditions was too aggressive (blew selectivity). The conditions are finding the right neighborhood; rejects are near-misses (triple tops, misshapen, untradable) not garbage.

62 examples in Railway DB (59 resolved in grinder — BRK-B, SMMT, VUZI not in 5yr cache).

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works |
| 1 | Optimal Samples | optimal_samples | 62 DTSS examples |
| 2 | Signal Brute Forcing | signal_brute | Complete — grind #4 done |
| 3 | Sample Expansion | sample_expansion | Complete — 3 vetting rounds done |
| 4a | Exit Grinder | setup_grinder_a | **Done — single-stage chosen (70% median capture)** |
| 4b | Setup Grinder | setup_grinder_b | **Done — 87 conditions, 164 signals, refiner working** |
| **5** | **Market Grinder** | **market_grind** | **NEXT** |

---

## IMMEDIATE NEXT — Step 5: Market Grinder

See ANALYSIS_SYSTEM.md Step 5 for full spec. Two tasks to build before the grinder itself:

### Task 1: Step 4 Signal Viewer (UI + Server)

The Sample Expansion vetting page needs a **Step 2 / Step 4** source toggle so we can browse the full 132 Step 4 signals (not just the 42 filtered ones). This is the UI needed to audit signals for hidden examples and assign market grinder verdicts.

**Server change:**
- Add `GET /api/setup-grinder/{setup_type}/signals` — reads from `data/setup_refiner/refined_{setup_type}.json`, returns all signals including the 86 with no exit trigger

**Frontend change (VettingPage):**
- Add two-button toggle at top: **Step 2** (reads `/api/vetting/{setup}/signals`) vs **Step 4** (reads `/api/setup-grinder/{setup}/signals`)
- In Step 4 mode: signals with no exit trigger shown in list with a visual indicator (e.g. dim color, "no exit" tag)
- YES/NO verdicts still enabled in Step 4 mode — NO overrides feed the Market Grinder winner/loser classification
- MAYBE disabled in Step 4 mode (not needed for market grinder)

### Task 2: Market Grinder Script + UI

See ANALYSIS_SYSTEM.md Step 5 for full spec.

---

## Grinder Architecture

### Grinder Rules (NON-NEGOTIABLE)

1. All grinders must use the largest possible shared expression set — flag out what can't compute for the specific job
2. All grinders must use the exact same computation methods so results replicate across pipeline steps
3. All grinders must use precached, precomputed, local data for fastest completion
4. All grinders must be optimized to remove network and CPU bottlenecks
5. All results must pass 100% of setup examples — none can abort or fail
6. Expression cache is the single computation path
7. Boolean aggregations (ct_, st_, tir_) excluded from exit grinders — monotonically increasing during trends, structurally wrong for exit detection

### Changes This Session (2026-03-05)

1. **setup_refiner.py** — Rebuilt with single-pass LOO condition pruning via boolean matrix (61s for 87 conditions × 4,167 tickers). Cache-excluded tickers skipped in validation. NPZ column slicing (87 cols instead of 12,175). Phase 2 re-scans when conditions pruned.

2. **Server endpoint fix reverted** — vetting signals endpoint reads from signal_filter path (sample expansion stage), not setup_refiner path. These are different pipeline stages.

3. **Market Grinder design finalized** — winner/loser classification system documented. See ANALYSIS_SYSTEM.md Step 5.

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Peak/day | Notes |
|-------|------|----------|------------|---------|----------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | 3 | First production grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | 3 | After first vetting pass |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | ~3 | After second vetting pass |
| 4 | 2026-03-03 | 62 (59 resolved) | 86 | 168 | 4 (1mo) / 11 (5yr) | After AI vetting |
| 4b-blackout | 2026-03-05 | 62 (59 resolved) | 87 | 164 | — | Blackout re-grind (example bars masked) |

---

## Exit Grinder Results (2026-03-05)

| Type | Expression | Floor | Median | Avg Bars | Notes |
|------|-----------|-------|--------|----------|-------|
| Single | `slope_xavgc21_off7_adr14 below -1.1142` | 24% | 70% | 20.7 | **Chosen** — EMA21 slope flattening = move exhaustion |
| Multi (old) | ext_ceil + bb_pctb | 30.9% | 59.7% | 19.5 | Not competitive |

---

## Refiner Results (2026-03-05)

**Without pruning (--skip-prune):** 87 conditions, 164 → 132 deduped → 44 with exit → 42 filtered. Median 4.46 ADR captured. This is the final Step 4 output.

**86 of 132 deduped signals (66%) had no exit trigger within 120 bars.** These are auto-classified as losers in the Market Grinder (unless manually overridden with a NO verdict in the Step 4 viewer).

---

## Condition Pruning (BUILT, NOT APPLIED)

`setup_refiner.py --min-power 0.10` pruned 87 → 12 conditions. Too aggressive — individual filter power doesn't account for collective filtering. Would need iterative pruning to be useful, but unclear if pruning is needed at all. The 87-condition signal set finds the right neighborhood; rejects are near-misses not garbage.

---

## The Math

- Losers: solidly under 1 ADR
- Winners: median 6.0 ADR captured, floor 3.4 ADR
- Even 36% win rate -> positive expectancy
- 50% win rate (after market grinder) -> massive profit factor
- 10% position size, 25% net winner/month = 2.5% compounding
- 20 years of 2.5%/month = mid 8 figures

---

## Data

- Expression cache: 4,119 tickers x 12,421 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- Earnings dates: batch scraped nightly
- 62 DTSS examples (59 in cache, 3 excluded: BRK-B, SMMT, VUZI)

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
