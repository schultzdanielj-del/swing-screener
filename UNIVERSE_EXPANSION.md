# Universe Expansion + Expression Cache Vectorization

**Created:** 2026-03-26
**Status:** Planning — no code changes until plan is fully vetted
**Branch:** v2

---

## THE PROBLEM

The grinder's data is bad in two directions:

1. **Contaminated signals:** 42.5% of BRKO signals (1,677 / 3,946) have ADR below $3.00. Bond ETFs (FTSM $0.025 ADR), fixed-income funds, and micro-ADR stocks produce garbage signals with inflated move_adr values (FTSM showing a "353.7 ADR move"). The `tradable_universe` table on Railway was supposed to filter these out but **never actually filtered anything** — `server.py` line 1441 inserts every ticker with OHLCV data directly into `tradable_universe` with zero checks. The `build_tradable.py` script with proper filters (price ≥ $1, APTR ≥ 1.5%, avg dollar volume ≥ $5M) was never wired into the Railway pipeline.

2. **Missing signals:** 6,857 tickers have OHLCV data on Railway but are not in `tradable_universe`. These tickers — including stocks that were liquid and volatile historically but have since delisted, faded, or fallen below current thresholds — are completely invisible to the grinder. Valid historical setups from these tickers are lost.

3. **No ticker discovery:** `append_daily` only pulls new bars for tickers already in `tradable_universe`. New IPOs, relisted tickers, or tickers that cross into tradable territory never enter the pipeline. There is no mechanism to discover new tickers.

**Nothing downstream of the expression cache (signal grind, refinement, EV, profit grind) can produce trustworthy results until this is fixed.**

---

## THE SOLUTION

Three components:

### A. Per-bar tradable filters in the grinder

Instead of a ticker-level gate, check tradability at each bar index during the grind scan. A ticker can be tradable in 2022 and untradable in 2024, or vice versa. The per-bar check is the only correct approach for historical scanning.

Filters (checked per bar from expression cache / OHLCV):
- `adr14 >= $3.00` (dollar ADR floor — catches bond ETFs, money markets, micro-ADR, low-volatility)
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

### The dvol_20d filter

Not an expression — computed on the fly from OHLCV (close × volume, 20-bar SMA) at scan time in the per-bar tradable filter. No expression slot needed. `cache_builder.py` already computes `dvol_20d` as a column in each ticker's DataFrame.

---

## EXECUTION PLAN

### Phase 0: Preparation (no code changes) ✅ COMPLETE
- [x] Check what's in local SQLite — 4,169 tickers locally vs 11,026 on Railway (6,857 gap)
- [x] Confirm RAM on Dan's machine — 32GB confirmed
- [x] Confirm disk space available for expanded expression cache — 480GB free
- [x] Verified server.py line 1441 blind insert, build_tradable.py never wired, cache_builder.py gates on tradable_universe

### Phase 1: OHLCV expansion ✅ COMPLETE
- [x] Pulled 6,857 missing tickers from Railway into local 5yr cache (178 failed — delisted/minimal data)
- [x] Local cache now: 10,856 tickers (99.5% of Railway's 11,026)
- [ ] Pull weekly OHLCV for all ~11,000 tickers from yfinance
- [ ] Pull monthly OHLCV for all ~11,000 tickers from yfinance
- [ ] Verify bar counts and date alignment across all three timeframes
- [ ] Store as three .pkl files

### Pre-Phase 2 Backups (2026-03-27)
- `local_runner/cache/expr_series_backup/` — 4,133 files, full expression cache snapshot
- `local_runner/cache/universe_ohlcv_5yr_pre_phase2.pkl` — expanded 10,856-ticker OHLCV cache
- `local_runner/cache/universe_ohlcv_5yr_backup.pkl` — pre-expansion 4,169-ticker OHLCV cache
- **Restore:** copy `expr_series_backup/` back to `expr_series/`, copy `*_pre_phase2.pkl` back to `universe_ohlcv_5yr.pkl`

### Phase 2: Vectorized expression cache builder

#### What works (correctness verified)
- [x] **Increment 1:** Numpy 2D base indicators (28 functions: SMA, EMA, HMA, ATR, ADR, RSI, ADX, DI+/-, CCI, MACD, Bollinger, OBV, BOP, Aroon, CMF, Kaufman, count_true, since_true, true_in_row). File: `vectorized_indicators.py`
- [x] **Increment 2:** Daily expression dispatcher (1,604 expressions, 86 op types). File: `vectorized_dispatch.py`
- [x] **Increment 3:** Boolean conditions + aggregates (2,413 expressions, 127 conditions). Added to `vectorized_dispatch.py`
- [x] **Increment 4:** Extension structure ops — on_series + on_series_bool_agg (1,198 expressions). Added to `vectorized_dispatch.py`
- [x] **Increment 5:** HTF weekly + monthly (10,466 expressions). No new code — same `build_intermediates` + `compute_expr_2d` on resampled OHLCV matrices.
- [x] **Validation gate:** All 5,215 non-precomputed expressions match pandas within float64 precision (tested with synthetic data, 2–3 tickers × 300 bars). HTF: 90/90 weekly + 90/90 monthly base ops match.
- [x] `vectorized_cache_builder.py` produces **correct output** — .npz files match the existing format. Correctness is not the issue.

#### What does NOT work (performance)
- The current builder is **~11 hours for 10,542 tickers** — SLOWER than the old per-ticker pandas builder (~4.5 hours).
- **Root cause:** Single-threaded Python dispatch overhead. `compute_expr_2d` is called 15,805 times per output batch (422 batches of 25 tickers). Each call costs ~18ms in Python function dispatch + numpy array allocation. The actual numpy compute is trivial.
- **CPU utilization was 8% throughout the entire 11-hour run.** The machine's 10 cores (i5-12600K) sat idle. The numpy operations are fast — Python's single-threaded interpreter is the bottleneck, not compute.
- **What was tried:** (1) Per-batch intermediates + per-expression dispatch = ~11 hours. (2) Global intermediates computed once for all 10,542 tickers (174 arrays, ~20GB), sliced per batch = still ~11 hours. The intermediates-once approach eliminated redundant computation but the per-expression dispatch loop within each batch is identical.

#### Next step: parallelize the expression loop
The expression loop is embarrassingly parallel — each expression is independent. The fix is to parallelize expression computation across CPU cores using `ProcessPoolExecutor` or `multiprocessing.Pool`. Each worker computes a subset of expressions for the current batch.

**RAM constraint:** 26.4 GB used at peak (83% of 32GB). Intermediates consume ~20GB. Each worker needs access to the intermediates (read-only) plus one output array per expression (~109MB for 10,542 × 1,297 float64). With fork-based multiprocessing, the intermediates are shared via copy-on-write. Workers only allocate the per-expression output (~109MB each).

**Estimated speedup:** i5-12600K has 10 cores (6P + 4E). At 8% single-threaded → ~80% utilization with 10 workers = ~10x theoretical. Realistic with overhead: 5–8x. Current 11 hours → **1.5–2.5 hours** for full rebuild. This is a realistic target, not the original 30-minute estimate which was wrong.

**Alternative approaches (if parallelization alone isn't enough):**
- Numba JIT: compile the expression dispatch to machine code, eliminating Python interpreter overhead entirely
- Bulk 3D ops: compute all parameter variants of an op simultaneously (e.g., all 240 ma_slope expressions in one operation). More complex but eliminates the per-expression loop
- Hybrid: parallelize + bulk the top 5 op types by count (ma_slope 240, distance_to_maxh 174, distance_to_minl 114, extension 78, spread_slope 64 = 670 expressions, ~40% of daily)

#### Backups (2026-03-27)
- `local_runner/cache/expr_series_backup/` — 4,133 files, expression cache snapshot from before this work
- `local_runner/cache/universe_ohlcv_5yr_pre_phase2.pkl` — 10,856-ticker OHLCV cache
- `local_runner/cache/universe_ohlcv_5yr_backup.pkl` — pre-expansion 4,169-ticker OHLCV cache
- **Restore:** copy `expr_series_backup/` back to `expr_series/`, copy `*_pre_phase2.pkl` back to `universe_ohlcv_5yr.pkl`

#### Files
- `local_runner/vectorized_indicators.py` — 28 numpy 2D base indicator functions (CORRECT, tested)
- `local_runner/vectorized_dispatch.py` — expression dispatcher, `build_intermediates()`, `compute_expr_2d()` (CORRECT, used for validation and as reference)
- `local_runner/vectorized_cache_builder.py` — batched pipeline (CORRECT output, needs parallelization)

### Phase 3: Per-bar tradable filters in grinder
- [ ] Add `--min-adr-dollars` CLI arg (default $3.00)
- [ ] Add `--min-price` CLI arg (default $1.00)
- [ ] Add `--min-dollar-vol` CLI arg (default $5,000,000)
- [ ] Per-bar ADR check in `_build_tier_batch()` after pass_mask, before surviving_indices
- [ ] Per-bar ADR check in `_scan_batch()` (signal_filter.py) same insertion point
- [ ] Per-bar close price check (same locations, from OHLCV or expression cache)
- [ ] Per-bar dollar volume check (same locations, computed from OHLCV close × volume)
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
5. BRKO signal contamination (ADR < $3.00) drops from 42.5% to 0%
6. All existing examples still pass signal conditions
7. No Railway dependency in operational pipeline (seed vault only)
8. Vectorized expression values match pandas values within float32 tolerance
