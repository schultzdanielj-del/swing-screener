# TODO — Swing Screener (2026-03-06)

## Current State

**71 DTSS examples.** Grind #5 in progress (Steps 2 → 3 → 4 re-running with new examples).

**Step 4 Signal Viewer — COMPLETE.** Sample Expansion page now has Step 2 / Step 4 source toggle. Step 4 mode reads from `refined_{setup}.json`, deduplicates against existing examples, disables MAYBE, shows "NO EXIT" badge on signals with no exit trigger.

**AI audit auto-fires on YES.** No more manual "Submit for Audit" button. Every YES verdict immediately queues a `sample_review` job if the agent is online and no other step is running.

**review_samples.py parser fixed.** Fallback keyword scanner handles unstructured Claude CLI responses (APPROVE/REJECT keyword scan when VERDICT: line not found).

Final DTSS configuration (grind #4, pre-grind #5):
- **Signal conditions:** 87 conditions from blackout re-grind (62 examples, 59 resolved)
- **Exit condition:** `slope_xavgc21_off7_adr14 below -1.1142` — 70% median capture, 24% floor, 20.7 avg bars
- **Refiner output:** 164 raw → 132 deduped → 44 with exit → 42 filtered signals across 5yr

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works |
| 1 | Optimal Samples | optimal_samples | **71 DTSS examples** |
| 2 | Signal Brute Forcing | signal_brute | **Grind #5 running** |
| 3 | Sample Expansion | sample_expansion | Re-running after grind #5 |
| 4a | Exit Grinder | setup_grinder_a | Done — single-stage chosen (70% median capture) |
| 4b | Setup Grinder | setup_grinder_b | Re-running after grind #5 |
| **5** | **Market Grinder** | **market_grind** | **NEXT — build after grind #5 complete** |

---

## IMMEDIATE NEXT — Step 5: Market Grinder

Step 4 Signal Viewer is done (Task 1 complete). Now build the Market Grinder.

### What the Market Grinder Does

**Inputs:**
- Setup examples (always winners — confirmed by human vetting)
- ALL deduped signals from Step 4b (`refined_{setup}.json`) — the full historical universe of every time the scan would have fired, exit trigger or not

**The ratio:**
- Examples = confirmed winners (numerator)
- All Step 4b signals minus examples = everything else the scan ever fired on (denominator)
- Win rate = examples / total signals at any historical point

**The grinder:**
- Computes SPY regime indicators at each historical signal date
- Buckets signals by regime state
- Computes win rate per bucket vs baseline
- Finds which SPY conditions correlate with higher win rates

**Output:** A live win rate estimator — given today's SPY conditions, what's the expected win rate on tonight's signals.

### Market Grinder Script (`scripts/market_grinder.py`) — TO BUILD

**Inputs:**
- `data/setup_refiner/refined_{setup}.json` — all deduped signals (exit or no-exit)
- Railway DB examples table — always winners
- `local_runner/cache/universe_ohlcv_5yr.pkl` — SPY OHLCV

**Classification:**
- Examples → winner
- All other signals → loser (no exit trigger = didn't pay, exit trigger = exit fired but not a confirmed setup)
- Manual NO verdicts on exit-triggered signals → loser override

**Regime indicators (computed on SPY at each signal date):**
- SPY vs SMA50, SMA200 (above/below)
- SPY EMA8 vs EMA21 vs SMA50 stack
- SPY extension from SMA50/SMA200 in ATR
- SPY SMA50/SMA200 slope
- SPY RSI(14) — bucketed <40 / 40-60 / >60
- SPY ROC(20)
- SPY % from 52-week high
- SPY ATR14 vs its 50d average (expanding/contracting)

**Output:** `data/market_grind/market_{setup}.json`

### UI (Market Grinder Page) — TO BUILD

Replace Step 5 "coming soon" placeholder with:
- RUN button
- Regime model table: win rate per bucket vs baseline
- Current market indicator: "Today's regime → expected win rate: X%"
- Per-signal classification table

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

### Changes This Session (2026-03-06)

1. **Step 4 Signal Viewer built** — `GET /api/setup-grinder/{setup_type}/signals` endpoint added to server.py. Reads `refined_{setup}.json`, deduplicates against examples (same 5-day window as Step 2), attaches vetting verdicts. VettingPage gets Step 2 / Step 4 source toggle with MAYBE disabled in Step 4 mode and "NO EXIT" badge for signals without exit_date.

2. **AI audit auto-fires on YES** — `save_vetting_decision` in server.py now queues `sample_review` job immediately when verdict is YES. SUBMIT FOR AUDIT button removed from frontend. Guards: agent alive, no other step running, no review already queued.

3. **review_samples.py parser fixed** — Added fallback keyword scanner: if no `VERDICT:` line found in Claude CLI response, scans full output for APPROVE/REJECT keywords. If both present, takes the last one. Previously all verdicts defaulted to UNKNOWN.

### Changes Previous Session (2026-03-05)

1. **setup_refiner.py** — Rebuilt with single-pass LOO condition pruning via boolean matrix (61s for 87 conditions × 4,167 tickers).
2. **Server endpoint fix reverted** — vetting signals endpoint reads from signal_filter path, not setup_refiner path.
3. **Market Grinder design finalized** — winner/loser classification system documented.

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Peak/day | Notes |
|-------|------|----------|------------|---------|----------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | 3 | First production grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | 3 | After first vetting pass |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | ~3 | After second vetting pass |
| 4 | 2026-03-03 | 62 (59 resolved) | 86 | 168 | 4 (1mo) / 11 (5yr) | After AI vetting |
| 4b-blackout | 2026-03-05 | 62 (59 resolved) | 87 | 164 | — | Blackout re-grind |
| **5** | **2026-03-06** | **71** | **TBD** | **TBD** | **TBD** | **In progress** |

---

## Exit Grinder Results (2026-03-05)

| Type | Expression | Floor | Median | Avg Bars | Notes |
|------|-----------|-------|--------|----------|-------|
| Single | `slope_xavgc21_off7_adr14 below -1.1142` | 24% | 70% | 20.7 | **Chosen** — EMA21 slope flattening = move exhaustion |
| Multi (old) | ext_ceil + bb_pctb | 30.9% | 59.7% | 19.5 | Not competitive |

---

## Refiner Results (2026-03-05)

**Without pruning (--skip-prune):** 87 conditions, 164 → 132 deduped → 44 with exit → 42 filtered. Median 4.46 ADR captured.

**86 of 132 deduped signals (66%) had no exit trigger within 120 bars.** These are the denominator for the Market Grinder win rate calculation (losers by default).

---

## Condition Pruning (BUILT, NOT APPLIED)

`setup_refiner.py --min-power 0.10` pruned 87 → 12 conditions. Too aggressive. Not applied — 87-condition signal set finds the right neighborhood.

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
- 71 DTSS examples (68 in cache, 3 excluded: BRK-B, SMMT, VUZI)

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
