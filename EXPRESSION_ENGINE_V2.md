# Expression Engine V2 — Build Plan

## What's Being Added

Three new TA capabilities, all precomputed into the expression cache so grinders see them as regular expressions:

### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-pivot expressions: `lsp1_distance`, `lsp1_break_count`, `lsp1_bars_back`, `lsp1_avwap_distance`, `lsp2_distance`, etc.
- The grinder discovers which pivot characteristics matter per setup (DTSS wants unbroken highest, pdub_unr wants once-broken, big base break wants monthly-scale)

### 2. Multi-Timeframe OHLCV
- Resample daily data → weekly (W), monthly (ME), yearly (YE) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.
- Grinder sees daily + weekly + monthly + yearly expressions as flat columns — discovers cross-timeframe alignment automatically

### 3. Contextual AVWAPs
- **Highest AVWAP of all time:** For each bar, brute-search all prior bars as anchor points, find which anchor produces the highest AVWAP value at the current bar. Also lowest.
- **Per-pivot contextual AVWAP:** For each detected LSP pivot, search bars before the pivot for the anchor that produces the highest (or lowest) AVWAP at the current bar.
- Expose as expressions: `highest_avwap_distance`, `lowest_avwap_distance`, `lsp1_ctx_avwap_distance`, etc.
- These are full series (value at every bar) so existing expression patterns (crosses, rolling counts, slopes) work on them automatically

---

## Architecture Constraints (Non-Negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` → `compute_series()`. No separate code paths for LSP/HTF/AVWAP.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and AVWAP computation happen during cache build. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local 5yr OHLCV cache. LSP detector refactored to accept DataFrame, not fetch from API.
4. **Parallel via ProcessPoolExecutor:** Same worker pattern as current cache builder. CPU-bound work spread across all cores.
5. **100% example pass rule:** New expressions either pass all examples or get auto-excluded from ranges. Existing grinder logic handles this — no grinder code changes.
6. **Grinders unchanged:** Pyramid, exit, outcome grinders see a bigger expression library. Same beam search, same matrix operations, same everything. Just more columns.

---

## Build Tasks (Ordered)

### Task A: LSP Detector Refactor
**What:** Make `lsp_detector.py` accept a DataFrame + target index, detect pivots across all timeframes, and cluster into proximity-ordered levels.

**New interface:**
```python
class LSPDetector:
    def __init__(self, daily_df: pd.DataFrame, weekly_df: pd.DataFrame, monthly_df: pd.DataFrame):
        """Initialize with OHLCV DataFrames. Precompute all pivots once."""
        # Detect pivots on each timeframe
        # Map weekly/monthly pivots back to daily bar indices
        # Store all raw pivots with timeframe tag
    
    def get_levels_at_bar(self, bar_idx: int, n_above: int = 5, n_below: int = 5) -> dict:
        """Get proximity-ordered levels as of a specific bar.
        
        Returns dict with 'above': [level1, level2, ...], 'below': [level1, level2, ...]
        Each level: {price, pivot_count, timeframe_count, break_count, 
                     max_window, bars_back_nearest, volume_ratio}
        
        Fast — uses precomputed pivot table, just clusters and sorts for the given bar.
        """
```

**Key optimization:** Pivot detection runs ONCE per ticker per timeframe across full history. `get_levels_at_bar()` filters to pivots before `bar_idx`, clusters within 1 ATR, sorts by proximity. This makes the millions of calls feasible.

**Break count computation:** For each pivot, count how many subsequent bars exceeded it (high above pivot high, or low below pivot low) up to the current bar_idx. This changes per bar — a pivot with 0 breaks at bar 500 might have 2 breaks at bar 700. Precompute a "first break bar" array per pivot so break count at any bar is a fast lookup.

**Validate:** Must still produce 23/23 match on DTSS labeled examples — the hand-labeled LSP should appear as `above1` (nearest level above) with `break_count=0` on the signal bar.

### Task B: LSP Expressions in Expression Engine
**What:** Expose LSP data as proximity-ordered **levels** (clustered pivots at similar prices), not individual ranked pivots.

**Level construction:**
1. Detect all pivots across all timeframes (daily, weekly, monthly)
2. Cluster pivots within 1 ATR of each other into a single **level**
3. Each level tracks: price zone center, pivot count (stacking strength), timeframe count, total break count, highest pivot window, contextual AVWAP
4. Order levels by proximity to the current bar's close: nearest above = `above1`, second nearest = `above2`, etc. Same below.

**Why proximity-ordered:** A mega-level 20% away doesn't matter at the signal bar. The grinder needs to see what's nearby. Strength/stacking is metadata ON the nearest levels, not a separate ranking axis.

**Per-level expressions:**
- `level_{dir}{rank}_distance` — distance from close to level center (normalized by ATR)
- `level_{dir}{rank}_pivot_count` — how many individual pivots clustered at this level (stacking strength — the big deal)
- `level_{dir}{rank}_timeframe_count` — how many timeframes (d/w/m) have a pivot at this level
- `level_{dir}{rank}_break_count` — total times any pivot in this cluster was broken
- `level_{dir}{rank}_max_window` — largest pivot window that detected a pivot here
- `level_{dir}{rank}_bars_back_nearest` — bars back to the most recent pivot in this cluster
- `level_{dir}{rank}_ctx_avwap_distance` — contextual AVWAP for this level (see Task D)
- `level_{dir}{rank}_volume_ratio` — highest volume ratio among pivots in this cluster

**Where `{dir}` = `above` or `below`, `{rank}` = 1-5 (nearest to furthest)**

**Expression count:** 8 expressions × 5 ranks × 2 directions = **80 LSP expressions** on daily timeframe. These already incorporate weekly/monthly pivots via the clustering — a daily pivot at $100 and a monthly pivot at $101 merge into one level with `timeframe_count=2` and `pivot_count=2`.

**No separate w_/m_ LSP expressions needed** — the multi-timeframe information is encoded in `timeframe_count` and `pivot_count` within each level. This is much cleaner than running LSP expressions per timeframe.

**Context injection:** `set_lsp_context()` updated to accept a list of levels (above + below), each with full metadata.

**Deduplication is built in:** Clustering handles the overlap between daily/weekly/monthly pivots naturally — they merge into the same level.

### Task C: Higher Timeframe Resampling
**What:** In the cache builder, resample daily OHLCV → weekly, monthly before computing expressions.
**How:**
```python
def resample_ohlcv(daily_df, freq='W'):
    """Resample daily OHLCV to weekly/monthly."""
    df = daily_df.set_index('date')
    resampled = df.resample(freq).agg({
        'open': 'first', 'high': 'max', 'low': 'min', 
        'close': 'last', 'volume': 'sum'
    }).dropna()
    return resampled.reset_index()
```
**Mapping back to daily bars:** Each daily bar maps to a weekly/monthly bar. The expression value for "weekly RSI on 2024-03-15" is the weekly RSI for the week containing that date. This means HTF expression series are step functions on the daily timeline (constant within each week/month).

**Expression naming:** Prefix with timeframe: `w_` (weekly), `m_` (monthly). E.g., `w_rsi_14`, `m_ext_above_avgc50_adr`.

**Expression count impact:** Current 4,017 × 2 additional timeframes = ~8,034 new expressions. Total ~12,051.

**Cache size impact:** Currently ~21 GB for 4,017 expressions. 3x expressions = ~63 GB total.

### Task D: Contextual AVWAP Computation
**What:** Precompute pivot-anchored contextual AVWAP series per level.

**Per-Level Contextual AVWAP:**
For each clustered level, take the most prominent pivot in the cluster. Search the ~20-30 bars before that pivot for the anchor bar that produces the highest AVWAP at the current bar. Also find the anchor that produces the lowest. This captures the "average buyer/seller cost basis" relative to each structural level.

**Optimization:** Precompute cumulative TP×V and cumulative V arrays once per ticker. AVWAP from any anchor to any bar is then just `(cum_tpv[bar] - cum_tpv[anchor-1]) / (cum_v[bar] - cum_v[anchor-1])`. Two array lookups and a division. For ~20-30 candidate anchors per level × ~10 levels per bar, this is very fast.

**Both directions:**
- Highest contextual AVWAP per level (sellers' break-even — relevant for longs)
- Lowest contextual AVWAP per level (buyers' break-even — relevant for shorts)

**Expressions (already included in Task B's per-level list):**
- `level_{dir}{rank}_ctx_avwap_distance` — close vs contextual AVWAP for this level

**Note:** "Highest all-time AVWAP" excluded — only 5yr of data, not enough. Revisit when full history is available.

### Task E: Integration into Cache Builder
**What:** Update `expr_cache_builder.py` to:
1. For each ticker: resample daily → weekly, monthly
2. For each ticker: run LSP detector on all three timeframes, merge into unified pivot list
3. For each ticker: precompute cumulative TP×V and V arrays for contextual AVWAP lookups
4. For each bar: get proximity-ordered levels, set LSP context, compute LSP + AVWAP expressions
5. For each timeframe (weekly, monthly): compute full expression library as series, map back to daily bar indices
6. Save expanded array to cache (daily expressions + HTF expressions + LSP/AVWAP expressions)

**Worker function outline:**
```python
def _compute_ticker_full(args):
    ticker, df_dict = args
    df = pd.DataFrame(df_dict)
    
    # 1. Resample
    weekly_df = resample_ohlcv(df, 'W')
    monthly_df = resample_ohlcv(df, 'ME')
    
    # 2. LSP detection (once per ticker, all timeframes)
    detector = LSPDetector(df, weekly_df, monthly_df)
    
    # 3. Precompute AVWAP lookup arrays
    cum_tpv, cum_v = precompute_avwap_arrays(df)
    
    # 4. Daily expressions (existing — series-based, fast)
    engine = ExpressionEngine(df)
    for j, expr in enumerate(daily_expressions):
        data[:, j] = compute_series(engine, expr["compute"])
    
    # 5. HTF expressions (series on resampled data, mapped to daily indices)
    w_engine = ExpressionEngine(weekly_df)
    m_engine = ExpressionEngine(monthly_df)
    for j, expr in enumerate(daily_expressions):  # same expressions, different data
        w_series = compute_series(w_engine, expr["compute"])
        m_series = compute_series(m_engine, expr["compute"])
        # Map back: each daily bar gets the value from its containing week/month
        data[:, weekly_offset + j] = map_htf_to_daily(w_series, weekly_df, df)
        data[:, monthly_offset + j] = map_htf_to_daily(m_series, monthly_df, df)
    
    # 6. LSP + AVWAP expressions (per-bar — needs level context per bar)
    for bar_idx in range(50, n_bars):  # skip warmup
        levels = detector.get_levels_at_bar(bar_idx)
        # Compute LSP expressions from level metadata
        # Compute contextual AVWAP distances using cum_tpv/cum_v arrays
        data[bar_idx, lsp_offset:lsp_offset+n_lsp_exprs] = compute_lsp_expressions(levels, df, bar_idx, cum_tpv, cum_v)
```

**Performance note:** Steps 4 and 5 are fast (series-based, same as current). Step 6 is per-bar but uses precomputed data — the `get_levels_at_bar()` call is just filtering and sorting, and AVWAP is two array lookups. Should add ~30-50% to current build time, not 10x.

### Task F: Matrix Builder Update  
**What:** Update `matrix_builder.py` to include LSP + HTF + AVWAP expressions.
- `get_universe_matrix()`: uses the expanded expression cache (no code changes needed if cache is already built)
- `get_example_matrix()`: for DTSS examples, inject hand-labeled LSPs; for others, use detector
- `compute_example_ranges()` in pyramid_grinder: same — inject LSP context per example

### Task G: Expression Library Update
**What:** Update `brute_expressions.py` to include:
- LSP expressions (36 new)
- HTF expressions (curated subset × 3 timeframes)  
- Contextual AVWAP expressions (8-12 new)
- Total new expressions TBD based on HTF subset decision

---

## Performance Estimates

| Component | Current | After V2 |
|-----------|---------|----------|
| Expression count | 4,017 | ~12,000+ (daily + weekly + monthly + LSP + AVWAP) |
| Cache size (disk) | ~21 GB | ~63-70 GB |
| Full cache build | ~40 min | ~90-150 min (one-time) |
| Nightly append | ~5-8 min | ~12-20 min |
| Matrix rebuild | ~5 min | ~10-15 min |
| Grinder runtime | ~2-3 min | ~4-8 min (more expressions to search) |

## Decisions (Resolved)

1. **HTF expression scope:** Full 4,017 on weekly + monthly. ~84 GB cache is fine (600 GB free).
2. **Highest all-time AVWAP:** EXCLUDED — only 5yr data, not enough for "all time." Pivot-anchored contextual AVWAPs only. Revisit when full history available (~1 TB cache).
3. **Yearly timeframe:** EXCLUDED — only ~5 bars in 5yr history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Expression engine exposes top N as ranked expressions.
5. **LSP on HTF:** Yes — run pivot detection natively on weekly + monthly resampled data. Deduplicate overlaps (daily pivot at same price as weekly pivot = keep weekly version).

---

## Build Order

```
Task A (LSP detector refactor)          — standalone, testable
    ↓
Task B (LSP level expressions)          — needs A
    ↓
Task C (HTF resampling)                 — standalone, testable  
    ↓
Task D (Contextual AVWAPs)              — needs A (uses pivot/level locations)
    ↓
Task E (Cache builder integration)      — needs A, B, C, D
    ↓
Task F (Matrix builder + example flow)  — needs E
    ↓
Task G (Expression library registry)    — needs B, C, D
    ↓
Full cache rebuild + validation
    ↓
Re-grind DTSS with expanded library
```

Tasks A and C can be done in parallel. Task D depends on A.
