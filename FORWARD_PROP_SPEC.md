# Forward-Propagation Spec — Expression Cache Incremental Append

**Date:** 2026-04-02
**Status:** SPEC ONLY — no code exists for this yet
**Location in build:** Task H Phase 3, Increment 2 (replaces _compute_ticker_full inside _append_one_ticker)
**Authoritative spec:** This file. EXPRESSION_ENGINE_V2.md has the original four-file design under "Forward-Propagation Design — Four Files Per Ticker".

## Purpose of the Expression Cache (Non-Negotiable)

The expression cache must provide the ability to find a historical signal and calculate the exact daily, weekly, AND monthly expressions for all ~16K expressions. The weekly and monthly expressions on any historical day must reflect the exact partial candle state for that day — before the week/month has closed. It cannot require an increase in any grind times, and the nightly refresh needs to be fast

Any optimization that compromises the above is rejected. No exceptions.

## Problem

Current `_append_one_ticker()` calls `_compute_ticker_full()` — the full rebuild path. For ~11,200 tickers adding 1 new bar each, this takes ~124 minutes because it recomputes ALL ~1,500 bars × 15,805 expressions per ticker. Only the last row is new.

## Expression Count Clarification

The full expression library (`brute_expressions.generate_all()` + generic exit expressions) has **16,051** expressions. After filtering in `_load_expressions()` (excluding entry-relative and context-dependent exit ops), **15,805** end up in the .npz files. The audit in `validate_incremental_append.py` classified all 16,051. The .npz column count is 15,805. All file layout math in this spec uses 15,805 as the expression column count. EXPRESSION_ENGINE_V2.md decision #7 says "16,051" — that's the library count, not the cache column count.

## Solution: Four-File Forward-Propagation

Compute only the new bar's values using stored intermediate state + lookback buffers. No ExpressionEngine, no pandas, no full indicator series. Pure numpy scalar math for the vast majority of expressions.

Projected time: ~0.87s/ticker × 11,200 tickers / 14 workers ≈ ~12 minutes.

---

## What Was Tried Before (And Failed)

**CRITICAL WARNING:** After the original four-file spec was lost in chat 12, a simpler two-file design was built in chats 12-14. That design is WRONG and must not be used as reference. Specifically:

### Code that exists but does NOT implement this spec:
- `_append_one_ticker()` — currently wraps `_compute_ticker_full()`, no forward-prop
- `.append` file format — currently 15,805 columns (same as .npz), must become wider
- `load_ticker_cache()` append handling — assumes same column width, must handle wider
- `signal_filter._load_ticker_npz()` — same wrong assumption
- `scripts/validate_append_infra.py` — tests full-compute-then-extract, not forward-prop
- `scripts/benchmark_incremental_append.py` — simulates state/lookback phases using EXPRESSION VALUES instead of INTERMEDIATES. The operations it measures (EMA-like update on expression results, summing expression values for count_true) are not the operations forward-prop actually performs. Per-phase timing for state and lookback is invalid. Total timing (~0.87s) is coincidentally correct because HTF (0.13s) and LSP+algo (0.64s) were measured with real computation and dominate the total.

### What IS trustworthy from those sessions:
- `scripts/validate_incremental_append.py` — the expression audit/classification (1,177 state, 4,284 lookback, 10,466 HTF, 80 LSP, 44 algo). This ran real analysis on the expression library. ✅
- Immutability gate — confirmed that bar N-1 produces identical values regardless of how many bars follow. ✅
- Per-ticker timing for HTF (~0.13s) and LSP+algo (~0.64s) — these ran real computation. ✅
- The projected total (~12 min for 11,200 tickers / 14 workers) — correct despite broken per-phase breakdown. ✅

---

## File Layout — Four Files Per Ticker

### 1. `{T}.npz` — Frozen base (READ-ONLY after full rebuild)
- Existing compressed file from last full rebuild
- Contains `data` (float16, n_bars × 15,805) and `dates` (string array)
- **Never modified by nightly append.** Frozen until next full rebuild.
- Written only by `build_full()` which also deletes all .append/.lookback/.state/.append_dates files.

### 2. `{T}.append` — New expression rows (WIDER than .npz)
- Raw float16 binary, no compression, no headers
- Each row: **15,805 expression columns + N_INTERMEDIATES intermediate columns**
- `load_ticker_cache()` reads this and returns ONLY the first 15,805 columns to consumers
- The append worker reads ALL columns (expressions + intermediates) for its forward computation
- File grows by one row (~32 KB) per trading day
- To read: `np.frombuffer(data, dtype=np.float16).reshape(-1, 15805 + N_INTERMEDIATES)`

### 3. `{T}.lookback` — Intermediate history from .npz era
- Raw float16 binary, fixed size: 504 rows × N_INTERMEDIATES columns
- Contains the last 504 rows of intermediate column values from the tail of the base .npz
- Written once during one-time setup, then sliding-window updated nightly
- Provides the append worker with lookback into intermediate values for rows that are in the .npz (which only has 15,805 expression columns, no intermediates)
- As appends accumulate and the lookback window shifts into .append territory, .lookback becomes redundant for those rows. After 504 trading days (~2 years), .lookback is fully superseded by .append's intermediate columns.
- Size per ticker: 504 × N_INTERMEDIATES × 2 bytes ≈ ~200 KB
- Total: ~2.2 GB

### 4. `{T}.state` — Forward computation state (OVERWRITTEN nightly)
- Float64 values for intermediate state that is NOT historical (not an array across bars)
- Contains: EMA states, ADX chain, MACD signal lines, rolling max/min tracking indices, HTF partial candle accumulators, stochastic raw_k history, OBV, extension-structure on_series EMA states
- Does NOT contain LSP or algo state (those run full scan every time)
- Size per ticker: ~2-3 KB
- Total: ~30 MB

### 5. `{T}.append_dates` — Date strings for appended rows
- One date string per line, appended nightly
- Existing format, unchanged

---

## Intermediate Columns — What Goes In .append and .lookback

The append worker needs access to historical values of technical INTERMEDIATES — not expression values — to compute the next bar. These intermediates are the raw indicator values, cumulative sums, and source arrays that the expression formulas operate on.

### From build_numpy_intermediates() — 178 columns

These are the same intermediates that the full rebuild extracts from ExpressionEngine. Complete list:

| Category | Keys | Count |
|----------|------|-------|
| SMA close | avgc{p} for p in [5,8,10,13,20,21,30,50,65,100,150,200] | 12 |
| EMA close | xavgc{p} for p in [5,8,9,10,12,13,20,21,30,50,65,100,150,200] | 14 |
| SMA volume | avgv{p} for p in [10,20,50] | 3 |
| Base OHLCV+derived | close, open, high, low, volume, atr14, adr14, pct | 8 |
| Rolling max high | maxh{p} for 29 periods: [2,3,5,7,10,15,20,25,30,35,40,45,50,55,60,63,65,70,75,80,85,90,95,100,105,110,115,120,126] | 29 |
| Rolling min low | minl{p} for 19 periods: [2,3,5,7,10,15,20,25,30,35,40,45,50,55,60,65,90,120,126] | 19 |
| RSI | rsi{p} for p in [5,7,9,14,21,28] | 6 |
| ADX chain | adx{p}, diplus{p}, diminus{p} for p in [7,10,14,20] | 12 |
| Stochastic | stoch{p} for p in [3,5,7,9,10,14,21,28,50] | 9 |
| CCI | cci{p} for p in [5,7,10,14,20,30,50] | 7 |
| BOP | bop{p} for p in [5,10,14,20] | 4 |
| OBV | obv | 1 |
| MACD | macd_{fast}_{slow} for 5 pairs: (12,26),(8,17),(5,35),(5,13),(6,19) | 5 |
| Bollinger | bbtop_{p}, bbbot_{p}, stddev_{p} for p in [5,10,15,20,30,50] | 18 |
| Aroon | aroon_up_{p}, aroon_down_{p} for p in [7,10,14,20,25,50,100] | 14 |
| CMF | cmf_{p} for p in [10,14,20,30,50] | 5 |
| Kaufman | kauf_eff_{p} for p in [5,7,10,15,20,30,50,65,100] | 9 |
| Rolling max close | maxc{p} for p in [10,20,50] | 3 |
| **TOTAL** | | **178** |

### Additional columns for forward-propagation — computed during setup and maintained during append

These are values that the full rebuild computes internally (inside ExpressionEngine or extract_closed_state) but doesn't expose as intermediates. Forward-prop needs them stored explicitly.

| Category | Keys | Count | Purpose |
|----------|------|-------|---------|
| Cumulative sums | cumsum_close, cumsum_volume, cumsum_hl, cumsum_tr, cumsum_bop_raw, cumsum_mfv, cumsum_abs_diff, cumsum_tp, cumsum_c2, cumsum_gains, cumsum_losses | 11 | SMA forward-prop: avgc50[i] = (cumsum[i] - cumsum[i-50]) / 50. Read cumsum[i-50] from .lookback/.append. Also for Bollinger variance, CMF, Kaufman, RSI. |
| Raw per-bar values | true_range, gains, losses, tp, bop_raw, mfv, abs_diff | 7 | Needed for CCI (mean deviation over tp window), RSI (gains window for cumsum validation), etc. These are the raw values before cumsum/smoothing. |
| **TOTAL additional** | | **18** |

### Grand total: N_INTERMEDIATES = 196 columns

Each .append row: 15,805 + 196 = 16,001 values × 2 bytes (float16) = **32,002 bytes ≈ 32 KB/row**

Each .lookback file: 504 × 196 × 2 bytes = **197,568 bytes ≈ 193 KB**

---

## State File Contents

Values stored at float64 precision. These are scalars (not historical arrays) — they represent the "current state" of running computations.

### Daily state

| Category | Values | Count | Forward-prop formula |
|----------|--------|-------|---------------------|
| EMA close | xavgc{p} for 14 periods | 14 | xavgc[i] = alpha * close + (1-alpha) * xavgc[i-1] |
| MACD signal | macd_signal_{fast}_{slow} for 4 configs: (12,26,9),(8,17,9),(5,13,8),(6,19,9) | 4 | signal[i] = alpha * macd_line + (1-alpha) * signal[i-1]. Note: (5,35) MACD line exists as intermediate but has no signal line expression. |
| ADX EMA chain | ema_dmp_{p}, ema_dmm_{p}, ema_dx_{p} for p in [7,10,14,20] | 12 | Standard EMA update. ADX(P) uses ATR(P) for normalization, NOT always ATR(14). |
| Rolling max/min idx | maxh_idx_{p} (29), minl_idx_{p} (19), maxc_idx_{p} (3) | 51 | Track bar index of current max/min. If new value beats it, update. If old index exits window, rescan from lookback/append. |
| Aroon tracking | aroon_maxh_idx_{p}, aroon_minl_idx_{p} for 7 periods | 14 | Same as rolling max/min tracking |
| Stochastic raw_k | raw_k_{p}_prev1, raw_k_{p}_prev2 for 9 periods | 18 | stoch = SMA(3) of raw_k. Need 2 prior raw_k values. raw_k computed from rolling_max(high,p)/rolling_min(low,p) directly — stoch periods [3,9] are NOT in the maxh/minl intermediates. |
| OBV | obv_prev | 1 | obv[i] = obv[i-1] + sign(close_change) * volume |
| Yesterday's OHLCV | prev_close, prev_high, prev_low | 3 | For true range calc and DM+/DM- |
| Current bar index | bar_index | 1 | Absolute bar number since EXPR_CACHE_START for rolling max/min window tracking |
| Cumsums | cumsum_close, cumsum_volume, cumsum_hl, cumsum_tr, cumsum_bop_raw, cumsum_mfv, cumsum_abs_diff, cumsum_tp, cumsum_c2, cumsum_gains, cumsum_losses | 11 | Running totals for SMA forward-prop via cumsum[i] - cumsum[i-P] / P |
| **Daily subtotal** | | **129** |

### HTF state (per timeframe — weekly + monthly = ×2)

| Category | Values per TF | Count ×2 | Notes |
|----------|--------------|----------|-------|
| Partial candle OHLCV | open, high, low, close, volume | 10 | Updated daily: high=max, low=min, close=today, vol+=today |
| Period ID | period_id | 2 | year*100 + week_number (or month). Detect boundary. |
| EMA close | xavgc{p} for 14 periods | 28 | HTF EMA state for partial candle engine |
| ADX EMA chain | ema_dmp, ema_dmm, ema_dx for 4 periods | 24 | Same |
| MACD signal | macd_signal for 4 configs | 8 | Same |
| OBV | obv | 2 | Same |
| Cumsums | cumsum_close, cumsum_volume, cumsum_hl, cumsum_tr, cumsum_bop_raw, cumsum_mfv, cumsum_abs_diff, cumsum_tp, cumsum_c2, cumsum_gains, cumsum_losses | 22 | For HTF SMA forward-prop |
| Stochastic raw_k | raw_k prev1, prev2 for 9 periods | 36 | For HTF stochastic smoothing |
| Previous HTF bar | prev_high, prev_low, prev_close | 6 | For HTF TR and DM+/DM- on period boundaries |
| **HTF subtotal** | | **138** |

### Extension structure on_series state (per extension series — 2 series = ×2)

The two driving series are `ext_avgc50_adr14` and `ext_avgc200_adr14`. The on_series ops compute indicators on these series. CRITICAL: on_series RSI uses EMA smoothing (ewm), NOT SMA smoothing like daily RSI.

| Category | Values per series | Count ×2 | Notes |
|----------|------------------|----------|-------|
| EMA avg_gain/loss for RSI | 6 periods [5,7,9,14,21,28] × 2 (avg_gain + avg_loss) = 12 | 24 | on_series RSI uses ewm(span=p, adjust=False). Confirmed in backtest_conditions.compute_on_series(). |
| ADX EMA chain | 4 periods [7,10,14,20] × 3 (ema_dmp + ema_dmm + ema_dx) = 12 | 24 | on_series ADX uses ewm for all internal EMAs |
| Previous extension value | prev_ext | 2 | For delta/diff calculations |
| **Ext struct subtotal** | | **50** |

**RESOLVED (2026-04-02):** Scanned `brute_expressions.generate_all()` — on_series RSI uses periods [5,7,9,14,21,28], on_series ADX uses periods [7,10,14,20]. These match daily RSI/ADX periods but use EMA smoothing (ewm) not SMA.

### LSP + Algo: NO STATE, NO FORWARD-PROP

The LSP detector (`lsp_detector_v2.py`) and algo line detector (`algo_line_detector.py`) scan the FULL daily OHLCV history every time. They cannot be forward-propagated because:

- **LSP** detects pivots with lag (a pivot at bar X with window 40 is confirmed 40 bars later). Adding bar N+1 can form new pivots at bar N+1-window. Break counts are precomputed across the full series using cumulative break arrays. `get_levels_at_bar()` clusters all active pivots and ranks by proximity — the ranking changes as new pivots form and existing ones get broken.
- **Algo lines** use O(n²) pair scanning for trendline origination from high-volume bars. A new bar can extend lines, break them, create new origination points, or change touch counts. The violation checking is vectorized across the full series.
- Neither detector exposes internal state in a serializable/resumable form. Rewriting them for forward-prop would be a major effort with high regression risk.

**Decision: LSP + algo run `compute_all_lsp_series(df)` and `compute_all_algo_series(df)` on the full daily OHLCV every append. Extract last-bar values for 80 + 44 expression columns. This is ~0.64s/ticker — 73% of total append cost. It is the performance floor.**

The .state file does NOT store LSP or algo state.

### Total .state: 129 daily + 138 HTF + 50 ext_struct = **317 values** ≈ ~2.5 KB per ticker, ~28 MB total

**Validated (2026-04-02):** `setup_forward_prop.py` produces exactly 317 state keys per ticker. Confirmed on AAPL (single) and 100 random tickers (0 failures, 0.9s/ticker).

---

## Forward-Propagation Engine — Phase by Phase

This is the algorithm that replaces `_compute_ticker_full()` inside `_append_one_ticker()`.

### Inputs per ticker
1. Today's daily OHLCV (O, H, L, C, V, date) — from daily pickle
2. `.state` file — loaded as structured dict
3. `.lookback` file — 504 × 196 float16 array → float32
4. `.append` file — all prior appended rows with 16,001 columns → float32. If no prior appends, empty.
5. `.npz` tail — last 1,260 rows of expression data (15,805 cols). Needed ONLY for 10 ext_ceiling_ratio expressions that need 1,260-bar rolling max of expression values. Loaded once per ticker.
6. Full daily OHLCV DataFrame — needed for LSP + algo detectors only
7. Weekly/monthly HTF DataFrames — from HTF pickles, already updated in nightly steps 3-4

### Helper: Reading historical intermediate values

The worker needs to read intermediate column values at historical bars. The source depends on where that bar falls:

- If bar is in .lookback range (last 504 rows of .npz era): read from .lookback array
- If bar is in .append range (rows after .npz): read from .append's intermediate columns
- Row addressing: bar_index in state file tracks the absolute bar number. .lookback covers bars [npz_end - 503, npz_end]. .append covers bars [npz_end + 1, ...].

### Phase 1: Compute daily intermediates (196 values)

For each intermediate, compute today's value using the appropriate forward-propagation formula:

**SMA intermediates (avgc, avgv, atr14, adr14, bop, cmf):**
```
cumsum[i] = state.cumsum + today_value
cumsum[i-P] = read_intermediate("cumsum_X", bar_index - P)  // from .lookback or .append
sma[i] = (cumsum[i] - cumsum[i-P]) / P
```

**EMA intermediates (xavgc):**
```
alpha = 2 / (P + 1)
xavgc[i] = alpha * today_close + (1 - alpha) * state.xavgc{P}
```
Pure state update, no lookback needed.

**RSI (SMA-based on daily — profiling_engine.rsi uses SMA of gains/losses):**
```
gain = max(0, today_close - state.prev_close)
loss = max(0, state.prev_close - today_close)
cumsum_gains[i] = state.cumsum_gains + gain
cumsum_losses[i] = state.cumsum_losses + loss
cumsum_gains[i-P] = read_intermediate("cumsum_gains", bar_index - P)
cumsum_losses[i-P] = read_intermediate("cumsum_losses", bar_index - P)
avg_gain = (cumsum_gains[i] - cumsum_gains[i-P]) / P
avg_loss = (cumsum_losses[i] - cumsum_losses[i-P]) / P
rsi[i] = 100 - 100 / (1 + avg_gain / avg_loss)
```

**ADX chain (EMA-based — profiling_engine uses ema for DM smoothing and ADX smoothing):**
```
up = today_high - state.prev_high
down = state.prev_low - today_low
dm_plus = up if (up > down and up > 0) else 0
dm_minus = down if (down > up and down > 0) else 0
alpha = 2 / (P + 1)
ema_dmp[i] = alpha * dm_plus + (1 - alpha) * state.ema_dmp{P}
ema_dmm[i] = alpha * dm_minus + (1 - alpha) * state.ema_dmm{P}
// NOTE: ADX(P) uses ATR(P) for normalization — period-matched, NOT always ATR(14).
// profiling_engine._di_plus_minus calls atr(df, period). ATR is SMA-based.
// ATR(P) = (cumsum_tr[i] - cumsum_tr[i-P]) / P — computed from cumsums.
atr_p = (cumsum_tr[i] - cumsum_tr[i-P]) / P
di_plus = 100 * ema_dmp / atr_p
di_minus = 100 * ema_dmm / atr_p
dx = |di_plus - di_minus| / (di_plus + di_minus) * 100
adx[i] = alpha * dx + (1 - alpha) * state.ema_dx{P}
```

**Stochastic (raw_k computed from rolling max/min, NOT from maxh/minl intermediates):**
```
// Stoch periods [3,9] do NOT exist in maxh/minl intermediate columns.
// Must compute rolling_max(high, P) and rolling_min(low, P) directly.
maxh_p = rolling_max(high, P)  // from .lookback/.append H values
minl_p = rolling_min(low, P)   // from .lookback/.append L values
raw_k = (today_close - minl_p) / (maxh_p - minl_p) * 100
stoch[i] = (state.raw_k_prev2 + state.raw_k_prev1 + raw_k) / 3
// Update state: prev2 = prev1, prev1 = raw_k
```

**Rolling max/min (maxh, minl, maxc):**
```
if today_high >= maxh[i-1]:
    maxh[i] = today_high
    state.maxh_idx{P} = bar_index
elif state.maxh_idx{P} >= bar_index - P + 1:
    // Current max still in window
    maxh[i] = read_intermediate("high", state.maxh_idx{P})
else:
    // Old max fell out of window — rescan
    // Read high values for last P bars from .lookback/.append
    // Find new max and its index
    maxh[i] = max of window
    state.maxh_idx{P} = index of max
```
Same logic for minl (using low) and maxc (using close).

**CCI (window-based — needs raw tp values):**
```
tp_today = (today_high + today_low + today_close) / 3
// Read last P-1 tp values from .lookback/.append intermediate columns
window = [tp at bar_index-P+1, ..., tp at bar_index-1, tp_today]
tp_sma = mean(window)
mean_dev = mean(|window - tp_sma|)
cci[i] = (tp_today - tp_sma) / (0.015 * mean_dev)
```

**Aroon (window-based — needs H/L positions):**
```
// Read last P high values from .lookback/.append intermediate columns
window_h = [high at bar_index-P+1, ..., today_high]
bars_since_max = P - 1 - argmax(window_h)
aroon_up[i] = (P - bars_since_max) / P * 100
// Same for aroon_down with low values
```

**Bollinger (cumsum-based):**
```
cumsum_c2[i] = state.cumsum_c2 + today_close^2
cumsum_c2[i-P] = read_intermediate("cumsum_c2", bar_index - P)
cumsum_c[i-P] = read_intermediate("cumsum_close", bar_index - P)
sum_sq = cumsum_c2[i] - cumsum_c2[i-P]
sum_c = cumsum_close[i] - cumsum_c[i-P]
mean_sq = (sum_sq + today_close^2 is already in sum_sq) ... 

Actually: variance = (sum_sq - sum_c^2 / P) / (P - 1)   // ddof=1, matches pandas .std()
stddev = sqrt(max(0, variance))
bbtop = avgc{P} + 2 * stddev
bbbot = avgc{P} - 2 * stddev
```
NOTE: **RESOLVED (2026-04-02):** `profiling_engine.stddev()` uses `pandas .rolling().std()` which defaults to ddof=1 (sample standard deviation). Forward-prop must use ddof=1 to match: `variance = (sum_sq - sum²/P) / (P - 1)`. The cumsum formula `sum_sq/P - mean²` is population variance (ddof=0) and would NOT match — must use the sample variance formula.

**OBV:**
```
sign = sign(today_close - state.prev_close)
obv[i] = state.obv_prev + sign * today_volume
```

**MACD:**
```
macd_line = xavgc{fast} - xavgc{slow}  // already computed as EMA intermediates
alpha9 = 2 / 10  // signal line is EMA(9) of MACD
macd_signal = alpha9 * macd_line + (1 - alpha9) * state.macd_signal
macd_histogram = macd_line - macd_signal
```

**Kaufman efficiency:**
```
cumsum_abs_diff[i] = state.cumsum_abs_diff + |today_close - state.prev_close|
close_P_bars_ago = read_intermediate("close", bar_index - P)
direction = |today_close - close_P_bars_ago|
volatility_sum = cumsum_abs_diff[i] - read_intermediate("cumsum_abs_diff", bar_index - P)
kauf_eff[i] = direction / volatility_sum
```

**CMF:**
```
hl = today_high - today_low
mfm = ((today_close - today_low) - (today_high - today_close)) / hl
mfv_today = mfm * today_volume
cumsum_mfv[i] = state.cumsum_mfv + mfv_today
// CMF = sum(MFV, P) / sum(V, P) = cumsum-based
sum_mfv = cumsum_mfv[i] - read_intermediate("cumsum_mfv", bar_index - P)
sum_vol = cumsum_volume[i] - read_intermediate("cumsum_volume", bar_index - P)
cmf[i] = sum_mfv / sum_vol
```

**All other intermediates** follow similar patterns — either cumsum-based, EMA-based, or window-based.

### Phase 2: Compute daily expression values (15,805 columns)

With all 196 intermediates computed, dispatch each expression using the same logic as `dispatch_arith_numpy` but scalar (single values instead of arrays).

**Categories and how they dispatch:**

**Dispatch ops (~1,300 — extension, ma_slope, ma_spread, distance_to_maxh, ratio_c_maxh, rsi, adx, stochastic, cci, bop, range_position, pullback, range_width, channel_slope, candle_range_ratio, body_range_ratio, etc.):**
- Direct reads from the 196 intermediates computed in Phase 1
- Example: `extension(close, avgc50, atr14) = (close - avgc50) / atr14`
- Example: `ma_slope(avgc50, offset=5, atr14) = (avgc50[i] - avgc50[i-5]) / atr14[i]`
  - avgc50[i-5] read from .lookback/.append intermediate columns

**SLOW_OPS (percentile_rank, swing counts, bars_since_ma_cross, roc_percentile_rank):**
- percentile_rank: read last P values of source (close, volume, range, atr14, rsi14) from intermediate columns. Count how many <= current. P values max = 252 reads.
- swing_high_count/low_count: read H and L for last P bars from intermediates. Identify swing points and count. Window scan.
- higher/lower_high/low_count: same data, find consecutive higher/lower swings. Window scan.
- bars_since_ma_cross: check if close crossed MA today. If same side as yesterday: prev expression value + 1. If crossed: 1. Read prev expression value from last .append row or .npz tail.
- roc_percentile_rank: compute ROC from close intermediates, then percentile_rank on that.

**Fallback ops (~459 ops that go through compute_series in full rebuild):**
These are ops like gap_size, gap_count, unfilled_gap_up_count, consecutive_up/down_days/roc, inside_bar_count, outside_bar_count, nr_ratio, up_volume_ratio, retracement_level, close_vs_open_ratio, avg_candle_body_ratio, high_volume_bar_pct, ma_cross_count, ma_undercut_depth, volume_price_divergence, etc.

Each needs its own forward-prop formula. Most are either:
- Pure OHLCV math (today's bar only) — trivial
- Window scans over OHLCV (last P bars) — read from intermediates
- Derived from intermediates (ma_cross_count needs close + MA values for P bars)

The build session must handle each individually. Many can use the "if same condition as yesterday, increment/carry; if changed, reset" pattern.

**Boolean aggregates (count_true, since_true, true_in_row — 2,413 expressions):**
```
count_true(condition, period):
    // Evaluate condition at today's bar from intermediates
    bool_today = evaluate_bool(condition, intermediates)
    // Read the value of the same condition P bars ago
    // The condition depends on intermediate values at that bar
    // Read the intermediate value at bar_index - P from .lookback/.append
    // Evaluate the condition on that historical intermediate value
    bool_dropping = evaluate_bool(condition, intermediates_at(bar_index - P))
    count[i] = prev_count - bool_dropping + bool_today

since_true(condition, period):
    bool_today = evaluate_bool(condition, intermediates)
    if bool_today: result = 0
    else: result = prev_since + 1  (cap at period → -1)

true_in_row(condition, period):
    bool_today = evaluate_bool(condition, intermediates)
    if bool_today: result = prev_tir + 1
    else: result = 0
```

The boolean conditions (127 unique) are all comparisons between intermediates: `rsi14 > 50`, `close > xavgc21`, `high > maxh10.shift(1)`, etc. Every operand is either today's intermediate value or a shifted intermediate value (readable from .lookback/.append).

### Phase 3: HTF expressions (10,466 — 5,233 weekly + 5,233 monthly)

For each timeframe (weekly, monthly):

**Step 3a: Period boundary detection**
```
today_period_id = year*100 + week_number  // or year*100 + month
if today_period_id != state.htf_period_id:
    // NEW PERIOD — previous partial candle is now closed
    // Roll forward ALL HTF closed intermediates using the completed candle
    // This is one EMA/cumsum/rolling update per intermediate
    // Then start new partial: open=O, high=H, low=L, close=C, vol=V
    state.htf_period_id = today_period_id
else:
    // SAME PERIOD — update partial candle
    state.htf_partial_high = max(state.htf_partial_high, today_high)
    state.htf_partial_low = min(state.htf_partial_low, today_low)
    state.htf_partial_close = today_close
    state.htf_partial_volume += today_volume
```

**Step 3b: Compute HTF intermediates**
Same formulas as Phase 1 but operating on HTF state (HTF EMA states, HTF cumsums, etc.) and the partial candle values. Uses the `_partial_sma`, `_partial_ema` formulas from partial_candle_engine.py but scalar.

**Step 3c: Dispatch HTF expressions**
Same as Phase 2 dispatch but using HTF intermediates. Each HTF expression has a `base_compute` dict that maps to the same ops (extension, ma_slope, rsi, etc.).

**CRITICAL: The partial candle engine in full rebuild computes at DAILY resolution** (one HTF value per daily bar). Forward-prop computes ONE value (today's). The formulas must produce identical results. The partial candle engine's `_partial_sma`, `_partial_ema`, `_partial_rolling_max/min` are the reference implementations.

**HTF lookback:** Some HTF expressions need lookback into HTF history (ma_slope with offset, extension_ceiling_ratio, etc.). These need closed HTF intermediate values at (lci - offset). The closed HTF intermediates are in the HTF pickle (weekly/monthly DataFrames). The append worker loads these and builds an ExpressionEngine on the closed series for lookback — same as the partial candle engine does in full rebuild. This is the 0.13s/ticker cost.

### Phase 4: Extension structure (1,198 on_series / on_series_bool_agg)

After Phase 2 computes the two base extension series values:
- ext_avgc50_adr14 = (close - avgc50) / adr14
- ext_avgc200_adr14 = (close - avgc200) / adr14

Use these as "price" input to on_series ops.

**CRITICAL DIFFERENCE: on_series RSI uses EMA smoothing (`ewm(span=p, adjust=False)`), not SMA smoothing. on_series ADX also uses EMA for all internal calculations. This is different from daily RSI (SMA-based, profiling_engine.rsi) and daily ADX (EMA-based, profiling_engine.adx). The forward-prop state must track separate EMA states for on_series RSI.**

**EMA-based on_series ops (rsi, rsi_slope, adx, adx_slope):**
```
// For on_series RSI on ext_avgc50_adr14:
ext_val = ext_avgc50_adr14 at today's bar (from Phase 2)
ext_prev = state.ext_prev_avgc50
delta = ext_val - ext_prev
gain = max(0, delta)
loss = max(0, -delta)
// EMA smoothing (NOT SMA):
alpha = 2 / (P + 1)
ema_avg_gain = alpha * gain + (1 - alpha) * state.ext_ema_avg_gain{P}
ema_avg_loss = alpha * loss + (1 - alpha) * state.ext_ema_avg_loss{P}
rs = ema_avg_gain / ema_avg_loss
on_series_rsi = 100 - 100 / (1 + rs)
```

**Window-based on_series ops (trendline_deviation, channel_position, stochastic, cci, range_position, pullback, floor_ratio, peak_ratio, ceiling_ratio):**
- Read the extension series history from the expression columns in .npz + .append
- Example: trendline_deviation needs last P values of the extension series
- The extension series IS an expression column (one of the 15,805), readable from .npz tail or prior .append rows

**on_series_bool_agg (count_true, since_true, true_in_row on on_series indicators):**
- Same forward-prop as daily boolean aggregates but the condition is evaluated on the on_series indicator value

### Phase 5: LSP + Algo (124 expressions)

No forward-propagation shortcut exists. These scan the full price history for structural patterns.

```
from scripts.lsp_detector_v2 import compute_all_lsp_series
from scripts.algo_line_detector import compute_all_algo_series

lsp_dict = compute_all_lsp_series(daily_ohlcv_df)
algo_dict = compute_all_algo_series(daily_ohlcv_df)
// Extract last-bar values for 80 + 44 expression columns
```

This is ~0.64s/ticker — 73% of total append cost. It's the performance floor.

### Phase 6: Save

1. Build expression row: 15,805 values from Phases 2-5
2. Build intermediate row: 196 values from Phase 1
3. Concatenate: full row = [expressions | intermediates] = 16,001 values
4. Cast to float16, append raw binary to .append file
5. Append date string to .append_dates file
6. Update .lookback: drop oldest row, append new intermediate-only row (sliding window)
7. Overwrite .state with all updated state values

---

## Consumer Changes

### load_ticker_cache(ticker) — MUST UPDATE

```
Current: reads .append assuming same width as .npz (15,805)
New: reads .append which is wider (16,001), slices to first 15,805 columns

Change:
  1. Load base .npz → data_base (n_base, 15805)
  2. If .append exists:
     a. total_cols = 15805 + N_INTERMEDIATES  // 16,001
     b. row_bytes = total_cols * 2  // float16
     c. Read raw binary, reshape to (n_appended, total_cols)
     d. Slice: appended_exprs = appended[:, :15805]
     e. Cast to float32
  3. If .append_dates exists: read date strings
  4. vstack(data_base, appended_exprs), concat(dates_base, dates_append)
  5. Return (dates, data) — shape (n_total, 15805), float32
```

### signal_filter._load_ticker_npz(ticker) — MUST UPDATE IN LOCKSTEP

Exact same change as load_ticker_cache. This function mirrors the load logic independently.

### build_full() — MUST UPDATE

After full rebuild:
- Delete all .append, .append_dates, .lookback, .state files
- (Already deletes .append and .append_dates; add .lookback and .state)

### All other consumers — NO CHANGES

ExprSeriesCache.get_ticker() calls load_ticker_cache(). Transparent.

---

## One-Time Setup Script — IMPLEMENTED

**File:** `scripts/setup_forward_prop.py`
**Status:** Validated 2026-04-02 — 100/100 random tickers, 0 failures, 0.9s/ticker

Generates .lookback and .state files from existing .npz + OHLCV data.

**Usage:**
```
python scripts/setup_forward_prop.py --ticker AAPL       # single ticker test
python scripts/setup_forward_prop.py --limit 100          # 100 random tickers (seed=42)
python scripts/setup_forward_prop.py                      # all ~11,200 tickers
python scripts/setup_forward_prop.py --workers 8          # custom worker count
```

For each ticker:

1. Load .npz → get bar count. Load daily OHLCV, **truncate to .npz bar count** (OHLCV may have bars appended since last full rebuild)
2. Build ExpressionEngine on truncated OHLCV
3. Call `build_numpy_intermediates(engine)` → 178 intermediate arrays
4. Compute 18 additional intermediate columns (11 cumsums + 7 raw per-bar values) from OHLCV
5. Build .lookback: last 504 rows of all 196 intermediate columns, cast to float16, write raw binary
6. Build .state (317 keys, float64 JSON):
   a. Daily: 14 EMA, 4 MACD signal, 12 ADX chain, 51 rolling max/min idx, 14 Aroon, 18 stochastic raw_k, 1 OBV, 3 prev OHLCV, 1 bar_index, 11 cumsums
   b. HTF (×2): partial candle OHLCV + period_id from HTF pickle, closed intermediate states (EMA, ADX, MACD signal, OBV, cumsums, stochastic raw_k, prev bar) from HTF pickle's closed bars
   c. Ext struct (×2): on_series RSI EMA pairs (6 periods) + ADX chain (4 periods) + prev_ext from .npz expression columns
7. Validate: check .lookback dimensions, .state key count, spot-check for inf/NaN

**Known behaviors:**
- .lookback cumsum columns overflow to inf in float16 for tickers with many bars — expected, forward-prop reads cumsums from .state at float64
- Short-history tickers may have all-NaN columns in .lookback for indicators needing long warmup (e.g., SMA200)
- Stochastic raw_k computed from `rolling_max(high,p)` / `rolling_min(low,p)` directly — stoch periods [3,9] do NOT exist in the pre-built maxh/minl intermediates

**Measured timing:** 0.9s/ticker × 11,200 / 14 workers ≈ **12 minutes**. One-time cost.

---

## Validation Strategy

### Gate 1: Single-ticker correctness (sandbox with 1 ticker)
- Run `_compute_ticker_full` on N bars → "truth" row at bar N (all 15,805 values)
- Run `_compute_ticker_full` on N-1 bars → generate .npz for N-1 bars
- Run one-time setup on the N-1 .npz → .lookback + .state
- Run forward-prop for bar N → "test" row (15,805 values)
- Compare truth vs test after float16 round-trip
- PASS criteria: zero mismatches beyond float16 precision

### Gate 2: 100-ticker correctness (sandbox with 100 tickers)
- Same as Gate 1 but across 100 randomly sampled tickers
- Report: number of mismatches per expression, per ticker
- PASS criteria: zero mismatches

### Gate 3: Signal filter regression
- Run signal filter on cache with .append files present
- Compare found signals against baseline (cache without .append files)
- PASS criteria: identical signal set

### Gate 4: Grind time impact
- Time `load_ticker_cache` for 100 tickers with and without .append files
- PASS criteria: no measurable slowdown (vstack + slice adds microseconds)

### Gate 5: Audit
- Run audit.py on the push
- Check all downstream consumers (per DEPENDENCY_MAP.md)
- Verify load_ticker_cache and signal_filter._load_ticker_npz are updated in lockstep

---

## Open Questions — All Resolved

1. **Bollinger stddev precision: RESOLVED (2026-04-02).** `profiling_engine.stddev()` uses `pandas .rolling().std()` → ddof=1 (sample std). Forward-prop cumsum formula must use `variance = (sum_sq - sum²/P) / (P - 1)` to match. The naive `sum_sq/P - mean²` is population variance and would mismatch.

2. **Exact on_series RSI/ADX periods: RESOLVED (2026-04-02).** Scanned `brute_expressions.generate_all()`. On-series RSI uses periods [5,7,9,14,21,28] with EMA smoothing (ewm(span=p, adjust=False)), confirmed in `backtest_conditions.compute_on_series()`. On-series ADX uses periods [7,10,14,20]. Per extension series: 12 RSI EMA pairs + 12 ADX chain + 1 prev_ext = 25 values × 2 series = **50 total ext_struct state values**.

3. **LSP/algo: RESOLVED.** Full scan confirmed. 0.64s/ticker performance floor.

4. **New tickers: RESOLVED.** Full compute via `_compute_and_save_ticker` → .npz. No .lookback/.state needed.

5. **Expression library changes: RESOLVED.** Fingerprint mismatch → full rebuild. build_full() clears all .append/.lookback/.state files.

6. **Truncation consistency: RESOLVED.** `append_new_bars()` must truncate to EXPR_CACHE_START.

7. **Period boundary edge case for HTF: RESOLVED.** Period ID comparison (ISO week/month) handles holiday-shortened weeks correctly. Verify during Gate 1.

---

## Build Order

1. ~~**Resolve open question #1**~~ — DONE: Bollinger uses ddof=1 (sample std)
2. ~~**Resolve open question #2**~~ — DONE: on_series RSI [5,7,9,14,21,28], ADX [7,10,14,20], 50 total ext_struct state
3. ~~**Build one-time setup script**~~ — DONE: `scripts/setup_forward_prop.py`, validated 100/100 random tickers, 0.9s/ticker
4. **Build forward-prop engine** — `_forward_prop_one_ticker()`, one phase at a time ← NEXT
5. **Gate 1** — single-ticker correctness test (script on 1 ticker)
6. **Gate 2** — 100-ticker correctness test (script on 100 tickers)
7. **Update load_ticker_cache + signal_filter._load_ticker_npz** — wider .append handling (16,001 cols → slice to 15,805)
8. **Update build_full()** — clear .lookback/.state files
9. **Push to repo**
10. **Dan runs audit.py** on the push
11. **Dan runs signal filter** locally → Gate 3
12. **Dan runs test grind** → Gate 4
13. **One fresh full rebuild** — correct HTF (no look-ahead bias), then setup_forward_prop.py to bootstrap .lookback/.state

---

## File Size Budget

| File | Per ticker | Total (~11,200) | Growth |
|------|-----------|-----------------|--------|
| .npz | ~10 MB | 111 GB | None (frozen) |
| .append | ~32 KB/day | ~350 MB/month | Linear |
| .lookback | ~193 KB | ~2.1 GB | None (fixed, sliding) |
| .state | ~3 KB | ~34 MB | None (overwritten) |
| .append_dates | ~11 bytes/day | tiny | Linear |
| **Total at setup** | | **~113 GB** | |
| **After 1 year** | | **~117 GB** | |

Consolidation (optional, quarterly): merge .npz + .append → new .npz. Regenerate .lookback/.state. Delete .append. ~40 min. Resets growth.
