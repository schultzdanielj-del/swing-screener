# TODO — Swing Screener (2026-03-05)

## Current State

**Step 4 rebuild complete.** All grinders now use the shared expression cache (12,175 expressions). Exit grinders exclude boolean aggregations (ct_, st_, tir_ — 2,413 monotonic expressions that fire early, not at move exhaustion). 9,762 expressions tested for exits.

Latest results:
- **4a Exit Grinder (single-stage):** `slope_xavgc21_off7_adr14 below -1.1142` — 70% median capture, 24% floor, 20.7 avg bars, 59/59 examples
- **4a Exit Grinder (multi-stage):** Running — results pending
- **4b Blackout re-grind:** 87 conditions, 164 signals
- **4b Setup Refiner:** Needs re-run after fix (was reading 790 signals from re-scan instead of 164 from pyramid JSON)

62 examples in Railway DB (59 resolved in grinder — BRK-B, SMMT, VUZI not in 5yr cache).

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works |
| 1 | Optimal Samples | optimal_samples | 62 DTSS examples |
| 2 | Signal Brute Forcing | signal_brute | Complete — grind #4 done |
| 3 | Sample Expansion | sample_expansion | 168 signals to vet |
| 3b | AI Sample Review | sample_review | Working |
| **4a** | **Exit Grinder** | setup_grinder_a | **Done — single-stage chosen (70% median capture)** |
| **4b** | **Setup Grinder** | setup_grinder_b | **Blackout grind done. Refiner needs re-run (bug fixed).** |
| 5 | Market Grinder | market_grind | Next |

---

## IMMEDIATE NEXT — Re-run setup_refiner then Step 5

1. `python scripts/setup_refiner.py --setup dtss` — re-run with fix (reads 164 signals from pyramid JSON, not re-scanning)
2. Review refined signals in vetting UI
3. Build Step 5: Market Grinder

---

## Grinder Architecture Changes (2026-03-05)

**All grinders now use the shared expression cache.** Key changes this session:

1. **profit_grinder.py** — Rewritten. Loads from expr cache .npz files (12,175 expressions). No ExitExprEngine, no exit_expressions.py, no exit_compute.py. Boolean aggregations excluded (9,762 tested). Median-primary scoring with 0.15 floor minimum. 50 thresholds (was 20).

2. **multistage_exit_grinder.py** — Rewritten. Same cache-based approach. Fixed broken matrix re-indexing bug that caused 0 valid conditions. Boolean aggregations excluded.

3. **setup_refiner.py** — Fixed to read `final_signals` from pyramid JSON instead of re-scanning universe (was finding 790 vs pyramid's 164). Fixed exit direction format (`below`/`above` vs `<=`/`>=`). Fixed `results` key (was `top_conditions`).

4. **exit_expressions.py / exit_compute.py** — Still in repo but no longer imported by any grinder. Can be deleted in a cleanup pass.

### Grinder Rules (NON-NEGOTIABLE)

1. All grinders must use the largest possible shared expression set — flag out what can't compute for the specific job
2. All grinders must use the exact same computation methods so results replicate across pipeline steps
3. All grinders must use precached, precomputed, local data for fastest completion
4. All grinders must be optimized to remove network and CPU bottlenecks
5. All results must pass 100% of setup examples — none can abort or fail
6. Expression cache is the single computation path
7. Boolean aggregations (ct_, st_, tir_) excluded from exit grinders — monotonically increasing during trends, structurally wrong for exit detection

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

Key insight: Boolean aggregations (count_true, since_true, true_in_row) are monotonically increasing during trends — they always fire early. Excluding them let the grinder find `slope_xavgc21` which actually describes move exhaustion. Market grinder filtering to optimal conditions should push median capture toward 80%+.

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

## AI Vetting System

### Architecture
- Agent polls pending_examples table every 15s
- Downloads chart PNG via `/api/chart/{setup}/{ticker}/{date}`
- Calls `claude -p` with `--allowedTools Read`
- Parses essay-style output via sentiment analysis
- Posts verdict + reasoning to Railway

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
