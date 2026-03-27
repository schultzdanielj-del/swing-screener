# Universe Expansion + Expression Cache Vectorization

**Created:** 2026-03-26
**Status:** Planning — no code changes until plan is fully vetted
**Branch:** v2

---

## THE PROBLEM

The grinder's data is bad in two directions:

1. **Contaminated signals:** 42.5% of BRKO signals (1,677 / 3,946) have ADR below $1.00. Bond ETFs (FTSM $0.025 ADR), fixed-income funds, and micro-ADR stocks produce garbage signals with inflated move_adr values (FTSM showing a "353.7 ADR move"). The `tradable_universe` table on Railway was supposed to filter these out but **never actually filtered anything** — `server.py` line 1441 inserts every ticker with OHLCV data directly into `tradable_universe` with zero checks. The `build_tradable.py` script with proper filters (price ≥ $1, APTR ≥ 1.5%, avg dollar volume ≥ $5M) was never wired into the Railway pipeline.

2. **Missing signals:** 6,857 tickers have OHLCV data on Railway but are not in `tradable_universe`. These tickers — including stocks that were liquid and volatile historically but have since delisted, faded, or fallen below current thresholds — are completely invisible to the grinder. Valid historical setups from these tickers are lost.

3. **No ticker discovery:** `append_daily` only pulls new bars for tickers already in `tradable_universe`. New IPOs, relisted tickers, or tickers that cross into tradable territory never enter the pipeline. There is no mechanism to discover new tickers.

**Nothing downstream of the expression cache (signal grind, refinement, EV, profit grind) can produce trustworthy results until this is fixed.**

---

## THE SOLUTION

Three components:

### A. Per-bar tradable filters in the grinder

Instead of a ticker-level gate, check tradability at each bar index during the grind scan. A ticker can be tradable in 2022 and untradable in 2024, or vice versa. The per-bar check is the only correct approach for historical scanning.

Filters (checked per bar from expression cache / OHLCV):
- `adr14 >= $1.00` (dollar ADR floor — catches bond ETFs, money markets, micro-ADR)
- `close >= $1.00` (price floor — catches sub-penny, near-delisting)
- `avg_dollar_volume_20d >= $5,000,000` (liquidity floor — catches illiquid)

These use data already in the expression cache (`adr14`) and OHLCV cache (close, volume). No new data sources needed.

### B. Expand universe to all ~11,000 tickers

Pull OHLCV and build expression caches for all tickers with data, not just the current ~4,100. This recovers 6,857 tickers of historical signal data.

### C. Vectorize the expression cache builder

The current builder computes expressions per-ticker using pandas Series operations. For 11,000 tickers this takes ~4.5 hours (full build). The nightly "append" actually recomputes the full bar history per ticker, not just new bars — it takes ~60 min for 4,100 tickers, would take ~160 min for 11,000.

The new approach: load OHLCV as 2D numpy matrices (n_tickers × n_bars), compute each expression across all tickers simultaneously using vectorized numpy operations, write per-ticker .npz files.

---

## ARCHITECTURE

### Expression cache builder — vectorized

**Current flow (per-ticker, pandas):**
```
for each ticker (11,000):
    load OHLCV → create ExpressionEngine → compute 15,805 expressions → save .npz
    ~1.5s per ticker with 10 workers × 11,000 = ~4.5 hours
```

**New flow (batched, numpy 2D):**
```
Load all OHLCV into matrices: O, H, L, C, V each (11,000 × 1,260) = 330MB total
Load weekly OHLCV into matrices: O, H, L, C, V each (11,000 × 252) = 66MB total
Load monthly OHLCV into matrices: O, H, L, C, V each (11,000 × 60) = 16MB total

For each batch of N tickers:
    Slice OHLCV matrices → (N × n_bars)
    Compute all base intermediates (26 MAs, ATR, ADR, rolling max/min)
    Compute all 15,805 expressions as matrix operations
    Allocate output: (N × n_bars × 15,805) float32
    Write N .npz files
    Free batch memory
```

### RAM budget per batch

| Batch size | Output array | Base intermediates | OHLCV (resident) | Peak total |
|------------|-------------|-------------------|-------------------|------------|
| 25 tickers | 2.0 GB | ~250 MB | 412 MB | ~3 GB |
| 50 tickers | 4.0 GB | ~500 MB | 412 MB | ~5 GB |
| 100 tickers | 8.0 GB | ~1.0 GB | 412 MB | ~10 GB |

Recommended: **batch size 25-50**, configurable via CLI. Need to confirm available RAM on Dan's machine.

### HTF — direct OHLCV pull instead of resampling

**Current:** Resample daily → weekly/monthly per-ticker using pandas. Expensive.
**New:** Pull weekly and monthly OHLCV directly from yfinance. Store as separate caches.

- Weekly: ~252 bars × 11,000 tickers. ~55MB as float32 matrix.
- Monthly: ~60 bars × 11,000 tickers. ~13MB as float32 matrix.

HTF expressions use the same vectorized batch approach on the weekly/monthly matrices. The 5,233 weekly and 5,233 monthly expressions are the same base ops (extension, RSI, MA slope, etc.) applied to weekly/monthly OHLCV instead of daily.

**Daily-to-HTF mapping:** US equities share the same trading calendar. One mapping array maps each daily bar index to its corresponding weekly/monthly bar index. Applied to all tickers identically (with edge case handling for tickers with different bar counts due to listing/delisting dates).

**yfinance bar boundaries:** Weekly candles from yfinance end on Friday (last trading day). Current resampling uses pandas `resample('W')` which ends on Sunday. Values will differ slightly. This doesn't matter — everything gets a full regrind after this change. The new yfinance values become the baseline.

### Nightly flow (post-implementation)

```
Step 1: OHLCV pull — yfinance daily + weekly + monthly for all active tickers (~11,000)
Step 2: Vectorized expression cache full rebuild (~15-20 min)
Step 3: D1 matrix rebuild
Step 4: Earnings, market cache, fundamentals (unchanged)
Step 5: Seed vault backup to Railway
```

The nightly "append" becomes a full rebuild because the vectorized approach makes it fast enough. No incremental append logic needed — simpler code, no edge cases around partial updates.

Dead tickers (no new bars) still get rebuilt but their output is identical to last time — same OHLCV in, same expressions out. The extra compute is trivial in the vectorized approach.

---

## EXPRESSION VECTORIZATION — DETAILED BREAKDOWN

### Category analysis

| Category | Count | Vectorizable? | Notes |
|----------|-------|---------------|-------|
| htf_weekly | 5,233 | YES | Same ops as daily, on weekly OHLCV matrix |
| htf_monthly | 5,233 | YES | Same ops as daily, on monthly OHLCV matrix |
| boolean (count_true, since_true, true_in_row) | 2,413 | YES | 127 unique base conditions, all simple comparisons. Rolling sum/scan on bool arrays. |
| extension_structure (on_series) | 1,198 | YES | Second pass — depends on base extension series computed first |
| ma_slope | 240 | YES | (MA - MA.shift(offset)) / normalizer |
| near_resistance | 203 | YES | Distance to rolling max |
| momentum | 138 | MOSTLY | RSI, ROC, stochastic, CCI: yes. ADX: sequential Wilder smoothing, vectorized across tickers. |
| near_support | 133 | YES | Distance to rolling min |
| extension | 98 | YES | (close - MA) / normalizer |
| extension_dynamics | 91 | YES | Slopes/deltas of extension series |
| lsp | 80 | **NO** | Per-ticker pattern detection (lsp_detector_v2) |
| ma_cross | 72 | YES | Boolean crossover on MA arrays |
| spread_slope | 64 | YES | Slope of MA spread |
| range | 59 | YES | High-low range ops |
| volume_character | 49 | YES | Volume / avg volume ratios |
| swing_structure | 48 | YES | Higher high/low counts |
| ma_spread | 46 | YES | (fast_MA - slow_MA) / normalizer |
| algo_lines | 44 | **NO** | Per-ticker (algo_line_detector) |
| All other small categories | ~363 | MOSTLY YES | candle_pattern, percentile_rank, vwap, macd, bollinger, etc. |

**Summary:**
- Vectorizable: ~15,681 expressions (99.2%)
- Per-ticker only: ~124 expressions (LSP: 80, algo lines: 44)

The 124 per-ticker expressions run in a separate pass after the batch phase. With 10 ProcessPoolExecutor workers, ~10 minutes for 11,000 tickers.

### Key numpy implementations needed

**Rolling SMA (2D):** Cumsum trick. O(n) per row, vectorized across all rows. Benchmarked: 2 SMAs across 11,000 × 1,260 in 713ms.

**EMA (2D):** Sequential dependency — must loop over axis=1 (bars) but vectorized across axis=0 (tickers). 1,260 iterations, each a vectorized op across all tickers in the batch. <100ms per period.

**RSI (2D):** gains = max(diff, 0), losses = max(-diff, 0). Wilder smooth both (same loop pattern as EMA). RSI = 100 - 100/(1 + avg_gain/avg_loss).

**Rolling max/min (2D):** `numpy.lib.stride_tricks.sliding_window_view` + max/min along window axis. May need chunking for large windows to avoid memory spikes.

**ADX (2D):** Most complex. +DM, -DM, Wilder-smoothed TR, +DI, -DI, DX, Wilder-smoothed DX. All sequential (Wilder) but vectorized across tickers. ~5 passes over bar axis.

**count_true (2D):** Rolling sum of boolean array. Cumsum trick on bool→int.

**since_true (2D):** Bars since last True. Sequential scan, vectorized across tickers:
```python
for i in range(1, n_bars):
    result[:, i] = np.where(bool_arr[:, i], 0, result[:, i-1] + 1)
```

**true_in_row (2D):** Consecutive True count. Same pattern:
```python
for i in range(1, n_bars):
    result[:, i] = np.where(bool_arr[:, i], result[:, i-1] + 1, 0)
```

### Bar count alignment

Not all tickers have 1,260 daily bars. Some have fewer (recent IPO), some may have more. Two options:

**Option A — Pad to max length:** All tickers padded with NaN to the longest ticker's bar count. Wastes some memory but uniform matrix shape. NaN handling in numpy ops is straightforward (nanmean, etc.) and matches the existing NaN-as-missing convention.

**Option B — Group by bar count:** Tickers grouped into cohorts with similar bar counts, each cohort processed as a separate matrix. More efficient but adds batch management complexity.

Recommended: **Option A** (pad with NaN). Simpler, and the memory waste is minimal — most tickers have similar bar counts.

---

## OHLCV DATA

### Current state (Railway)

- `universe_ohlcv`: 11,026 distinct tickers with daily OHLCV
- `tradable_universe`: 4,169 tickers (NOT filtered — blind INSERT OR IGNORE)
- Gap: 6,857 tickers have OHLCV but are not in `tradable_universe`
- 10,852 of 11,026 tickers have bars as recent as March 2025

### What's needed locally

| Timeframe | Source | Tickers | Approx bars | Storage |
|-----------|--------|---------|-------------|---------|
| Daily | Railway (existing) + yfinance (gap fill) | ~11,000 | ~1,260 | ~2GB pkl |
| Weekly | yfinance `interval='1wk'` (new) | ~11,000 | ~252 | ~400MB pkl |
| Monthly | yfinance `interval='1mo'` (new) | ~11,000 | ~60 | ~100MB pkl |

### Storage format

Three separate pickle files:
- `universe_ohlcv_daily.pkl` (rename from `universe_ohlcv_5yr.pkl`)
- `universe_ohlcv_weekly.pkl` (new)
- `universe_ohlcv_monthly.pkl` (new)

### The dvol_20d expression

Not currently in the expression library. Needed for per-bar dollar volume floor check. Add to `brute_expressions.py`:
```
name: "dvol_20d"
category: "volume_character"  
compute: {"op": "dollar_volume_avg", "period": 20}
```
This is SMA(close × volume, 20). Available in the expression cache like `adr14`. Must be added before the full rebuild.

---

## EXECUTION PLAN

### Phase 0: Preparation (no code changes)
- [ ] Check what's in local SQLite — how many tickers have daily OHLCV locally vs only on Railway
- [ ] Benchmark yfinance weekly + monthly pull for 100 tickers — verify data quality and rate limits
- [ ] Confirm RAM on Dan's machine (expect 32GB) and decide batch size
- [ ] Confirm disk space available for expanded expression cache (~110GB)
- [ ] Verify: sample 10 tickers, compare yfinance weekly OHLCV vs pandas resample('W') on daily data — document differences

### Phase 1: OHLCV expansion (one-time, run overnight)
- [ ] Pull all ~11,000 tickers' daily OHLCV into local 5yr cache (from Railway bulk query, not per-ticker)
- [ ] Pull weekly OHLCV for all ~11,000 tickers from yfinance
- [ ] Pull monthly OHLCV for all ~11,000 tickers from yfinance
- [ ] Verify bar counts and date alignment across all three timeframes
- [ ] Store as three .pkl files

### Phase 2: Vectorized expression cache builder
- [ ] Add `dvol_20d` expression to `brute_expressions.py`
- [ ] Build numpy 2D implementations for each expression op category
- [ ] **Validation gate:** For 50 sample tickers, compute expressions both ways (old pandas vs new numpy). All values must match within float32 tolerance (1e-4). No proceeding until this passes.
- [ ] Build the batched pipeline: load OHLCV matrices → batch → compute → write .npz
- [ ] Handle per-ticker expressions (LSP, algo lines) in separate ProcessPoolExecutor pass
- [ ] Handle HTF expressions using weekly/monthly OHLCV matrices + daily-to-HTF bar mapping
- [ ] Full build on Dan's machine — target: <30 min for 11,000 × 15,805
- [ ] Wire into nightly.py as the new step 3

### Phase 3: Per-bar tradable filters in grinder
- [ ] Add `--min-adr-dollars` CLI arg (default $1.00)
- [ ] Add `--min-price` CLI arg (default $1.00)
- [ ] Add `--min-dollar-vol` CLI arg (default $5,000,000)
- [ ] Per-bar ADR check in `_build_tier_batch()` after pass_mask, before surviving_indices
- [ ] Per-bar ADR check in `_scan_batch()` (signal_filter.py) same insertion point
- [ ] Per-bar close price check (same locations, from OHLCV or expression cache)
- [ ] Per-bar dollar volume check (same locations, from `dvol_20d` in expression cache)
- [ ] Thread all parameters through CLI → run_pyramid() → worker init
- [ ] Validate: all existing examples (DTSS 68, BRKO) still pass filters

### Phase 4: Nightly pipeline update
- [ ] New OHLCV pull script: yfinance direct for daily + weekly + monthly, all ~11,000 tickers
- [ ] Dormant ticker detection: skip yfinance pull for tickers with no new bars for 30+ consecutive days, retry monthly
- [ ] Wire vectorized expression cache builder into nightly.py
- [ ] Remove Railway as OHLCV source in nightly flow
- [ ] End-to-end nightly benchmark — target: total under 60 minutes

### Phase 5: Full regrind
- [ ] Full expression cache build (all 11,000 tickers, vectorized)
- [ ] Signal grind → exit grind → refinement → EV for DTSS with per-bar filters
- [ ] Same for BRKO
- [ ] Compare signal counts and contamination rates vs pre-fix
- [ ] Verify move_adr distribution is clean
- [ ] Verify examples still pass

### Phase 6: Cleanup
- [ ] Remove `tradable_universe` as a pipeline dependency everywhere
- [ ] Remove or repurpose `build_tradable.py`
- [ ] Remove Railway OHLCV pull from `cache_builder.py`
- [ ] Fix `server.py` line 1441 — stop blindly inserting into `tradable_universe`
- [ ] Update PIPELINE_V2.md, SIGNAL_GRINDER.md, REFINEMENT_GRINDER.md

---

## RISKS AND OPEN QUESTIONS

1. **Float32 precision drift.** Numpy float32 vs pandas float64→float32. Intermediate precision differs. The validation gate in Phase 2 catches this — if values don't match, compute in float64 and cast at save time (doubles RAM per batch but still fits).

2. **yfinance rate limits for nightly pull.** 11,000 tickers × 3 timeframes. yfinance supports batched multi-ticker download which helps. Fallback: only pull weekly on Fridays, monthly on 1st of month (since those bars only close then anyway).

3. **Bar count padding.** Tickers with fewer bars (recent IPOs) get NaN-padded in the matrix. This is consistent with existing NaN handling but increases memory slightly. Alternative: group by bar count. Start with padding, optimize later if needed.

4. **Weekly/monthly bar boundary differences.** yfinance weekly vs pandas resample. Documented in Phase 0 prep — differences are expected and acceptable since everything gets reground.

5. **Disk space for expanded expression cache.** ~11,000 tickers × ~5-10MB each = 55-110GB. Confirm available disk.

6. **New ticker discovery.** No mechanism exists. Not blocking for Phases 1-5 but needed for long-term completeness. Options: Nasdaq/NYSE FTP ticker lists (free, daily), or periodic yfinance screening. Separate feature.

7. **OHLCV data quality for the 6,857 "new" tickers.** These have been sitting on Railway untouched. May have gaps, bad data, or stale last dates. Need a data quality check during Phase 1.

---

## WHAT NOT TO TOUCH

- **Expression library** (`brute_expressions.py`) — the 15,805 expressions stay identical (plus the one new `dvol_20d`). No removals, no reordering. Vectorized builder must produce identical column indices.
- **Expression cache .npz format** — `dates` array + `data` array (n_bars, n_expressions). All downstream consumers read this format unchanged.
- **Grinder engine logic** — beam search, clustering, classification unchanged. Only per-bar tradable filter added to scan phase.
- **Exit grinder, EV grinder, profit grinder** — downstream consumers, untouched.
- **Railway** — stays as seed vault backup. Push-only operationally.
- **Examples** — existing examples are real setups, they pass per-bar filters.

---

## SUCCESS CRITERIA

1. Expression cache covers all ~11,000 tickers with daily + weekly + monthly HTF expressions
2. Full expression cache build completes in <30 minutes on Dan's i5-12600K
3. Nightly expression cache rebuild completes in <20 minutes
4. Total nightly pipeline completes in <60 minutes
5. BRKO signal contamination (ADR < $1.00) drops from 42.5% to 0%
6. All existing examples still pass signal conditions
7. No Railway dependency in operational pipeline (seed vault only)
8. Vectorized expression values match pandas values within float32 tolerance
