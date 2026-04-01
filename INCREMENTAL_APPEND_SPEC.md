# Expression Cache Incremental Append — Specification

## Overview

Nightly append of 1 new bar to the expression cache for all ~11,200 tickers.
Target: under 5 minutes total. Current "append" does a full recompute: 124 minutes.

## Core Principle

**Zero lookback.** Each new bar is computed from:
1. Today's daily OHLCV candle (1 bar: O, H, L, C, V)
2. The previous bar's expression values (already in the cache)
3. A small state file per ticker (~3 KB) with intermediate values needed for forward computation

No ExpressionEngine. No full indicator series. No pandas. Pure numpy scalar math.

## Storage Format

### Base file: `{TICKER}.npz` (read-only after full rebuild)
- Existing compressed file from last full rebuild
- Contains `data` (float16, n_bars × 15,805) and `dates` (string array)
- **Never modified by nightly append.** Frozen until next full rebuild or manual consolidation.

### Append file: `{TICKER}.append` (raw binary, grows nightly)
- Raw float16 array, no compression, no headers
- Each night: 1 new row of 15,805 values appended (31 KB)
- File grows by 31 KB per trading day
- After 250 trading days: ~7.7 MB per ticker, ~86 GB total across all tickers
- To read: `np.frombuffer(data, dtype=np.float16).reshape(-1, 15805)`

### State file: `{TICKER}.state` (overwritten nightly)
- Raw float64 array, ~3 KB per ticker
- Contains intermediate values needed to compute the next bar
- Overwritten (not appended) each night with current state
- Total across all tickers: ~34 MB (negligible)

### Dates file: `{TICKER}.dates` (appended nightly)
- One date string per line, appended nightly
- Or raw binary of date ordinals — TBD simplest approach

## Consumer Impact

### `load_ticker_cache(ticker)` — updated
```
1. Load base .npz → dates_base, data_base (float16 → float32)
2. If .append file exists:
   a. Read raw binary → data_append (float16 → float32)
   b. Read .dates file → dates_append
   c. Concatenate: dates = concat(dates_base, dates_append)
   d. Concatenate: data = vstack(data_base, data_append)
3. Return (dates, data)  — shape (n_bars_total, 15805), float32
```
Consumers see the same (dates, data) tuple as before. The extra 212 state
variables are NOT in the expression columns — they're only in the .state file.
Column indices 0-15,804 are unchanged. No consumer code changes needed.

### `signal_filter._load_ticker_npz(ticker)` — updated same way
This function bypasses load_ticker_cache but does the same np.load.
Update to also read .append + .dates and concatenate.

### All other consumers
Use `ExprSeriesCache.get_ticker()` which calls `load_ticker_cache()`.
Transparent — no changes needed.

### Performance impact on grinds
- Base .npz decompression: ~0.3s per ticker (unchanged)
- Append file read: under 1ms (raw binary, small file)
- vstack: microseconds
- Net impact: negligible. Consensus runs (11+ hours) see no meaningful slowdown.

## State File Contents (~212 float64 values)

### Cumulative sums (for SMA-based indicators)
- `cumsum_close` — running sum of all closes from bar 0
- `cumsum_volume` — running sum of all volumes
- `cumsum_hl` — running sum of (high - low), for ADR
- `cumsum_tr` — running sum of true range, for ATR
- `cumsum_bop_raw` — for BOP SMA
- `cumsum_mfv` — money flow volume, for CMF
- `cumsum_abs_diff` — for Kaufman efficiency
- `cumsum_tp` — typical price, for CCI
- `cumsum_c2` — close squared, for Bollinger stddev

### EMA states
- `xavgc{p}` for p in [5,8,9,10,12,13,20,21,30,50,65,100,150,200] — 14 values
  (These are also daily intermediates but stored at float64 precision for forward computation)

### RSI internals
- `rsi_avg_gain_{p}`, `rsi_avg_loss_{p}` for p in [5,7,9,14,21,28] — 12 values
  RSI uses Wilder smoothing: avg_gain[i] = (avg_gain[i-1]*(p-1) + gain) / p
  Cannot be recovered from the RSI value alone without precision loss.

### ADX chain
- `ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` for p in [7,10,14,20] — 12 values

### MACD signal line
- `macd_signal_{fast}_{slow}` for 5 MACD pairs — 5 values

### Stochastic raw_k
- `raw_k_{p}` for p in [3,5,7,9,10,14,21,28,50] — 9 values
  Stochastic uses 3-bar SMA of raw_k. Need previous 2 raw_k values.
  Actually need raw_k[i-1] and raw_k[i-2] — so 18 values (2 per period).

### Rolling max/min tracking
- `maxh_idx_{p}` for 29 maxH periods — bar index where current max occurred
- `minl_idx_{p}` for 19 minL periods — bar index where current min occurred
- `maxc_idx_{p}` for 3 maxC periods — bar index where current max close occurred
  Total: 51 values
  
  Update rule: if new bar's value >= current max, update index to current bar.
  If current max's index is falling out of the window (idx < current_bar - period),
  need to rescan the window. The window data is in the base .npz + .append file,
  which is already loaded by load_ticker_cache(). So the append worker loads the
  last N values for the affected period and finds the new max. This is rare
  (only when the old max drops off) and the scan is tiny (max 200 values).

### Aroon tracking
- `aroon_maxh_idx_{p}`, `aroon_minl_idx_{p}` for 7 periods — 14 values
  Same pattern as rolling max/min.

### OBV
- `obv` — cumulative, just previous value needed (already in intermediates)

### HTF partial candle state (per timeframe: weekly + monthly)
- `htf_{w,m}_partial_{open,high,low,close,volume}` — 10 values
- `htf_{w,m}_period_id` — 2 values (which week/month number we're in)
- `htf_{w,m}_xavgc{p}` — EMA states, 14 × 2 = 28 values
- `htf_{w,m}_ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` — 12 × 2 = 24 values
- `htf_{w,m}_obv` — 2 values
- `htf_{w,m}_cumsum_*` — 11 cumsums × 2 = 22 values
- `htf_{w,m}_macd_signal_*` — 5 × 2 = 10 values
  Total HTF state: ~98 values

### LSP state
- Serialized pivot data: active pivot prices, break counts, bar indices
- Variable length — stored as a separate small binary blob or JSON
- Updated each bar: check if new pivot formed, update break counts

### Algo line state
- Serialized trendline data: slope, intercept, volume, bar index
- Similar to LSP — small variable-length blob

## 1-Bar Forward Computation — By Expression Type

### Daily Arithmetic Expressions (~1,850)

**extension(close, ma, normalizer):**
- SMA: `avgc50[i] = (cumsum_close[i] - cumsum_close[i-50]) / 50`
  where `cumsum_close[i] = cumsum_close[i-1] + close[today]` (from state)
  and `cumsum_close[i-50]` is row (current_bar - 50) in the cached data.
  For this we need to read that ONE historical value from the .append or base .npz.
  But wait — we said 1 bar lookback only. The cumsum at i-50 was the cumsum 
  state value 50 days ago. We don't have that in the state file.
  
  RESOLUTION: Store the cumsum in the STATE file. Store it also as a column
  in the .append file (as an intermediate column). Then cumsum[i-50] is 
  just column value at row (current_bar - 50) in the combined data.
  
  BUT — that means the .append file needs intermediate columns too, 
  which changes the column count from 15,805 to 15,805 + intermediates.
  And consumers only expect 15,805 columns.
  
  RESOLUTION: The .append file stores 15,805 + N_intermediates columns.
  load_ticker_cache() reads the .append file and returns ONLY the first
  15,805 columns to consumers. The append worker reads ALL columns 
  (including intermediates) for its forward computation.
  
  This way:
  - Consumers see 15,805 columns (unchanged)
  - Append worker has access to historical intermediate values via the .append file
  - No .state file needed for cumsums — they're in the .append columns
  - The .state file reduces to just the values that AREN'T stored historically
    (like rolling max/min indices, HTF partial candle state, LSP/algo state)

- EMA: `xavgc20[i] = alpha * close[today] + (1-alpha) * xavgc20[i-1]`
  Previous EMA value is in the intermediate column of the previous .append row.

- Result: `(close[today] - avgc50[i]) / atr14[i]`

**ma_slope(ma, offset, normalizer):**
- `(ma[i] - ma[i - offset]) / normalizer[i]`
- ma[i] computed as above. ma[i-offset] is the intermediate column value 
  at row (current_bar - offset) in the cached data.

**rsi(period):**
- `avg_gain[i] = (avg_gain[i-1] * (p-1) + max(0, close[today] - close[yesterday])) / p`
- `avg_loss[i] = (avg_loss[i-1] * (p-1) + max(0, close[yesterday] - close[today])) / p`
- `rsi[i] = 100 - 100 / (1 + avg_gain[i] / avg_loss[i])`
- avg_gain/avg_loss at i-1 stored as intermediate columns in previous .append row.
- close[yesterday] is the OHLCV close from the previous bar (in daily pickle or 
  stored in state).

**adx(period):**
- Needs smoothed DM+, DM-, then DI+, DI-, then DX, then smoothed DX.
- All EMA-based: `ema[i] = alpha * value + (1-alpha) * ema[i-1]`
- Previous EMA states in intermediate columns.

**rolling max/min (maxh, minl, maxc):**
- If new bar's high >= maxh[i-1]: maxh[i] = new high, update index
- Else if the bar at maxh_idx is still in window: maxh[i] = maxh[i-1]
- Else: rescan window from historical data (rare, max 200 values)
- For rescan: read the high column from intermediate columns in cached data

**percentile_rank(source, period):**
- Needs to know how many of the last N values are <= current value.
- This requires access to the last N values of the source.
- Source is one of: close, volume, range, atr14, rsi14.
- These are all available as intermediate columns in the cached data.
- Read last N values from the column, count <= current value. Simple.
- Max period is ~252. Reading 252 float16 values from a file = trivial.
- NOTE: This is a "lookback into cached data" but NOT into raw OHLCV.
  We're reading from our own intermediate columns in the .append file.

**bars_since_ma_cross(ma, max_lookback):**
- Scan backwards from current bar: is close > MA? Find where it flips.
- Previous bar's bars_since value helps: if sign didn't change, increment by 1.
  If sign DID change, reset to 1.
- Stored as expression column — just check: did close cross MA today?
  If same side: previous_value + 1. If crossed: 1.

**All other arithmetic ops** follow similar patterns:
- candle_range_ratio: (H-L) / ATR14 — pure OHLCV + intermediate
- body_range_ratio: abs(C-O) / (H-L) — pure OHLCV
- volume_ratio: V / avgv{p} — OHLCV + intermediate
- bollinger: need SMA + stddev. stddev from cumsum_c2 and cumsum_c intermediates.
- aroon: need position of max/min in window — tracked in intermediates
- cmf: need cumsum of MFV — intermediate column
- kaufman: need cumsum of abs_diff — intermediate column
- obv: obv[i-1] + sign(close_change) * volume — intermediate + OHLCV

### Daily Boolean Aggregates (~2,413)

**count_true(condition, period):**
- The condition (e.g., "rsi14_gt_50") evaluates to true/false at the new bar.
- `count[i] = count[i-1] - bool_value[i-period] + bool_value[i]`
- bool_value[i-period] is the boolean at the bar dropping out of the window.
- The boolean depends on an expression value (rsi14 > 50), which IS in the 
  cached data at row (i-period).
- So: read expression value at (i-period), evaluate condition, subtract.
  Evaluate condition at current bar, add.

**since_true(condition, period):**
- If condition is true today: 0
- Else: since_true[i-1] + 1 (capped at period, then -1)
- Previous since_true is in the expression column of previous row. Done.

**true_in_row(condition, period):**
- If condition is true today: true_in_row[i-1] + 1
- Else: 0
- Previous value is in the expression column. Done.

### Extension Structure (~1,198 on_series / on_series_bool_agg)

These operate on the 2 base extension series: ext_avgc50_adr14, ext_avgc200_adr14.
Both are regular daily arithmetic expressions — computed as part of step above.

**on_series inner ops (trendline_deviation, channel_position, etc.):**
- These are computed on the extension series as if it were price data.
- Same 1-bar-forward logic as daily arithmetic, but the "price" is the 
  extension series value instead of close.
- The extension series history is in the expression cache (it's an expression column).
- trendline_deviation needs a window of extension values + linear regression.
  Read the window from cached data, compute regression, get deviation at current bar.
- channel_position: same — window + regression + stddev normalization.

**on_series_bool_agg:**
- Same as daily boolean aggregates but on an indicator computed from the extension series.
- The indicator (e.g., RSI of extension series) needs its own forward state.
- Store these as additional intermediate columns.

### LSP Expressions (80 precomputed)

LSP detects pivot highs/lows and computes: distance, bars_back, break_count, 
AVWAP distance for top 5 above and below.

**1-bar forward update:**
- Did today's bar create a new pivot? Check if H[today] > H[yesterday] and 
  H[yesterday] > H[two days ago] (for pivot high at yesterday). Need H[yesterday-1]
  which is just the previous bar's high — available from state or intermediate.
- Actually, pivot detection needs N bars on each side (window sizes 5,10,15,20,30,40).
  A pivot at bar X with window 40 is only confirmed 40 bars LATER. So pivots are 
  detected with a lag. On each new bar, check if bar (today - window) is a pivot.
- Update break counts: for each active level, did today's bar break it?
  Compare today's H/L against level prices.
- Update distances: (level_price - close[today]) / ATR14[today]
- Update bars_back: increment by 1 for all levels.
- Update AVWAP: rolling VWAP from pivot bar to today.

LSP state is variable-length (depends on how many pivots are active).
Stored as a serialized blob in the .state file.

### Algo Line Expressions (44 precomputed)

Similar to LSP — detect trendlines from high-volume bars, track distance/slope/touch_count.

**1-bar forward update:**
- Check if today forms a new trendline anchor (high volume bar).
- Update distances to existing trendlines.
- Track touches and breaks.

Algo state is also variable-length, stored in .state file.

### HTF Expressions (~10,466: 5,233 weekly + 5,233 monthly)

**Partial candle update:**
- Load HTF partial candle state from state file (or previous intermediate columns)
- Is today in the same period as yesterday?
  - Same period: update partial candle
    - high = max(partial_high, today_high)
    - low = min(partial_low, today_low)  
    - close = today_close
    - volume += today_volume
    - open stays the same
  - New period: the previous partial becomes a closed candle
    - Update all HTF closed intermediates (EMA states roll forward, cumsums increment)
    - Start new partial: open=today_open, high=today_high, low=today_low, 
      close=today_close, volume=today_volume
- Compute all HTF expression values using the same 1-bar-forward formulas
  as daily, but operating on HTF intermediates + partial candle values.
- The partial candle engine already does exactly this logic.

**Period boundary detection:**
- Weekly: is today Monday? (or first trading day of the week)
- Monthly: is today the first trading day of the month?
- Compare today's date vs previous bar's date to detect boundary.

## One-Time Setup (~33 minutes)

Transform existing cache to add intermediate columns and generate state files:

For each of 11,200 tickers:
1. Load .npz (dates + data)
2. Load ticker's OHLCV from daily pickle
3. Build ExpressionEngine on full OHLCV
4. Run build_numpy_intermediates() → get all intermediate arrays
5. Extract the cumulative sums and EMA states needed for forward computation
6. Save .state file with last-bar values of all state variables
7. Compute intermediate columns for all historical bars
8. Save .append file with intermediate columns (so future lookbacks work)
   — Actually NO. The .append file only stores rows AFTER the base .npz.
   Historical intermediate values for rows IN the base .npz are NOT accessible
   during append. This is a problem for SMA (need cumsum[i-50]) and 
   percentile_rank (need last 252 values) and count_true (need bool[i-period]).

   RESOLUTION: During setup, also write a `.history` file containing the 
   intermediate columns for the LAST N rows of the base .npz (where N = max 
   lookback period, ~252). This is ~252 × intermediates × 2 bytes per ticker.
   That's 252 × 178 × 2 = ~90 KB per ticker, ~1 GB total. Negligible.
   
   On append: the worker reads the .history file to get intermediate values
   for rows that are in the base .npz. As new rows accumulate in .append,
   the .history window slides forward and old rows in .history become unused.
   Eventually all lookback rows are in .append and .history is no longer needed.

   Actually simpler: the .history file just stores the last 252 rows of 
   intermediate columns from the base .npz. Fixed size, written once during setup.
   The append worker reads from .history OR .append depending on which row it needs.

Wait — this is getting complicated. Let me simplify.

## REVISED: Append File Stores Everything

The .append file stores 15,805 expression columns + N intermediate columns per row.

During the one-time setup:
1. Load .npz
2. Compute intermediates for ALL historical bars  
3. Write the LAST 252 rows (expressions + intermediates) to a .lookback file
4. Write .state file (last-bar values only)

Nightly append:
1. Read .state file (last-bar intermediates + HTF state + LSP/algo state)
2. Read today's OHLCV bar
3. For expressions needing lookback (SMA, count_true, percentile_rank, rolling max/min):
   Read the specific historical value from .lookback or .append file
4. Compute new intermediates + new expression values
5. Append full row (expressions + intermediates) to .append file
6. Update .lookback: drop oldest row, add new row (sliding window)
7. Overwrite .state file

load_ticker_cache():
1. Read base .npz → data_base (n_base × 15,805)
2. Read .append → data_append, take ONLY first 15,805 columns
3. Concatenate and return

This means:
- .append file is wider than base .npz (has intermediate columns)
- load_ticker_cache strips the extra columns — consumers see 15,805
- .lookback file provides the trailing window of intermediates for 
  lookback ops (cumsums, rolling max indices, boolean values from N bars ago)
- .state file provides the current state for non-historical intermediates
  (HTF partial candle, LSP/algo state)

## File Summary

| File | Size per ticker | Total | Written when | Purpose |
|------|----------------|-------|-------------|---------|
| `{T}.npz` | ~10 MB | 111 GB | Full rebuild only | Base historical data (15,805 cols) |
| `{T}.append` | 31 KB/day | ~0.3 GB/month | Nightly | New expression rows (15,805 + intermediates) |
| `{T}.lookback` | ~90 KB | ~1 GB | Setup + nightly | Last 252 rows of intermediates (sliding window) |
| `{T}.state` | ~3 KB | ~34 MB | Nightly | Forward computation state (HTF, LSP, algo, EMA, cumsums) |
| `{T}.dates` | ~10 bytes/day | tiny | Nightly | Date strings for appended rows |

Total disk after setup: 111 + 1 + 0.034 = ~112 GB
Growth: ~0.3 GB per month (from .append files)
After 1 year: 111 + 3.6 + 1 = ~116 GB

## Consolidation (Optional, Quarterly)

Merge base .npz + .append into new base .npz. Regenerate .lookback from new base.
Delete old .append. Takes ~33 minutes. Resets growth to zero.

## Nightly Append Timing Estimate

Per ticker:
- Read .state: < 1ms (3 KB)
- Read lookback values: < 1ms (sequential read from .lookback)
- Compute intermediates: < 1ms (scalar math)
- Compute 15,805 expressions: < 1ms (scalar math from intermediates)
- Compute HTF expressions: < 1ms (same scalar math on HTF intermediates)
- Compute LSP/algo updates: < 5ms (pivot detection, break checking)
- Write .append row: < 1ms (seek + 32 KB write)
- Write .state: < 1ms (3 KB overwrite)
- Update .lookback: < 1ms (shift + write)

Total per ticker: ~10ms

11,200 tickers / 14 workers: 11,200 × 10ms / 14 = ~8 seconds

Add overhead (pickle load, process spawning, manifest update): ~2-3 minutes

**Total nightly append: under 5 minutes.**

