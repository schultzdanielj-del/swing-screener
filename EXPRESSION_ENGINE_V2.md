# Expression Engine V2

Architecture reference for the `.npz` expression cache consumed by the grinders. The cache is built and operational. Pending work lives under Pending research and Pending build.

## 1. Purpose

The grinder evaluates expressions at arbitrary bars across the full historical universe. The expression cache pre-computes every expression value for every bar of every ticker and stores it as a per-ticker `.npz` file. Grinders load per-ticker data via `ExprSeriesCache.get_ticker()` and slice to the bars and columns they need.

The full cache is built from ground truth via `expr_cache_builder.build_full()` (uses `_compute_ticker_full` — the authoritative compute path). Historical expression values are immutable — bar 500 of AAPL must produce the same values today as yesterday, modulo a ticker-specific rebuild caused by a split-adjustment detection.

A separate best-effort fast path exists for nightly updates to the live-scan watchlist (`forward_prop_engine.py`, documented in `FORWARD_PROP_SPEC.md`). The forward-prop path is not consensus-grade — before any consensus pipeline run, run a full rebuild to guarantee every expression is computed from ground truth.

**Critical correctness gate:** After any rebuild with optimized code, the signal filter must still find ALL examples. If it doesn't, the optimization is broken and we don't ship it.

**HTF look-ahead bias — FIXED (2026-04-01):** The partial candle engine (`local_runner/partial_candle_engine.py`) computes HTF expression values using only data available on each day. Monday of a week sees only Monday's partial weekly candle; Friday sees the full closed week. All prior completed periods use final closed values. Fallback to closed-candle mapping retained for unhandled ops. Requires full expression cache rebuild to take effect.

## 2. EXACT spec

### Expression library categories

#### 1. LSP Detection (Left Side Pivots)
- Find all pivot highs and pivot lows across multiple window sizes (5, 10, 15, 20, 30, 40)
- For each pivot: track price, bars back, break count (how many times subsequent bars exceeded it)
- Return top N pivots ranked by prominence
- Expose per-level expressions: `level_above1_distance`, `level_above1_break_count`, `level_above1_bars_back_nearest`, etc.

#### 2. Multi-Timeframe OHLCV
- Resample daily data to weekly (W), monthly (ME) using pandas
- Run the FULL existing expression library on each timeframe
- Expression naming: `w_rsi_14` (weekly RSI 14), `m_ext_above_avgc50` (monthly extension above 50 SMA), etc.

#### 3. Contextual AVWAPs — REMOVED
- AVWAP computation has been removed from the project. Dan handles AVWAP manually at trade entry.
- All AVWAP code was removed from `lsp_detector_v2.py`, `algo_line_detector.py`, `exit_compute.py`, `exit_expressions.py`, and `expression_engine.py` (2026-04-02).

### History window

`EXPR_CACHE_START = 2020-01-02`. OHLCV data before this date is truncated before computing expressions. ~6 years of history, chosen to balance cache size against grinder scan diversity.

### Full-rebuild compute path (optimizations shipped)

The full-rebuild compute path (via `expr_cache_builder.build_full()`, `_compute_ticker_full`) applies these optimizations:

**1. SLOW_OPS custom numpy (daily):** percentile_rank, roc_percentile_rank, bars_since_ma_cross, swing_high/low_count, higher/lower_high/low_count go through custom numpy implementations instead of compute_series. percentile_rank uses sliding_window_view + vectorized comparison. swing_high/low_count precomputes swing boolean array once, reuses for all periods. higher/lower_high/low_count use the same precomputed swing arrays. roc_percentile_rank vectorized with sliding_window_view. bars_since_ma_cross uses numpy loop replacement for nested Python loop. Warms the ExpressionEngine cache as a side effect.

**2. Numpy boolean aggregates (daily):** count_true uses numpy cumsum trick. since_true uses numpy running counter with pre-computed bars_since array once per unique condition. true_in_row uses numpy backward scan. Unique conditions computed once and cached. Same optimization applied to HTF weekly and monthly engines.

**3. Extension structure (daily):** Vectorized linreg for trendline_deviation and channel_position via `sliding_window_view` + vectorized mean/slope/std computation across ALL windows simultaneously. Cached bool_agg — unique indicator booleans computed once, dispatched to all expressions.

**4. HTF intermediates dispatch (weekly + monthly):** `build_numpy_intermediates` on HTF engine (cheap on 260/60 bar arrays, ~50ms vs hundreds of ms on daily). `dispatch_arith_numpy` for HTF arith ops with `compute_series` fallback for unhandled ops. Numpy bools at HTF resolution. HTF ext struct at HTF resolution — compute base extension series from intermediates, vectorized linreg, cached bool_agg, then map to daily. Eliminates thousands of `compute_series()` calls per timeframe.

**5. Fast compression:** `zipfile.ZipFile` at `compresslevel=1` instead of `np.savez_compressed` default zlib. Roughly 4x faster saves, files only ~5% larger. Compression was the single biggest production win.

**6. Worker-side saves (no IPC bottleneck):** `_compute_and_save_ticker` saves `.npz` inside the worker rather than returning large numpy arrays through IPC pipes to the main thread for serial save. Eliminates per-ticker serialize/deserialize cost and serial I/O blocking.

**7. Sorted work items:** Tickers sorted by bar count descending. Large tickers go first, short ones fill gaps at the end for better load balancing.

**8. Bounds checks on numpy helpers:** `np_count_true`, `np_since_true`, `np_true_in_row`, `np_swing_count_rolling`, `np_trend_swing_count` guard `period <= n_bars`. HTF monthly arrays can be short (minimum around 20 bars) while periods go up to 50. Without guards, IndexError crashes the ticker.

**9. RuntimeWarning suppression:** `warnings.filterwarnings("ignore", category=RuntimeWarning)` prevents numpy division warnings from being raised thousands of times per ticker.

**10. Rolling max/min helpers:** `_rolling_max` and `_rolling_min` in `dispatch_arith_numpy` use pandas rolling (not Python for-loops).

### Incremental append infrastructure (Increment 1 shipped; Increment 2 is Pending build)

**OHLCV infrastructure (Increment 1):**

- `cache_builder.py` — Railway HTTP replaced with EODHD API. `_yf_download(ticker, start, interval)` — unified download function using explicit start date. `_yf_append_after_date(ticker, after_date)` — fetches only new bars. `get_tradable_tickers_local()` reads from local SQLite `data/scanperfect.db`. `append_daily_cache()` reads ticker list from existing pickle keys. `check_yfinance_freshness()` downloads 1 SPY bar, compares to cache. Output format identical — same pickle, same DataFrame structure.
- HTF caches merged into `cache_builder.py`. `universe_ohlcv_weekly.pkl` and `universe_ohlcv_monthly.pkl`, dict-of-DataFrames format. Pulled from source (not resampled from daily). 10yr lookback from `HISTORY_START` — weekly expressions need ~4yr of bars for 200-period lookbacks plus warmup; 10yr gives 522 weekly bars, 120 monthly bars. Full build: `cache_builder.py --htf` or `--all` using `_batched_fetch()` with adaptive rate limiting. Nightly append: `_merge_htf_bars()` — overwrite partial bar if same date, append if new date, freeze history.
- `_sync_htf_cache(full_sweep)` unified HTF build and append. `full_sweep=True` updates stale tickers AND fetches missing ones. `full_sweep=False` only updates existing tickers with recent bars. Skips already-current tickers (compares last date to SPY's last date). Eliminates the old three-mode build/append/retry problem.
- Data validation infrastructure: `ticker_reference.json` with `firstTradeDateMilliseconds` for all tickers. SPY fetched first on every run — its date array is ground truth for expected bar counts. Exact match validation: ticker bars == SPY bars from `max(firstTradeDate, HISTORY_START)`. Mismatch = failed fetch → retry until pass or None. Don't save cache until all tickers validated. Split detection: tickers that split get full refetch.
- EODHD migration of `cache_builder.py`: OHLCV adjustment via `ratio = adjusted_close / close` applied to O/H/L/C (split + dividend adjusted). `EODHD_API_TOKEN` from environment variable. Ticker reference built from first-bar dates. Split detection via adjusted_close comparison. Adaptive rate limiting under EODHD's per-minute limit. `--force` flag: discard + rebuild for daily, weekly, monthly, htf, all.
- `append_new_bars()` truncates weekly/monthly pickles to `EXPR_CACHE_START` (2020-01-02) before passing to workers — same as `build_full()`. Also truncates daily OHLCV to `EXPR_CACHE_START` (~1,500 bars, not ~2,500). Sorts work items by bar count descending for load balancing.
- Nightly pipeline: 10 steps. Freshness check → daily append → weekly append → monthly append → expression cache → matrix → earnings → market → fundamentals → seed vault.

**Expression dependency audit (Increment 1):**

Classified all expressions by what each needs to compute a single new bar's value:

| Category | Description |
|----------|-------------|
| state_only | Prev expression row + today's OHLCV. Scalar math: EMA updates, slope diffs, ratios. |
| lookback | Needs historical window of prior expression values. Rolling max, percentile rank, boolean scans, aroon argmax, CCI mean deviation, trendline regression. Max depth observed: 1,260 bars. |
| htf | Needs HTF OHLCV pickles (weekly + monthly). Partial candle engine builds intermediates on closed HTF series, extends to today's partial candle. |
| precomputed_lsp | LSP detector — runs `compute_all_lsp_series(df)` on full daily OHLCV. |
| precomputed_algo | Algo line detector — runs `compute_all_algo_series(df)` on full daily OHLCV. |

Lookback depth distribution spans 1-10 / 11-50 / 51-126 / 127-252 / 253-504 / 505-1260 buckets. Deepest expressions are `ext_ceiling_ratio` from exit expressions at 1,260 bars (5 years).

**Immutability gate — PASSED:** Running `_compute_ticker_full` on N bars vs N-1 bars produces identical values for bar N-1. Zero mismatches across all expressions. Computation is deterministic — same data, same bar, identical output regardless of how many bars follow.

**Incremental feasibility:** Vast majority (over 99%) of expressions are incrementally computable without full OHLCV scan. Only LSP + algo detectors (~124 expressions) need the full daily DataFrame.

**What's shipped in Increment 1:**
- `_append_one_ticker()` worker infrastructure and save-phase (currently runs `_compute_ticker_full` internally — save-phase savings only).
- `scripts/validate_append_infra.py` — correctness gate. Fakes a new bar, verifies appended row matches fresh `_compute_ticker_full` output after float16 round-trip. Tests `load_ticker_cache` vstack, `signal_filter._load_ticker_npz`, file sizes, cleanup.
- Already wired into nightly pipeline step.

## 3. Details you need to know

### Architecture constraints (non-negotiable)

1. **Single computation path:** All expressions go through `ExpressionEngine` then `compute_series()`. No separate code paths for LSP/HTF.
2. **Precomputed in expr_cache_builder:** LSP detection, HTF resampling, and algo line detection happen during cache build. They are independent systems. Grinders never compute these live.
3. **No network calls in pipeline:** All data from local daily OHLCV cache.
4. **Parallel via ProcessPoolExecutor:** Same worker pattern across cache builder. CPU-bound work spread across all cores.
5. **100% example pass rule:** New expressions either pass all examples or get auto-excluded from ranges.
6. **Grinders unchanged:** Pyramid, exit, outcome grinders see a bigger expression library. Same beam search, same matrix operations. Just more columns.
7. **Historical immutability:** Old bars' expression values never change between rebuilds. Only new bars get appended.

### RAM management (critical)

`expr_cache_builder.py` deliberately loads the daily OHLCV pickle, prepares work items as dicts, then does `del universe_cache` + `gc.collect()` BEFORE spawning `ProcessPoolExecutor` workers. This is intentional — `ProcessPoolExecutor` copies data to each worker process. Without freeing the pickle first, workers × pickle size = multi-GB wasted RAM on top of each worker's own allocations. Has crashed before.

Any optimized worker must respect this pattern:
- The worker receives one ticker's OHLCV as a dict (not the full pickle)
- All numpy caches created inside the worker (swing arrays, bool caches, bars_since, sliding_window_view, intermediates dict) must be per-ticker only
- Worker must not hold references to large cross-ticker data
- The `del + gc.collect()` between phases in the main process is INTENTIONAL and must never be removed

RAM budget per worker: output array + daily intermediates dict + HTF intermediates + OHLCV dict. HTF dicts are small (~15KB per ticker). Workers are freed after work item prep alongside daily cache.

### Decisions (resolved)

1. **HTF expression scope:** Full library on weekly + monthly.
2. **Contextual AVWAPs:** REMOVED from project (2026-04-02). Dan handles AVWAP manually at trade entry.
3. **Yearly timeframe:** EXCLUDED — only ~5 bars in full history, useless for expressions. Weekly + monthly only.
4. **Number of LSP ranks:** ALL detected pivots, ranked. Top 5 above + 5 below exposed as expressions.
5. **Cache builder modes:** Two modes — full rebuild (current compute path, shipped) and incremental append (Increment 1 shipped, Increment 2 in Pending build). Full rebuild uses per-ticker worker with lazy-cached ExpressionEngine + targeted numpy replacements. Incremental loads existing `.npz`, computes only the new bar's values, appends.
6. **Nightly ticker coverage:** All valid tickers (≥50 bars). Every ticker must go through the pipeline every night. For incremental mode, every ticker still gets processed — it's just computing 1 new bar instead of all bars.
7. **Precompute-all on daily data is net negative.** Tested in benchmark AND in production — both confirmed slower. ExpressionEngine's lazy caching is the correct pattern for daily data. Precompute on HTF is net positive (small arrays, trivial cost). Daily uses SLOW_OPS numpy + compute_series for everything else.
8. **Compression:** `zipfile` `compresslevel=1` instead of `np.savez_compressed` default. ~4x faster saves, ~5% larger files.
9. **IPC:** Workers save `.npz` in-process, return only small metadata. NEVER return large numpy arrays through IPC.
10. **HTF on incremental:** Cannot just copy forward on non-boundary days. Weekly/monthly candles are partial (current week/month in progress) — today's close, this week's high/low so far, etc. Must recompute the current HTF period's expressions every day, but only 1 HTF bar of computation. Full HTF history stays cached.
11. **Failed approaches (do not re-litigate):**
    - Precompute-all intermediates on DAILY data: 5x SLOWER than original. ExpressionEngine's lazy caching is the correct pattern for daily data.
    - Daily arith two-phase dispatch (Phase B — `build_numpy_intermediates` + `dispatch_arith_numpy` on daily data): faster in benchmark when engine was pre-warm, SLOWER in production where workers start cold across thousands of tickers. Reverted — daily arith (non-SLOW_OPS) goes through original compute_series. HTF dispatch kept because HTF arrays are small (260/60 bars) so precompute is genuinely cheap.
    - High worker counts (cpu_count − 1): on i5-12600K severe contention. Throughput dropped from 1.1 to 1.0 tickers/s. CPU stayed low. Workers fighting over shared CPU caches.
    - Uncompressed saves (`np.savez`): would save compression time but cache grows 2-3x. Disk can't absorb. Reverted.

## 4. Known bugs

None currently identified as broken. (Bugs that have been fixed fold into the spec; currently-broken items surface here.)

## 5. Pending research

### Remaining optimization opportunities (open)

- Daily arith fallback ops + HTF fallback ops: adding these to `dispatch_arith_numpy` would eliminate remaining `compute_series` calls. Small per-call savings.
- LSP + Algo detectors: structural cost of the detectors. Would require rewriting `lsp_detector_v2` / `algo_line_detector`.
- `ext_ceiling_ratio`: goes through dispatch but `_rolling_max` is called per expression on computed series. Diminishing returns.

## 6. Pending build

> **Status note (2026-04-25):** all three sub-features (Extension Chart Levels, Extension Chart Trendlines, MOC) shipped to the worktree branch `feature-build-2026-04-24` on 2026-04-25. Universe rebuild verified: 11,534 tickers × 16,216 expressions (16,039 baseline + 12 Levels + 104 Trendlines + 61 MOC = 16,216). Per CLAUDE.md doc flow ("Completed build items fold into spec"), the three sub-sections below should fold into §2 EXACT spec on the next doc-cleanup pass; left in §6 for now with shipping detail intact.

### Extension Chart Levels — shipped 2026-04-25

Per-ticker constants derived from return-rate curve geometry on the 50-SMA and 200-SMA extension series. Adds **12 new expressions** to the cache (D1 only — D1 50/200 SMA carry the structural TA significance; weekly/monthly extensions of the same MAs do not). Purpose: quality MFE capture in the profit_grinder — single-metric reversal levels produce a "mushy median" that misses ticker-specific structure; the 6-constant profile preserves it.

#### Per-ticker constants (6 per MA, D1 only)

Per MA ∈ {ext50, ext200}, daily timeframe only:

| constant | meaning |
|----------|---------|
| `upside_1` | first upside reversal level — where momentum tends to stop |
| `upside_2` | second upside reversal level — rare / momo territory |
| `downside_1` | first downside reversal level (mirror of upside_1) |
| `downside_2` | second downside reversal level (mirror of upside_2) |
| `chop_upper` | upper edge of chop band (positive magnitude) |
| `chop_lower` | lower edge of chop band (negative magnitude — symmetric around zero) |

**Total:** 6 × 2 MAs = **12 new expressions per ticker** (D1 only).

**Naming convention.** `ext_avgc50_adr14_upside_1`, `..._upside_2`, `..._downside_1`, `..._downside_2`, `..._chop_upper`, `..._chop_lower` (and the same 6 for `ext_avgc200_adr14`). No HTF prefix — registered with `op="precomputed", source="reversal_profile"` so the HTF auto-prefix block in `brute_expressions.generate_all()` excludes them.

#### Return-rate curve (the derivation primitive)

For each (ticker × MA × timeframe), compute the per-bar return-rate curve on the ext series:

- **L grid:** 0.5 to `ceil(max(|ext|)) + 1` in 0.5-ADR steps (typical max: 0.5 to 12.0)
- **Forward window:** 14 bars = the ADR14 period in the metric's denominator (natural timescale tied to the metric's normalization)
- **Upside crossing:** bar t where `vals[t-1] < L ≤ vals[t]` for positive L
- **Upside return:** any bar in `[t, t+14]` where `vals[bar] < L`
- **Downside:** mirror with `vals[t-1] > L ≥ vals[t]` and return condition `vals[bar] > L`
- **Per-L metrics:** total crossings count, rate = returned_count / crossings_count

**Stat window:** expanding from `EXPR_CACHE_START` (2020-01-02) to the current bar. At bar N, the curve uses bars 0..N — no future-leak, historical immutability preserved.

#### Constant derivation (v2 authoritative)

**`upside_1` (first upside reversal level):**
- Normally: first L (ascending) where rate exceeds the median rate of the full curve.
- Saturation fallback: if median rate = observed max (rate saturates at 1.0), `upside_1` = first L where rate reaches observed max. Fixes AAPL/SPY NaN downside issue.
- Rationale: where reversal probability first lifts off chop baseline into sustained territory.

**`upside_2` (second upside reversal / rare territory):**
- L at the knee of the crossings-count decline curve.
- Knee = point of maximum perpendicular distance from the chord connecting the first and last points of the crossings-decline tail (tail starts at argmax of crossings).
- Search constrained to L > upside_1 so knee can't precede onset.
- Concavity-tie fallback: `max_pos < max_neg` (strict `<`, not `≤`) triggers fallback to argmax of the curve. Fixes TSLA `upside_2`.

**`chop_upper`:**
- First genuine local peak in rate curve below `upside_1`, detected via `scipy.signal.find_peaks` with prominence = 2 × std of the pre-peak baseline rate sub-range.
- Returns NaN if no qualifying peak exists.
- Replaces v1's "argmax in L ≤ u1 − 0.5" which picked top-of-region on monotonic curves.

**`downside_1`, `downside_2`, `chop_lower`:** symmetric mirrors on the downside rate curve, returned as negative magnitudes.

**Degenerate cases:**
- If fewer than 3 L values have crossings, return NaN for all 6 constants (insufficient sample).

#### Compute path — incremental running tally (Option C)

**Problem:** naive per-bar curve recomputation is O(N² × L_grid) per ticker per combo — too slow.

**Solution:** maintain running per-L upcrossing + return counts as bars are scanned chronologically.

- For each bar t, scan the L grid once:
  - If bar t upcrosses L, register a pending upcrossing with deadline t+14
  - For each pending upcrossing registered in the past 14 bars, check if bar t triggers its return condition; if yes, increment returned_count for that L
- At each bar, the current cumulative counts give the curve up to that bar
- Derive the 6 constants at every bar from the current curve
- Total ops per ticker per combo: O(bars × L_grid × fwd_window)

**Historical immutability:** curve at bar N only uses bars 0..N, so the 6 constants at bar N never change between rebuilds.

**Nightly append:** extend the running tallies by one bar; recompute 6 constants using new curve. Full rebuild recomputes from scratch.

#### Calibration points (re-locked 2026-04-24 against post-distribution-fix OHLCV)

Values below are derived by `compute_all_reversal_profile_series` on the
post-2026-04-23 OHLCV cache, at asof 2026-04-10 (D1 ext50). Table is the
authoritative validation truth for any future re-derivation. The earlier
table (set against pre-fix OHLCV) is superseded; the AAPL chop band shifted
most because AAPL's pre-fix ext was distorted by un-adjusted distributions.

| ticker | upside_1 | upside_2 | chop band |
|--------|----------|----------|-----------|
| AAPL | +5.5 (saturation; first-lift alt +4.0 from pre-fix data) | +7.5 | −2.5 to +1.5 |
| MSFT | +4.0 | +7.0 | −2.5 to +2.0 |
| TSLA | +5.0 (workable +3.75) | +8.5 | −2.5 to +2.0 |
| RIVN | +4.5 | +6.0 | −3.5 to +1.5 |

**Structural observation (ta_knowledge alignment):** 4.0 and 6.0 are common reversal levels across stocks; big momo stocks hit 7-10 at `upside_2`; fundamental catalyst monsters can reach 13.

**Validation gate post-build:** derived values on these 4 tickers must land within ±0.5 ADR of the calibration, otherwise the derivation needs rework. v2 derivation lands at the calibration values exactly when re-derived on the same post-fix OHLCV cache.

#### Design intent — how grinders use these constants

Grinders compute derived measures on the fly from the 6 constants:
- Distance: `ext − upside_1`, `ext − upside_2`, etc.
- Booleans: `ext > upside_1`, `ext in [chop_lower, chop_upper]`, etc.
- Pressure ratios: `ext / upside_1`, `ext / upside_2`

No pre-computed position expressions in the cache. If a grinder doesn't find them useful for a setup, nothing added to core compute.

#### Rejected alternatives (prevent drift — do not re-litigate)

- **Single-number "reversal level" per side:** rejected as "mushy median nothing number." Two-number framing preserves the structure the grinder needs.
- **"Onset" as name for upside_1:** rejected because onset implies "extension beginning" when it's the opposite — where momentum tends to *stop*. Use plain `upside_1` / `upside_2` naming.
- **Chop band = [downside_1, upside_1]:** DEFINITELY wrong. Chop is narrower and symmetric around zero. Momentum zone exists between chop and reversal.
- **Prominence = 2σ or similar σ-based peak detection:** rejected — doesn't scale cleanly across tickers with different volatility profiles.
- **IQR of |ext| as chop:** rejected as conventional, not data-derived.
- **Expose the full return-rate curve as per-ticker metadata:** rejected — single number (or pair of numbers), not a shape-exposed metadata blob.

#### Authoritative source

Production primitive: `scripts/reversal_profile.py` — Option C running tally + bootstrap/step API for forward-prop. Bit-equivalent to `research/reversal_profile_derive_v2.derive_profile()` at the asof bar (validated on 5 sandbox tickers; the research file remains as the algorithm reference but is not called at runtime).

#### Files touched at ship (2026-04-25)

| file | change |
|------|--------|
| `scripts/reversal_profile.py` | new — primitive + bootstrap + step API |
| `local_runner/brute_expressions.py` | register 12 D1 expressions, op="precomputed", source="reversal_profile" |
| `local_runner/expr_cache_builder.py` | classifier branch + Phase 2d compute block + worktree-aware OHLCV loader |
| `local_runner/vectorized_cache_builder.py` | classifier + per-ticker pass extension |
| `local_runner/forward_prop_engine.py` | `_compute_reversal_profile_expressions` helper + Phase 5 call |
| `scripts/setup_forward_prop.py` | bootstrap `reversal_profile_state` per ext source |
| `_manifest.json` | auto-updates (+12 expressions) |

---

### Extension Chart Trendlines — shipped 2026-04-25

Trendlines drawn on the 50-extension chart, emitted per bar, exposed as expression cache columns so grinders can use line geometry as discriminating technical-analysis features. Each daily candle has its own set of valid trendlines — the set evolves with the ticker's extension history. Uses Extension Chart Levels as input. **104 D1-only expressions (16 numeric × 6 lines + 5 aggregates + 3 pass-throughs).**

**Production primitive:** `scripts/ext50_trendlines.py` — vectorized full-walk via 2D `[pair × bar]` matrices; bit-equivalent to the slow per-bar `cascade_at` reference (kept in same file as `compute_all_ext50_trendline_series_per_pair_loop` for parity testing).

**Research reference:** `swing-screener/research/trendline_primitive_v7_momentum.py`. The file's code (not its docstring) was the algorithm reference. Locked PNG truth set: `research/ext50_trendlines_truth_2026-04-24/ext50_trendlines_{AAPL,CAR,SPY,MSFT,TSLA}_2026-04-10.png` (rendered with the post-port primitive on full-history ext, eyeball-locked by Dan).

#### Inputs at bar t

- 50-extension series for the ticker (daily).
- Extension Chart Levels at bar t: `upside_1`, `upside_2`, `downside_1`, `downside_2`, `chop_upper`.

#### Scope

Daily timeframe only. 50-extension only (no weekly/monthly, no ext200).

#### Window

**Full ext cache history** (since `EXPR_CACHE_START` 2020-01-02) is the lookback at every bar. Earlier 260-bar cap removed 2026-04-24 — no calibration evidence behind that value (research dir has no `trendline_window_sweep.py` or comparison; the value first appeared in v4 as a default function arg with no justification), and the analogous algo lines feature on price uses full history per ta_knowledge.md ("can span thousands of candles, lines from years ago remain relevant if unbroken"). Per-bar `find_peaks` runs on `ext[:t+1]` only — historical immutability.

#### Pivot detection

- Peaks = local maxima via `scipy.signal.find_peaks` with `prominence = 0.5` ADR.
- Troughs = local minima (find_peaks on negated series), same prominence.
- **No slide** — anchors are raw detected pivots only. Slide was removed because it could land on non-pivot bars between real detected pivots during steep descents (observed on SPY during research).
- Restricted to pivots in [0, t]. The fast full-walk pre-computes `t_appear[K]` per pivot (smallest t at which K is detected by `find_peaks(ext[:t+1])`) and gates each pair by `visible_from = max(t_appear[i0], t_appear[i1])`. Pivot detection is empirically monotonic in t on the 5 sandbox tickers (zero disappearances after first detection); production runs assume this holds universe-wide.

#### Candidate enumeration

For each anchor type (peak-anchored pairs, trough-anchored pairs) independently: every ordered pair (i0, i1) of same-type pivots with i1 > i0 is a candidate trendline (line through (i0, v0) and (i1, v1), extended to bar t).

#### Gates — drop candidate if any fail

1. **Min span between anchors:** `i1 − i0 ≥ 15 bars`.
2. **Min span origin-to-asof:** `t − i0 ≥ 15 bars`.
3. **Origin-sign opposition:** descending (`slope < 0`) requires `v0 ≥ 0`. Ascending (`slope > 0`) requires `v0 ≤ 0`.
4. **Same-side anchors:** `sign(v0) == sign(v1)`, both non-zero. Lines spanning `y=0` rejected.
5. **Anchor-type ↔ slope direction:** peak-anchored pairs must be descending (slope < 0). Trough-anchored pairs must be ascending (slope > 0). Collapses valid candidates to wedge geometry — U lines descend from peaks, L lines rise from troughs.
6. **Asymmetric break check:**
   - Descending lines — only origination-side segment (bars where projection has same sign as `v0`) must be unbroken. No sign-flip of `(ext − projection)` over that segment. Pokes in flipped negative-hill role are tolerated.
   - Ascending lines — entire line life from `i0` to `t` must be unbroken. No sign-flip of `(ext − projection)` across full life.
7. **Projection-in-range (cycle containment):** projection at bar t must satisfy `downside_2 ≤ proj ≤ upside_2` (uses Extension Chart Levels data). Rejects wildly extrapolated lines. NaN values in these constants pass-through.

#### Per-candidate emitted data (13 fields per surviving line; 16 numeric columns per slot in cache)

| # | field | meaning |
|---|-------|---------|
| 1 | anchors `(i0, v0, i1, v1)` | bar indices and extension values at the two anchors (expands to 4 cache columns: `anchor_i0`, `anchor_v0`, `anchor_i1`, `anchor_v1`) |
| 2 | slope | ADR per bar |
| 3 | anchor_type | encoded numerically: 0 = peak_anchored (descending) / 1 = trough_anchored (ascending) |
| 4 | proj_asof | line value extended to bar t |
| 5 | signed_dist | `proj_asof − ext[t]` |
| 6 | zero_bar | bar where projection crosses y=0 (−1 if outside [i0, t]) |
| 7 | pos_bars | bars in [i0, t] with projection ≥ 0 |
| 8 | neg_bars | bars in [i0, t] with projection < 0 |
| 9 | touches | pivots within 0.25 ADR of projection, plus 2 anchor touches |
| 10 | last_cross_bar | bar of most recent ext-crosses-line event |
| 11 | last_cross_dir | +1 (ext crossed up through line) / −1 (down through) / 0 (no cross) |
| 12 | total_cross | total cross-event count over line's life up to t |
| 13 | span | `t − i0` |

#### Ranking and emission

- **Upper cascade (U lines)** = peak_anchored descending, sorted by `|signed_dist|` ascending, top 3.
- **Lower cascade (L lines)** = trough_anchored ascending, sorted by `|signed_dist|` ascending, top 3.
- Total emitted per bar: up to 3 U + up to 3 L = up to 6 lines.

#### Per-bar aggregates

- `total_candidates`, `count_descending`, `count_ascending`
- `nearest_descending_dist`, `nearest_ascending_dist` (NaN if side empty)
- Pass-through of Extension Chart Levels used: `upside_1`, `upside_2`, `chop_upper`

#### Constants (locked, ship as-is)

`PROMINENCE = 0.5` ADR, `MIN_SPAN_BARS = 15`, `TOUCH_TOL = 0.25` ADR, `TOP_N = 3`. (Earlier `WINDOW_BARS = 260` removed — see Window section above.)

#### Port-time corrections applied (from research file)

- **Filename rename:** research file `trendline_primitive_v7_momentum.py` → production `scripts/ext50_trendlines.py`. The `v7_momentum` residue from the pre-reframe "Momentum-Channel" sub-feature was eliminated.
- **Dead code removed:** `ANCHOR_SLIDE_BARS = 3` and `slide_to_local_extremum` from research file are NOT in the production primitive.
- **find_peaks history slicing:** research file feeds the full ext array to find_peaks even when computing a snapshot at an earlier bar (lets future bars influence past peak detection). Production primitive slices `ext[:asof_bar+1]` → historical immutability preserved.
- **Reversal profile import:** production primitive uses `scripts/reversal_profile.py` (the production port of v2 derivation), not the research `reversal_profile_derive` module.

#### Implementation

Vectorized full-walk in `compute_all_ext50_trendline_series(ext, levels_at_bar)`: precompute per-pair cumulative arrays once per ticker via 2D `[pair × bar]` numpy matrices, then per-bar emission reads from the matrices. Empirical per-ticker time on 5 sandbox tickers (1583 bars each): 1.0–1.7s for trendlines alone, ~7s for full `_compute_ticker_full`. The slow per-bar `cascade_at` reference is preserved as `compute_all_ext50_trendline_series_per_pair_loop` for parity diffs.

Forward-prop: `cascade_at(ext, asof_bar, levels)` is path-pure (no carried state to roll across bars). State stored in `.state` is just `ext50_trendline_state.ext50_history` (full ext50 history through the .npz end-bar; sliding-appended on each forward-prop call), so the engine doesn't need to load the full .npz to access ext history.

#### Validation gate

PNG pixel-identical replay protocol: run the production primitive at asof 2026-04-10 for AAPL, CAR, SPY, MSFT, TSLA on full ext history (post 2026-04-23 OHLCV distribution-fix). Render via `scripts/render_ext50_trendline_truth.py` to `research/ext50_trendlines_truth_2026-04-24/`. Eyeball-locked by Dan 2026-04-25. Any future re-derivation must reproduce these PNGs pixel-identical.

#### Files touched at ship (2026-04-25)

| file | change |
|------|--------|
| `scripts/ext50_trendlines.py` | new — primitive (vectorized + per-pair-loop reference) + bootstrap + step API |
| `scripts/render_ext50_trendline_truth.py` | new — render utility for the locked PNG truth set |
| `local_runner/brute_expressions.py` | register 104 D1 expressions, op="precomputed", source="ext50_trendlines" |
| `local_runner/expr_cache_builder.py` | classifier branch + Phase 2e compute block (after Levels) |
| `local_runner/vectorized_cache_builder.py` | classifier + per-ticker pass extension (Trendlines reads in-memory rp_outputs to avoid double-reading the .npz) |
| `local_runner/forward_prop_engine.py` | `_compute_ext50_trendline_expressions` helper + Phase 5 call (after reversal_profile) |
| `scripts/setup_forward_prop.py` | bootstrap `ext50_trendline_state.ext50_history` from .npz ext50 column |
| `_manifest.json` | auto-updates (+104 expressions) |

---

### MOC (Market-On-Close) levels — shipped 2026-04-25

D1 horizontal support/resistance levels from high-RVOL candle H/L prints. 61 per-ticker expressions, D1 only. Concept: highs and lows printed on above-average-volume days cluster at specific prices and act as S/R on intraday (5m) charts. The cache emits a per-bar snapshot of the top-weighted levels above and below current close, plus zone-level composites describing the overall landscape.

#### Level construction

- **RVOL** = `volume / SMA(volume, 50)`.
- **Birth**: every D1 H and L with RVOL > 1 that is NOT within tolerance of an existing active level spawns a new level at that price. The RVOL > 1 gate captures the "above-average-volume days only" semantic; low-volume H/L prints are noise.
- **Tolerance**: `ATR14[bar] × √(1/78) ≈ 0.113 × ATR14`. Empirically robust — signal metrics are flat across a 100× tolerance sweep (0.005 to 0.5 ATR14, ≤ 10% aggregate variation), with no clear optimum. This specific value ships as the empirically-flat choice; the feature is not sensitive to it.
- **Stacking**: an H or L within tolerance of an existing level adds `rvol` (raw, no subtraction) to that level's `stack_weight`, increments `stack_count`, updates `max_contributor_rvol` if exceeded, and updates `last_contribution_bar`.
- **Cross event**: D1 close crosses to opposite side of level beyond tolerance → `cross_count` increments. Raw feature, no decay applied.

#### Per-bar emission — 61 expressions

**Per-level features (9 per level × 3 slots × 2 sides = 54):**

Top 3 levels above current close and top 3 below, ranked by `stack_weight`. Each slot emits:

| feature | meaning |
|---|---|
| `distance` | `(level_price − close) / ATR14`, signed |
| `stack_weight` | sum of contributor RVOLs (raw) |
| `stack_count` | count of contributors |
| `max_contributor_rvol` | largest single-day RVOL that built the level |
| `cross_count` | times D1 close has crossed beyond tolerance since birth |
| `bars_since_birth` | level age in D1 bars |
| `bars_since_last_contribution` | D1 bars since last stack event |
| `max_abs_beyond_atr` | max over all bars since birth of `abs(close − level_price) / ATR14` — how decisively close has ever gotten past the level |
| `contact_count` | bars post-birth whose HL range intersected the level, resolved with 5-bar forward window |

**Per-bar composite features (7):**

| feature | meaning |
|---|---|
| `total_weight_above` | sum of stack_weight across all active levels above close |
| `total_weight_below` | sum of stack_weight across all active levels below close |
| `n_levels_above` | active level count above close |
| `n_levels_below` | active level count below close |
| `n_levels_within_2atr` | active levels within 2 × ATR14 of close |
| `top1_spread_atr` | distance between highest-weighted above level and highest-weighted below level, in ATR14 units |
| `weight_asymmetry` | `(total_weight_above − total_weight_below) / (total_weight_above + total_weight_below)` |

**Total: 61 expressions per ticker per bar. D1 only — no HTF prefix, no weekly/monthly variants.**

#### Compute path — stateful per-bar walk

Maintain per-level state during the D1 chronological walk. Each bar updates:

- Birth / stack on RVOL > 1 H and L prints (within-tolerance → stack; otherwise → birth).
- `cross_count` via close-side-change logic beyond tolerance.
- `max_abs_beyond_atr` via running max of `abs(close − price) / ATR14`.
- `contact_count` updated retroactively at bar `t` for contacts detected at `t − 5` (approach side = `sign(close[t−6] − price)`, outcome at bar `t`). Five-bar forward window establishes the contact direction unambiguously and prevents forward leak at any snapshot.

At each bar, sort active levels by `stack_weight`, take top 3 above and top 3 below current close, compute the 54 per-level features + 7 per-bar composites from the full levels list.

**Historical immutability preserved**: snapshot at bar N uses only state built from bars 0..N, so feature values at bar N never change between rebuilds. Nightly append extends per-level state by one bar.

Implementation cost target: ~0.3s/ticker.

#### Usefulness — empirical justification

Tested against 111 breakout-setup examples (HTF/BF/BASE from the examples SQLite table) with 3,002 tradable-universe null bars over matching date range.

| feature slice | top-5 KS mean | top-10 \|ρ\| mean | combined |
|---|---|---|---|
| 42-feature baseline (7 per level × 6 slots) | 0.286 | 0.301 | 0.587 |
| + `max_abs_beyond_atr` per level (48) | 0.286 | 0.342 | 0.628 |
| + 7 composites (49) | 0.309 | 0.302 | 0.611 |
| + `contact_count` per level (60) | 0.305 | 0.309 | 0.614 |
| **61-feature full spec** | **0.322** | **0.348** | **0.669** |

+14% relative lift over baseline. Top individual features include `above_N_max_abs_beyond_atr` carrying |ρ| up to +0.45 against forward 20-day move, and `n_levels_below` / `weight_asymmetry` each carrying KS ≥ 0.30.

#### Consumer pattern

Grinders load the 61 expressions per ticker via `load_ticker_cache()` — they appear as regular expression columns: `moc_above_1_distance`, `moc_above_1_stack_weight`, ..., `moc_above_1_max_abs_beyond_atr`, `moc_above_1_contact_count`, ... `moc_composite_total_weight_above`, ..., `moc_composite_weight_asymmetry`. Downstream grinders derive boolean conditions (`moc_above_1_distance < 0.5`, `moc_composite_weight_asymmetry > 0.3`) directly from these columns.

#### Rejected alternatives (do not re-litigate)

- Volume-profile smearing (levels aren't fuzzy zones; 5m chart shows exact bounces).
- Binary "active vs dead on first cross" (wrong).
- Exponential decay with picked coefficient (no punts).
- Survival-curve `S(N)` decay applied to stack_weight — data doesn't support. Retest-survival research (`research/retest_survival_extended.py`, `research/retest_survival_v3_decisive.py`) showed flat P(bounce) across cross_count N, for both loose and strict cross definitions.
- Ranking by proximity (ticker with full history = levels everywhere, proximity ranking becomes trivial).
- Including close as anchor — only H and L act as S/R, not close.
- HTF (weekly/monthly) variants — D1 only.
- `bounce_rate` and `avg_bounce_atr` per-level features — tested, NaN-dominated (most levels have zero contacts in sample), don't rank in signal tests. `contact_count` alone carries the bounce-history signal.
- Contrast-amplifying weight functions (`rvol^1.5`, `rvol^2`, `rvol^3`, `exp(rvol−1)`, `exp(rvol/2)`, `exp(rvol)`) — tested at two tolerances, all underperform linear `w = rvol`.
- `max(0, rvol − 1)` weight subtraction — redundant punt; raw `rvol` performs equivalently, removes the subtraction punt.
- Data-derived tolerance from pairwise H/L distance distribution — attempted; distribution is a smooth monotone decay with no natural gap. 100× tolerance sweep shows aggregate signal is flat across the range. Tolerance is empirically insensitive, not data-derivable by this methodology; the `√(1/78)` value ships as the middle-of-plateau choice.

#### Files touched at ship (2026-04-25)

| file | change |
|---|---|
| `scripts/moc_detector.py` | new — primitive (`compute_all_moc_series`) + bootstrap + step API. Uses pandas `Series.rolling(p, min_periods=1).mean()` for ATR14/RVOL to bit-match the research file's float behavior. |
| `local_runner/brute_expressions.py` | register 61 D1 expressions (54 per-level + 7 composites), op="precomputed", source="moc" |
| `local_runner/expr_cache_builder.py` | classifier branch + Phase 2c compute block (no HTF) |
| `local_runner/vectorized_cache_builder.py` | classifier + per-ticker pass extension |
| `local_runner/forward_prop_engine.py` | `_compute_moc_expressions` helper + Phase 5 call |
| `scripts/setup_forward_prop.py` | bootstrap `moc_levels` (variable-length list of active level dicts) |
| `_manifest.json` | auto-updates (+61 expressions) |
