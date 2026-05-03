# ScanPerfect — Dependency Map

**Built from:** Reading every active `.py` file's imports, file I/O, and function calls.
**Purpose:** Before changing any component, check its downstream consumers here. If you change outputs, every downstream consumer must be verified.

For data schemas and format rules, see `DATA_CONTRACT.md`.

---

## How to Use This Document

1. Find the component you're about to change
2. Check **Downstream Consumers** — these are the files that read your component's output
3. Before pushing, verify those consumers won't break
4. The **auditor** uses this map to automatically pull downstream files for review

---

## Layer 1 — Data Acquisition

These are the only components that make network calls for market data.

### cache_builder.py
**Location:** `local_runner/cache_builder.py`
**Spec:** `NIGHTLY_REFRESH.md`, `LOCALIZE.md`
**What it does:** Downloads OHLCV from EODHD API, stores as pickles. Universe sourced from EODHD exchange symbol list: Common Stock + ETF on NYSE/NASDAQ/NYSE ARCA/BATS. No local DB dependency for ticker list. Nightly append detects IPOs (new tickers) and delistings (removed tickers) automatically. All fetches use explicit `start=HISTORY_START` (2016-01-01). OHLCV adjustment: `ratio = adjusted_close / close` applied to O/H/L/C (split + dividend adjusted). Validated: SPY fetched first as ground truth, every ticker's bar count must exactly match SPY's count from `max(firstTradeDate, HISTORY_START)`. Mismatches retry until pass. Split detection on nightly append via adjusted_close comparison — tickers whose adjustment changed get full refetch. No bar minimum — new IPOs included immediately regardless of history length.

**Inputs:**
- EODHD API (network) — `exchange-symbol-list/US` endpoint for universe (1 call)
- EODHD API (network) — via `_eodhd_download(ticker, start, interval)` using explicit start dates
- EODHD API (network) — for first trade date (first bar date from narrow range fetch)
- `EODHD_API_TOKEN` environment variable (required)
- `local_runner/cache/ticker_reference.json` (for validation — expected bar counts)

**Outputs:**
- `local_runner/cache/universe_ohlcv.pkl` — 300-bar daily (legacy)
- `local_runner/cache/universe_ohlcv_daily.pkl` — full history daily (from HISTORY_START)
- `local_runner/cache/universe_ohlcv_weekly.pkl` — weekly (from HISTORY_START)
- `local_runner/cache/universe_ohlcv_monthly.pkl` — monthly (from HISTORY_START)
- `local_runner/cache/ticker_reference.json` — first trade date per ticker (for validation)
- `local_runner/cache/cache_meta.txt`, `cache_daily_meta.txt`, `cache_weekly_meta.txt`, `cache_monthly_meta.txt`

**Key functions called by others:**
- `check_freshness()` — called by `nightly.py` step 1 (alias `check_yfinance_freshness` for compat)
- `append_daily_cache()` — called by `nightly.py` step 2
- `append_weekly()` — called by `nightly.py` step 3
- `append_monthly()` — called by `nightly.py` step 4
- `load_cache()`, `load_daily_cache()` — called by `matrix_builder.py` fallback path
- `fetch_eodhd_universe()` — hits EODHD exchange-symbol-list, returns sorted ticker list (source of truth)
- `get_tradable_tickers_local()` — DEPRECATED, reads SQLite, kept for backward compat only
- `_batched_fetch()` — shared adaptive rate limiter (scales workers + sleep based on failure rate, retry sweeps). Used by all fetch paths.

**CLI:**
- `--sync` — sync universe against EODHD: add new tickers, remove delisted, across daily + weekly + monthly
- `--build-reference [--force]` — build/rebuild ticker_reference.json (first trade dates from EODHD)
- `--daily [--force]` — build/rebuild daily cache (validated against SPY + reference)
- `--htf [--force]` — build/rebuild weekly + monthly caches (full sweep; --force discards existing)
- `--weekly [--force]` — build/rebuild weekly cache only (full sweep; --force discards existing)
- `--monthly [--force]` — build/rebuild monthly cache only (full sweep; --force discards existing)
- `--all [--force]` — daily + weekly + monthly in one command
- `--status` — show daily + HTF cache status
- `--daily-status` — show daily cache status only
- `--htf-status` — show HTF cache status only

**--force behavior (consistent across all timeframes):**
- With `--force`: discard existing cache, fetch all tickers from scratch
- Without `--force`: load existing cache, fetch only stale/missing tickers
- Nightly append paths (called by nightly.py) never use force — always load + append

**Downstream Consumers (if you change pickle format or file paths, these break):**
- `expr_cache_builder.py` — reads daily + weekly + monthly pickles
- `vectorized_cache_builder.py` — reads daily pickle
- `matrix_builder.py` — reads daily pickle (fallback path)
- `pyramid_grinder.py` — reads daily pickle
- `signal_filter.py` — reads daily pickle
- `signal_exit_grinder.py` — reads daily pickle
- `entry_grinder.py` — reads daily pickle
- `exit_grinder.py` — reads daily pickle
- `ev_grinder.py` — reads daily pickle
- `profit_grinder.py` — reads daily pickle
- `scanperfect.py` — reads daily pickle into memory
- `server.py` — reads daily pickle (local mode)
- `fetch_fundamentals.py` — reads daily pickle for ticker list

---

### fetch_fundamentals.py
**Location:** `scripts/fetch_fundamentals.py`
**Spec:** `NIGHTLY_REFRESH.md`
**What it does:** Scrapes Yahoo Finance for sector, shares outstanding, float.

**Inputs:**
- Yahoo Finance API (network — custom urllib session with crumb auth)
- daily OHLCV pickle (for ticker list)

**Outputs:**
- `local_runner/cache/fundamentals_cache.json`

**Called by:** `nightly.py` step 9

**Downstream Consumers:**
- `ev_grinder.py` — reads `fundamentals_cache.json` for sector data in setup features

---

### build_tradable.py
**Location:** `scripts/build_tradable.py`
**What it does:** Rebuilds `tradable_universe` table in SQLite from `universe_ohlcv` data.

**Inputs:**
- SQLite `universe_ohlcv` table (Railway-side)

**Outputs:**
- SQLite `tradable_universe` table

**Downstream Consumers:**
- `cache_builder.py` — NO LONGER reads `tradable_universe` (uses EODHD exchange-symbol-list instead)
- `nightly.py` step 6 (matrix rebuild uses universe)

---

### fetch_universe.py
**Location:** `scripts/fetch_universe.py`
**What it does:** Fetches full NYSE+NASDAQ ticker list from NASDAQ FTP, stores in SQLite. Runs on Railway.

**Inputs:**
- NASDAQ FTP (network)
- yfinance (for OHLCV batch download)

**Outputs:**
- SQLite `universe_tickers` table
- SQLite `universe_ohlcv` table

**Downstream Consumers:**
- `build_tradable.py` — reads `universe_ohlcv` to build tradable list

---

### market_cache_builder.py
**Location:** `local_runner/market_cache_builder.py`
**Spec:** `LOCALIZE.md`
**What it does:** Fetches OHLCV for ~264 market instruments from four sources, then computes expression series for each. Data sources: (1) local daily pickle for ~227 US ETFs/stocks, (2) EODHD INDX/CC for indices, crypto, and breadth internals, (3) yfinance for futures only, (4) FRED for macro series. Three derived instruments (NYMO_CALC, NYUD_CALC, NDXADP_CALC) are computed from fetched breadth components after all fetches complete.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl` — for ~227 ETF/stock instruments (Phase 1 pickle reads)
- EODHD API (network) — INDX exchange for indices + breadth, CC exchange for crypto (Phase 1 fetch)
- yfinance (network) — futures only: ES=F, NQ=F, etc. (Phase 1 fetch)
- FRED API (network) — macro series: yield spreads, credit spreads, NFCI, etc. (Phase 1 fetch)
- `EODHD_API_TOKEN` environment variable (required for EODHD instruments)
- `brute_expressions.py` — expression library (Phase 2 compute)
- `scripts/expression_engine.py` — ExpressionEngine class
- `scripts/backtest_conditions.py` — compute_series()
- `expr_cache_builder.py` — numpy helper functions

**Outputs:**
- `local_runner/cache/market_ohlcv.pkl` — raw OHLCV for all ~264 instruments
- `local_runner/cache/market_series/*.npz` — per-instrument expression series
- `local_runner/cache/market_series/_manifest.json`

**Called by:** `nightly.py` step 8 (via `append_new_bars`)

**Key functions called by others:**
- `instrument_filename()` — used by `ev_grinder.py` (duplicated there as `_instrument_filename`, must stay in sync)
- `all_instruments()` — used by `fetch_missing_market.py`
- `_fetch_one()` — used by `fetch_missing_market.py`

**Downstream Consumers:**
- `ev_grinder.py` — reads `market_series/*.npz` + `_manifest.json` for correlative screening; has its own copy of `_instrument_filename()` that MUST match
- `fetch_missing_market.py` — reads/writes `market_ohlcv.pkl`, imports `_fetch_one` and `all_instruments`

---

## Layer 2 — Precomputation

### brute_expressions.py
**Location:** `local_runner/brute_expressions.py`
**Spec:** `EXPRESSION_ENGINE_V2.md`
**What it does:** Generates the master list of ~16,216 expressions (the "expression library"; post-2026-04-25 feature-build adds +61 MOC + 12 Levels + 104 Trendlines on top of the prior 16,039). Pure computation, no I/O dependencies.

**Inputs:**
- `scripts/lsp_detector_v2.py` — `get_lsp_expression_names()` (for LSP expression names)
- `scripts/algo_line_detector.py` — `get_algo_expression_names()` (for algo line names)

**Outputs:**
- `local_runner/cache/brute_expressions.json` (cached copy)
- In-memory list returned by `generate_all()`

**Key function:** `generate_all()` — called by almost everything

**Downstream Consumers (if expression list changes, everything must rebuild):**
- `expr_cache_builder.py` — imports `generate_all`, uses fingerprint to detect changes
- `vectorized_cache_builder.py` — imports `generate_all`
- `market_cache_builder.py` — imports `generate_all`
- `matrix_builder.py` — calls `generate_all` via `_load_expressions()`
- `pyramid_grinder.py` — imports `generate_all`

**CRITICAL:** Changing the expression library triggers a full rebuild of expr cache (~163 GB), market cache, and universe matrix. The fingerprint system in expr_cache_builder detects this automatically.

---

### expr_cache_builder.py
**Location:** `local_runner/expr_cache_builder.py`
**Spec:** `EXPRESSION_ENGINE_V2.md`
**What it does:** Computes all expression values for every ticker across daily/weekly/monthly timeframes. One `.npz` file per ticker. Uses ProcessPoolExecutor.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl` (daily OHLCV)
- `local_runner/cache/universe_ohlcv_weekly.pkl` (weekly OHLCV, optional — resamples from daily if missing)
- `local_runner/cache/universe_ohlcv_monthly.pkl` (monthly OHLCV, optional — resamples from daily if missing)
- `brute_expressions.py` — `generate_all()` for expression list
- `scripts/exit_expressions.py` — `generate_exit_expressions()` for extension expressions
- `scripts/expression_engine.py` — ExpressionEngine class
- `scripts/backtest_conditions.py` — `compute_series()`, `compute_on_series()`
- `scripts/lsp_detector_v2.py` — `compute_all_lsp_series()`
- `scripts/algo_line_detector.py` — `compute_all_algo_series()`
- `scripts/profiling_engine.py` — TA indicator functions
- `scripts/setup_forward_prop.py` — generates .lookback + .state files (one-time setup for forward-prop)

**Outputs:**
- `local_runner/cache/expr_series/{TICKER}.npz` — per-ticker expression values. Contains `data` (float16 array on disk, shape `n_bars × n_expressions`) and `dates` (date strings). Cast to float32 on load via `load_ticker_cache()` — all consumers see float32 transparently. Storage dtype is float16 to halve disk usage.
- `local_runner/cache/expr_series/{TICKER}.append` — incremental append rows (raw float16 binary, one row per nightly append, wider than .npz: 16,216 + 196 = 16,412 cols post-2026-04-25 feature-build)
- `local_runner/cache/expr_series/{TICKER}.append_dates` — date strings for appended rows (one per line)
- `local_runner/cache/expr_series/{TICKER}.lookback` — last 504 rows × 196 intermediate columns (float16 raw binary, generated by `setup_forward_prop.py`, sliding-window updated by nightly append)
- `local_runner/cache/expr_series/{TICKER}.state` — forward-prop computation state (JSON, 317 float64 values, overwritten nightly)
- `local_runner/cache/expr_series/_manifest.json` — metadata (expression fingerprint, dates, counts)

**Key functions called by others:**
- `append_new_bars()` — manual/occasional operation. Uses `forward_prop_engine._forward_prop_one_ticker` for existing tickers with .state files (writes .append, updates .lookback + .state), falls back to `_compute_and_save_ticker` for new tickers (writes .npz). **NOT called by nightly.py** — nightly now uses `intermediate_cache_builder` + `scan_engine` instead (see `nightly.py` entry below). The forward-prop engine is a **best-effort fast path**: per-expression compute failures silently leave cells as NaN. Consequence: `.append` rows may have NaN cells that the full rebuild path would not have produced. For consensus grinding, always use `build_full()` — do NOT rely on appended rows.
- `build_full()` — full rebuild from scratch via `_compute_ticker_full` (ground-truth path). Clears all .append/.append_dates/.lookback/.state files before computing. This is the only path that guarantees every expression is computed from the full OHLCV history for every bar. Required before any consensus pipeline run. HTF pickles are loaded once by the orchestrator and passed per-ticker to workers — no Monday/month-start skip logic is applied because workers receive the full closed HTF history directly.
- `load_ticker_cache(ticker)` — called by grinders to load individual ticker data. Reads `.npz` + `.append` if present, concatenates dates and vstacks data, returns combined `(dates, data)` as shape `(n_bars_total, n_expressions)` float32. `.append` is 16,001 cols wide (15,805 expressions + 196 intermediates for forward-prop state) — sliced to first 15,805 before return. **Consumer contract unchanged by forward-prop rollout:** same `(dates, data)` tuple as before, same column indices; extra intermediate columns in `.append` are stripped transparently. Any NaN cells left by the best-effort `.append` path are surfaced to the consumer.
- **Load-path audit surface:** all grinder consumers go through `ExprSeriesCache.get_ticker()` → `load_ticker_cache()`. The one exception is `signal_filter._load_ticker_npz()`, which bypasses `ExprSeriesCache` but mirrors `load_ticker_cache()` logic (reads `.npz` + `.append` + `.append_dates` and concatenates). If the load path changes, these are the two sites to audit.
- `save_ticker_cache(ticker, dates, data)` — saves one ticker's npz
- `ExprSeriesCache` class — high-level accessor used by grinders

**Downstream Consumers:**
- `matrix_builder.py` — reads `.npz` files via `ExprSeriesCache` to build universe matrix
- `pyramid_grinder.py` — reads `.npz` files for signal/refinement grinding
- `signal_filter.py` — reads `.npz` files for full universe scan
- `signal_exit_grinder.py` — reads `.npz` files for exit condition search
- `entry_grinder.py` — reads `.npz` via `ExprSeriesCache`
- `entry_candle_scorer.py` — reads `.npz` via `ExprSeriesCache`
- `ev_grinder.py` — reads `.npz` via `ExprSeriesCache` for replay
- `profit_grinder.py` — reads `.npz` via `ExprSeriesCache`

**CRITICAL:** If `.npz` format changes (column order, expression count, data type), every grinder breaks silently — they'll read wrong values with no error.

**`.state` schema (forward-prop state file contents):**

*Spec categorization below totals ~212 float64 values; the live `.state` file carries 317. The category shape is authoritative; the count gap is absorbed by ranges that have grown since the spec was written (e.g. rolling max/min window set). Reconcile on next forward-prop revisit.*

Categories of state needed for forward computation:

**Cumulative sums (for SMA-based indicators):**
- `cumsum_close`, `cumsum_volume`, `cumsum_hl` (for ADR), `cumsum_tr` (for ATR), `cumsum_bop_raw` (for BOP SMA), `cumsum_mfv` (for CMF), `cumsum_abs_diff` (for Kaufman efficiency), `cumsum_tp` (typical price for CCI), `cumsum_c2` (close squared for Bollinger stddev).

**EMA states:**
- `xavgc{p}` for p in [5, 8, 9, 10, 12, 13, 20, 21, 30, 50, 65, 100, 150, 200] — 14 values, float64 precision for forward computation.

**RSI internals:**
- `rsi_avg_gain_{p}`, `rsi_avg_loss_{p}` for p in [5, 7, 9, 14, 21, 28] — 12 values. Wilder smoothing: `avg_gain[i] = (avg_gain[i-1] × (p-1) + gain) / p`. Cannot be recovered from the RSI value alone without precision loss.

**ADX chain:**
- `ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` for p in [7, 10, 14, 20] — 12 values.

**MACD signal line:**
- `macd_signal_{fast}_{slow}` for 5 MACD pairs — 5 values.

**Stochastic raw_k:**
- `raw_k_{p}` for p in [3, 5, 7, 9, 10, 14, 21, 28, 50] — 9 periods × 2 previous values per period = 18 values. Stochastic uses 3-bar SMA of raw_k, needs previous 2 raw_k values.

**Rolling max/min tracking:**
- `maxh_idx_{p}` for 29 maxH periods — bar index where current max occurred.
- `minl_idx_{p}` for 19 minL periods.
- `maxc_idx_{p}` for 3 maxC periods.
- Total: 51 values. Update rule: if new bar's value ≥ current max, update index to current bar. If current max's index is falling out of the window (`idx < current_bar - period`), rescan the window from cached data (rare, tiny scan).

**Aroon tracking:**
- `aroon_maxh_idx_{p}`, `aroon_minl_idx_{p}` for 7 periods — 14 values. Same pattern as rolling max/min.

**OBV:**
- `obv` — cumulative, only previous value needed.

**HTF partial candle state (per timeframe: weekly + monthly):**
- `htf_{w,m}_partial_{open,high,low,close,volume}` — 10 values.
- `htf_{w,m}_period_id` — 2 values (which week/month number we're in).
- `htf_{w,m}_xavgc{p}` — EMA states, 14 × 2 = 28 values.
- `htf_{w,m}_ema_dmp_{p}`, `ema_dmm_{p}`, `ema_dx_{p}` — 12 × 2 = 24 values.
- `htf_{w,m}_obv` — 2 values.
- `htf_{w,m}_cumsum_*` — 11 cumsums × 2 = 22 values.
- `htf_{w,m}_macd_signal_*` — 5 × 2 = 10 values.
- Total HTF state: ~98 values.

**LSP state:**
- Serialized pivot data: active pivot prices, break counts, bar indices. Variable length — stored as a separate small binary blob or JSON. Updated each bar: check if new pivot formed, update break counts.

**Algo line state:**
- Serialized trendline data: slope, intercept, volume, bar index. Similar to LSP — small variable-length blob.

Approximate total scalar state size: ~212 float64 values (plus variable-length LSP/algo blobs), ~3 KB per ticker.

---

### setup_forward_prop.py
**Location:** `scripts/setup_forward_prop.py`
**Spec:** `FORWARD_PROP_SPEC.md`
**What it does:** One-time setup for forward-propagation incremental append. Generates .lookback and .state files for all tickers from existing .npz + OHLCV data. Run once after a full expr cache rebuild to bootstrap the four-file system.

**Inputs:**
- `local_runner/cache/expr_series/{TICKER}.npz` — existing expression cache (read-only, for bar count + extension series columns)
- `local_runner/cache/universe_ohlcv_daily.pkl` — daily OHLCV (truncated to .npz bar count)
- `local_runner/cache/universe_ohlcv_weekly.pkl` — weekly HTF OHLCV
- `local_runner/cache/universe_ohlcv_monthly.pkl` — monthly HTF OHLCV
- `scripts/expression_engine.py` — ExpressionEngine class
- `scripts/profiling_engine.py` — TA indicator functions (ema, rolling_max, rolling_min, sma)
- `local_runner/expr_cache_builder.py` — `build_numpy_intermediates()`, `_load_expressions()`, `resample_ohlcv()`
- `local_runner/brute_expressions.py` — `generate_all()` (for expression library + extension series indices)

**Outputs:**
- `local_runner/cache/expr_series/{TICKER}.lookback` — last 504 rows × 196 intermediate columns, float16 raw binary
- `local_runner/cache/expr_series/{TICKER}.state` — JSON, 317 float64 state values (daily + HTF + ext struct)

**CLI:**
- `--ticker AAPL` — single ticker mode (no multiprocessing)
- `--limit N` — random sample of N tickers (seed=42)
- `--workers N` — worker count (default 14)

**Downstream Consumers:**
- `forward_prop_engine.py` — `_forward_prop_one_ticker()` reads .lookback + .state (engine built 2026-04-07, Gate 1 passes)
- `build_full()` — must delete .lookback + .state files alongside .append/.append_dates on full rebuild

---

### vectorized_cache_builder.py
**Location:** `local_runner/vectorized_cache_builder.py`
**What it does:** Alternative (faster) builder for the same `.npz` expr cache. Produces identical output to `expr_cache_builder.py`. Uses 2D numpy arrays across all tickers simultaneously.

**Inputs:**
- Same as `expr_cache_builder.py`
- `vectorized_dispatch.py` — 2D computation functions
- `vectorized_indicators.py` — 2D TA indicator functions

**Outputs:**
- Same `.npz` files and `_manifest.json` as `expr_cache_builder.py`

**Downstream Consumers:** Same as `expr_cache_builder.py`

---

### vectorized_dispatch.py
**Location:** `local_runner/vectorized_dispatch.py`
**What it does:** Pure computation — dispatches expression computations in 2D (all tickers at once). No file I/O.

**Inputs:** `vectorized_indicators.py` functions

**Called by:** `vectorized_cache_builder.py`

---

### vectorized_indicators.py
**Location:** `local_runner/vectorized_indicators.py`
**What it does:** Pure computation — 2D versions of all TA indicators (SMA, EMA, RSI, ATR, etc.). No file I/O.

**Called by:** `vectorized_dispatch.py`, `vectorized_cache_builder.py`

---

### intermediate_cache_builder.py
**Location:** `local_runner/intermediate_cache_builder.py`
**What it does:** Computes 196 indicator intermediates per ticker (pure numpy), writes compact `.im` binary files. Full rebuild is ~1.7 min for all tickers (14 workers). Replaces the 16,000-column expression cache for daily nightly use — expressions are derived on-the-fly from intermediates by `scan_engine.py`.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl` (daily OHLCV — read via `load_daily_cache()`)

**Outputs:**
- `local_runner/cache/intermediate_series/{TICKER}.im` — binary file: 4-byte uint32 header (row count), then `n_rows x 196 x float16` data, then `n_rows x 10` bytes of date strings (YYYY-MM-DD)

**Key functions called by others:**
- `build_full(daily_cache, workers=14)` — called by `nightly.py` step 5a
- `read_im_as_dict(filepath)` — returns `({col_name: float32 array}, date_list)`, called by `scan_engine.py`
- `load_daily_cache()` — worktree-aware OHLCV loader, called by `scan_engine.py`
- `compute_intermediates(close, open_, high, low, volume)` — returns dict of 196 float64 arrays
- `build_intermediate_column_order()` — returns ordered list of 196 column names

**CLI:**
- `--build` — full rebuild of all tickers
- `--validate AAPL` — spot-check one ticker
- `--compare AAPL` — compare numpy vs pandas ExpressionEngine

**Downstream Consumers (if .im format or column order changes, these break):**
- `scan_engine.py` — reads `.im` files via `read_im_as_dict()`
- `materialize_for_consensus.py` — imports validation helpers (does NOT read .im files for materialization)

---

### scan_engine.py
**Location:** `local_runner/scan_engine.py`
**What it does:** Standalone live-scan tool. Evaluates locked scan conditions against intermediate cache for daily signal detection. Classifies conditions into dispatch (fast, from intermediates), other (ExpressionEngine fallback), and LSP (expensive, late-eval). Dispatch conditions that fail automatically defer to ExpressionEngine.

**Not invoked by `nightly.py`** — nightly is infrastructure-only. Run scan_engine via its own CLI when you want a watchlist.

**Inputs:**
- `local_runner/cache/intermediate_series/{TICKER}.im` — via `read_im_as_dict()`
- `data/pyramid_results_{setup_type}.json` — locked conditions from grinder
- `local_runner/cache/universe_ohlcv_daily.pkl` — for ExpressionEngine fallback + LSP conditions

**Outputs:**
- Signal list (in-memory): `[{ticker, date, close}, ...]` printed to stdout

**Key functions called by others:**
- `scan_setup(setup_type, daily_cache, workers=14)` — invoked by CLI
- `load_conditions(setup_type)` — loads from pyramid_results JSON
- `classify_conditions(conditions)` — splits into dispatch/other/LSP buckets
- `validate_scan(setup_type, daily_cache)` — compares results vs `signal_filter.py`

**CLI:**
- `--setup dtss` — scan one setup type
- `--all` — scan all discovered setups
- `--validate dtss` — validate against signal_filter.py (requires valid expr cache)

**Downstream Consumers:**
- None automated. Manual CLI use only.

---

### materialize_for_consensus.py
**Location:** `local_runner/materialize_for_consensus.py`
**What it does:** Derives all ~16,000 expressions from OHLCV and saves as `.npz` files for quarterly consensus grinding. Uses the same computation pipeline as `expr_cache_builder.py` — the `--all` path delegates to `expr_cache_builder.build_full(force=True)`. The `--ticker` path has an independent per-ticker implementation for testing.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`, `universe_ohlcv_weekly.pkl`, `universe_ohlcv_monthly.pkl`
- `brute_expressions.py` — `generate_all()` for expression list
- `scripts/exit_expressions.py` — `generate_exit_expressions()` for extension expressions
- `expr_cache_builder.py` — `build_full()`, `dispatch_arith_numpy()`, numpy helper functions
- `scripts/expression_engine.py`, `scripts/backtest_conditions.py`, `scripts/lsp_detector_v2.py`, `scripts/algo_line_detector.py`

**Outputs:**
- `local_runner/cache/expr_series/{TICKER}.npz` — same format as `expr_cache_builder.py` output
- `local_runner/cache/expr_series/_manifest.json` — ExprSeriesCache-compatible manifest

**Key functions called by others:**
- `materialize_all()` — delegates to `expr_cache_builder.build_full(force=True)`
- `materialize_one_ticker()` — per-ticker computation (for `--ticker` and `--validate` modes)
- `validate_against_existing(ticker, daily_cache, expressions)` — compares materialized vs existing `.npz`

**CLI:**
- `--all` — materialize all tickers (delegates to expr_cache_builder)
- `--ticker AAPL` — materialize one ticker
- `--validate AAPL` — compare against existing .npz at float16 precision

**Downstream Consumers:**
- All grinders that read `.npz` via `ExprSeriesCache` (unchanged — same file format)

---

### matrix_builder.py
**Location:** `local_runner/matrix_builder.py`
**Spec:** `LOCALIZE.md`
**What it does:** Builds the universe matrix (last bar of every ticker's expression values) from the expr cache. Also builds example matrices.

**Inputs:**
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `brute_expressions.py` — `generate_all()` for expression list
- `local_runner/cache/universe_ohlcv.pkl` or `universe_ohlcv_daily.pkl` (fallback path only)
- Railway API (for example matrices — fetches example metadata)

**Outputs:**
- `local_runner/cache/universe_matrix.pkl` — {n_universe} tickers × {n_exprs} expressions
- `local_runner/cache/example_matrix_{setup}.pkl` — per-setup example matrices

**Key functions called by others:**
- `get_universe_matrix()` — called by `nightly.py` step 6, `pyramid_grinder.py`
- `get_example_matrix()` — called by legacy paths
- `_universe_matrix_fresh()` — called by `agent.py` to check staleness

**Downstream Consumers:**
- `pyramid_grinder.py` — reads `universe_matrix.pkl` for D1 tier prefiltering
- `agent.py` — checks matrix freshness

---

## Layer 3 — Grinders (Analysis Pipeline)

### pyramid_grinder.py
**Location:** `local_runner/pyramid_grinder.py`
**Spec:** `SIGNAL_GRINDER.md`, `REFINEMENT_GRINDER.md`
**What it does:** The main grinder. Three jobs: (1) signal grind — beam search for conditions, (2) raw signal cluster gathering — classify signals into winners/losers, (3) refinement grind — eliminate losing clusters. Also implements the multi-run consensus pipeline grind primitives via additive `--permute`, `--seed`, `--subsample`, `--zero-margin`, `--no-peak-target`, `--pass-order`, `--scan-only`, `--skip-gather`, `--conditions-file`, `--output-dir` flags.

**Key design facts:**
- Uses **cache-relative coordinates** throughout. Every example carries a `cache_scan_idx` resolved from its signal date against the ticker's expression-cache `dates` array. Bar indices are never carried across OHLCV and expression cache (different start dates, different lengths) — this was the root cause of the Session 5 bar-count bug where historical tiers were silently no-ops.
- Applies a **per-bar tradable filter** via `compute_tradable_masks()` at grind start. Bar is tradable if close ≥ $1, 20-day average dollar volume ≥ $4M, and 20-bar ADRP ≥ 1.8% — **all evaluated at that bar**, not just at today. Tickers with zero tradable bars are skipped before `.npz` load. Filter is passed to tier workers and AND-ed into `pass_mask` during tier matrix building.
- Spawns a **fresh `ProcessPoolExecutor` per historical tier** — workers have no persistent state across tiers. Each ticker's `.npz` is loaded, decompressed, and cast to float32 once per tier that needs it. Known inefficiency; target for future quality-preserving optimization work per `CONSENSUS.md`.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache` — **must be built via `expr_cache_builder.py --build`, not `--append`, for consensus runs**
- `local_runner/cache/universe_matrix.pkl` via `matrix_builder.get_universe_matrix()`
- `brute_expressions.py` — `generate_all()`
- SQLite `examples` table (for example ticker+date pairs)
- SQLite `setups` table (for trade direction)
- `spiderweb.py` — `SpiderwebSearch` class (D1 tier only)
- ~~`signal_filter.py` — `scan_all_signals()`, `_build_slim_cache()`~~ — cluster gathering now scans directly from `.npz` expression cache (2026-04-13), no longer uses `signal_filter.py` scanner or `.im` files
- `local_runner/cache/pyramid_{setup}_*.json` (loads own prior output for cluster gathering)
- `data/signal_exit_grind/signal_exit_{setup}.json` (exit condition for cluster gathering)

**Outputs:**
- `local_runner/cache/pyramid_{setup}_{description}_{timestamp}.json` — signal grind results (multi-pass runs). Consensus runs go to `local_runner/cache/consensus/` via `--output-dir`.
- `local_runner/cache/permuted_{setup}_*.json` — permuted-run outputs under `--permute` (separate prefix so loaders never mistake a permuted result for a real one).
- `local_runner/cache/raw_signal_clusters_{setup}_{timestamp}.json` — classified clusters
- `local_runner/cache/raw_signal_clusters_{setup}.json` — latest pointer
- `local_runner/cache/refinement_{setup}_{description}_{timestamp}.json` — refinement results

**Also calls:** `file_mirror.mirror_file()`, `grind_uploader.upload()` (Railway backup). **Both suppressed when `--output-dir` is set** (consensus pipeline case).

**Downstream Consumers:**
- `signal_exit_grinder.py` — reads `pyramid_*.json` for signal conditions
- `signal_filter.py` — reads `pyramid_*.json` for signal conditions
- `entry_grinder.py` — reads `raw_signal_clusters_{setup}.json`
- `entry_candle_scorer.py` — reads `refinement_*.json` + `raw_signal_clusters_{setup}.json`
- `ev_grinder.py` — reads `refinement_*.json` + `raw_signal_clusters_{setup}.json`
- `profit_grinder.py` — reads `raw_signal_clusters_{setup}.json` (for entry window)
- `consensus_engine.py` — reads all `pyramid_*.json` and `refinement_*.json` files
- `scanperfect.py` — reads refinement + cluster files for UI display

---

### spiderweb.py
**Location:** `local_runner/spiderweb.py`
**What it does:** Beam search algorithm. Pure computation — no file I/O.

**Called by:** `pyramid_grinder.py` (D1 tier)

---

### signal_exit_grinder.py
**Location:** `scripts/signal_exit_grinder.py`
**Spec:** `SIGNAL_GRINDER.md` (exit condition section)
**What it does:** Brute-forces exit conditions from the signal bar (not entry bar). Finds when signals resolve.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `local_runner/cache/pyramid_{setup}_*.json` (signal conditions)
- SQLite `examples` table
- SQLite `setups` table (direction)

**Outputs:**
- `data/signal_exit_grind/signal_exit_{setup}_{n}ex_{adr}adr_{timestamp}.json`
- `data/signal_exit_grind/signal_exit_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`, Railway API upload

**Downstream Consumers:**
- `pyramid_grinder.py` — reads exit condition for cluster gathering step
- `signal_filter.py` — reads exit condition for exit application
- `entry_grinder.py` — reads exit condition
- `scanperfect.py` — reads exit condition for chart display

---

### signal_exit_pool_grinder.py
**Location:** `scripts/signal_exit_pool_grinder.py`
**Spec:** `CLASSIFIER_SPEC.md §15` (labeler mechanic), `plans/proud-hatching-puddle.md` step 3
**What it does:** Aggregate-realized-P&L exit rule search over the combined breakout-pool clusters (HTF/BF/BASE), per CLASSIFIER_SPEC §15.3–15.5. For each setup, reads ENTRY-tagged rows from the classifier tag file, applies the stop/cap/forward-race rule to each, then brute-forces the expression cache (count read at runtime) × ~20 percentile thresholds × {>=, <=} for the single boolean exit expression that maximizes sum-of-per-cluster realized P&L in ADR units. Stop-first race, flat −1 ADR loss on stop hit, close-based pnl on exit fire or forced exit at cap. Separate from `signal_exit_grinder.py`, which fits per-example exit conditions for the legacy classifier path.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `data/scanperfect.db.earnings_dates`
- `<classifier worktree>/research/classifier_tags/{setup}_tags.json`

**Outputs:**
- `data/signal_exit_grind/signal_exit_pool_{setup}.json` (does NOT clobber the existing `signal_exit_{setup}.json`)

**Downstream Consumers:**
- `<classifier worktree>/research/labeler.py` — pending build, per CLASSIFIER_SPEC §15.6
- `<classifier worktree>/research/classify_pool.py` — pending build

**Status:** Code + smoke-test complete 2026-04-24 on worktree branch `signal-exit-pool` off v2; real gate-verify pending on fresh upstream, then merge.

---

### signal_filter.py
**Location:** `scripts/signal_filter.py`
**Spec:** `SIGNAL_FILTER.md` (classification logic), `PIPELINE_V2.md` (pipeline position)
**What it does:** Scans full universe with locked conditions, applies exit, classifies signals into winner/loser piles using ADR thresholds, filters by ADR.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` + `*.append` — has its own `_load_ticker_npz()` that reads .npz + .append files (mirrors `load_ticker_cache()` logic). Exit batch path also calls `load_ticker_cache()` directly.
- `local_runner/cache/pyramid_{setup}_*.json` (signal conditions)
- `data/signal_exit_grind/signal_exit_{setup}.json` (exit condition)
- SQLite `examples` table

**Outputs:**
- `data/signal_filter/filtered_{setup}_{n}sig_{timestamp}.json`
- `data/signal_filter/filtered_{setup}.json` — latest pointer
- `data/signal_filter/classified_{setup}_{n}sig_{timestamp}.json`
- `data/signal_filter/classified_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`, Railway API upload

**Downstream Consumers:**
- `scanperfect.py` — reads `filtered_{setup}.json` for vetting display
- `upload_vetting_data.py` — uploads filtered file to Railway

---

### entry_grinder.py
**Location:** `scripts/entry_grinder.py`
**Spec:** `MANAGEMENT_GRINDER.md` (stop management sub-component)
**What it does:** Stop management optimization — ratchet protocol, breakeven timing, initial stop placement. Entry-relative, works with example set. Deferred pending classification (Problem 1).

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `local_runner/cache/raw_signal_clusters_{setup}.json`
- `data/signal_exit_grind/signal_exit_{setup}.json`
- SQLite `setups` table (direction)

**Outputs:**
- `local_runner/cache/entry_grinder_{setup}_{timestamp}.json`
- `local_runner/cache/entry_grinder_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`

**Downstream Consumers:**
- `scanperfect.py` — reads entry grinder output for display (indirect, via cluster data)

---

### entry_candle_scorer.py
**Location:** `scripts/entry_candle_scorer.py`
**Spec:** `PIPELINE_V2.md`
**What it does:** Builds centroid vector from example entry candles, scores all winners by similarity.

**Inputs:**
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `local_runner/cache/refinement_{setup}_*.json`
- `local_runner/cache/raw_signal_clusters_{setup}.json`
- SQLite `examples` table

**Outputs:**
- `local_runner/cache/entry_scores_{setup}_{timestamp}.json`
- `local_runner/cache/entry_scores_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`

**Downstream Consumers:**
- `profit_grinder.py` — reads entry scores for tradability weighting
- `scanperfect.py` — reads entry scores for vetting sort + display

---

### ev_grinder.py
**Location:** `scripts/ev_grinder.py`
**Spec:** `archive/shelved_docs/EV_GRINDER.md` (deferred — not on active call graph, see spec banner)
**What it does:** Correlative scoring. Tests every market condition and ticker attribute against the signal set. Scores signals by predicted WR/MFE/EV. **Not currently wired into the consensus pipeline** — will return in the future "live EV ranked watchlist" build.

**Inputs:**
- `local_runner/cache/refinement_{setup}_*.json`
- `local_runner/cache/raw_signal_clusters_{setup}.json`
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/fundamentals_cache.json`
- `local_runner/cache/market_series/*.npz` + `_manifest.json`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `ev_tree_scorer.py` — optional XGBoost tree model

**Outputs:**
- `local_runner/cache/ev_{setup}_inc6_{timestamp}.json`

**Also calls:** `file_mirror.mirror_file()`

**Downstream Consumers:**
- `profit_grinder.py` — reads EV output for signal population + scores
- `scanperfect.py` — reads EV output for correlative vetting display

---

### ev_tree_scorer.py
**Location:** `scripts/ev_tree_scorer.py`
**What it does:** XGBoost-based alternative scorer. Trains WR + MFE models, produces SHAP-based scores.

**Inputs:**
- Deduped survivors from `ev_grinder.py` (passed in-memory)

**Called by:** `ev_grinder.py`

**Outputs:** Returns scores in-memory (no file I/O)

---

### profit_grinder.py
**Location:** `scripts/profit_grinder.py`
**Spec:** `MANAGEMENT_GRINDER.md` (exit optimization sub-component)
**What it does:** Brute-forces TA-expression-based exit conditions for profit-taking, multi-stage trim. Entry-relative. Inc 1-4 complete but deferred — not on active call graph.

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/ev_{setup}_*.json` (latest EV output)
- `local_runner/cache/entry_scores_{setup}.json`
- `local_runner/cache/raw_signal_clusters_{setup}.json` (for entry window)
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- SQLite `rejected_signals` table (vetted-NO exclusions)
- SQLite `examples` table (for entry dates)

**Outputs:**
- `local_runner/cache/profit_{setup}_{timestamp}.json`
- `local_runner/cache/profit_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`

**Downstream Consumers:**
- `scanperfect.py` — reads profit output for exit tab in scan tuning + vetting

---

### profit_grinder_2stage.py
**Location:** `scripts/profit_grinder_2stage.py`
**What it does:** Helper module for 2-stage profit grinding. Called by `profit_grinder.py`.

**Called by:** `profit_grinder.py` (imports `grind_2stage` at runtime via `__main__`)

---

### consensus_engine.py
**Location:** `scripts/consensus_engine.py`
**Spec:** `SIGNAL_GRINDER.md` (signal stage), `REFINEMENT_GRINDER.md` (refinement stage), `CONSENSUS.md` (active status tracker)
**What it does:** Analyzes multiple grind runs to find conditions that appear consistently (threshold frequency). Runs in two stages — signal consensus and refinement consensus — invoked by `run_consensus_pipeline.py`.

**Inputs:**
- `local_runner/cache/pyramid_{setup}_*.json` (all signal grind runs)
- `local_runner/cache/refinement_{setup}_*.json` (all refinement runs)
- SQLite `examples` table (for example count / EPV calculation)

**Outputs:**
- `local_runner/cache/consensus_{stage}_{setup}_{timestamp}.json`
- `local_runner/cache/consensus_{stage}_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`

**Downstream Consumers:**
- Used as locked condition input for subsequent grinder runs

---

### exit_grinder.py
**Location:** `scripts/exit_grinder.py`
**What it does:** Trade management exit optimizer (runs from entry bar, not signal bar). Uses `exit_expressions.py` library (6,410 expressions).

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl`
- Railway API (for example list — still uses API, not local SQLite)
- `scripts/exit_expressions.py` — `generate_exit_expressions()`
- `scripts/exit_compute.py` — `ExitExprEngine` class

**Outputs:**
- `data/exit_grind/exit_grind_{setup}_{timestamp}.json`
- `data/exit_grind/exit_grind_{setup}.json` — latest pointer

**Also calls:** `file_mirror.mirror_file()`

**Note:** This is separate from `signal_exit_grinder.py`. Exit grinder works on trade management (entry-relative). Signal exit grinder works on signal resolution (cache-compatible).

---

### avatar_phase_probe.py
**Location:** `research/avatar_phase_probe.py`
**Spec:** `Avatar_Correlator.md`
**Status:** Research-stage. Single-anchor approach-A probe; multi-anchor 1-NN refactor pending per Avatar_Correlator.md §6.3.
**What it does:** Setup-agnostic phase-decomposition shape recognizer. Per setup, takes a single bank example with phase boundaries and produces a ranked top-N list of universe candidates by Euclidean distance in 6-per-phase generic signature space. Replaces the prior log-close Pearson viability gate (research/avatar_viability_bank.py).

**Key design facts:**
- 6-dim per-phase signature (length_frac, end_level, max_level, min_level, directness, argmax_position) — log-return anchored at window-start, no setup-specific math.
- Hard structural constraint inside DP fit for K ≥ 2: `phase_0_max_level ≥ close[asof]_level`. Replaces buggy §3.10 fade filter for retrace-length-exceeding-exclude_last cases.
- Two-stage parallel pipeline: sequential pre-pass (tradable + class filter + finite-window check) → numba `prange` DP fit over flat eligible-end-idxs → sequential per-ticker greedy non-overlap dedup → global heap top-N.
- Per-ticker eligibility uses `fundamentals_cache.json`: skip industries in EXCLUDED_INDUSTRIES (currently {"Biotechnology"}); ETFs/ETNs without fundamentals pass through (market cap check skipped when shares_outstanding missing).
- Bank entries hardcoded in BANK list at top of script; phase boundaries pulled from "Setup phases" Google Sheet (public-share + curl `gviz/tq?sheet=<name>` endpoint, or Drive MCP XLSX export as fallback).

**Inputs:**
- `local_runner/cache/universe_ohlcv_daily.pkl` — universe OHLCV
- `local_runner/cache/fundamentals_cache.json` — sector/industry/shares_outstanding for industry exclusion + market cap filter
- Google Sheet "Setup phases" (file id `1hG2Pm0KeRNNaUam_oKiUuDQtT8xaqYnUxFE-ry_fCF8`) — bank phase boundary dates per setup tab. Read via Drive MCP or public-share `/gviz/tq?tqx=out:csv&sheet=<setup>` endpoint.

**Outputs (per setup, written to `research/avatar_viability_bank/`):**
- `<setup>_phase_probe_results.json` — bank metadata + top-N ranked candidates with fit boundaries
- `<setup>_phase_probe_top<N>.png` — 5×6 grid of log-close overlays
- `<setup>_all_dedup_distances.npy` — full dedup'd per-candidate distance distribution (used by percentile flag, §6.4 of Avatar_Correlator.md — partial implementation, not yet validated end-to-end)
- `<setup>_per_day_min_distances.npy` — per-day-min distance distribution (day-strength context, §6.4)

**Downstream consumers:** None yet built. Per Avatar_Correlator.md §3.6, the joint EV surface combines ranked candidates with classifier W/L outcomes and EV grinder regime scores. Integration pending the multi-anchor refactor (§6.3) and the classifier hookup.

---

## Layer 4 — Infrastructure

### nightly.py
**Location:** `local_runner/nightly.py`
**Spec:** `NIGHTLY_REFRESH.md`
**What it does:** Orchestrates the 10-step nightly data refresh pipeline. Infrastructure-only — no scanning or signal output. Runs via Windows Task Scheduler at 4:30pm ET. Step 5 uses intermediate cache architecture — full rebuild of 196-column .im files (~1.7 min), replacing the previous 19-min forward-prop expression cache append.

**Calls (in order):**
1. `cache_builder.check_freshness()` — gate (alias `check_yfinance_freshness` for compat)
2. `cache_builder.append_daily_cache()` — daily OHLCV
3. `cache_builder.append_weekly()` — weekly OHLCV
4. `cache_builder.append_monthly()` — monthly OHLCV
5. `intermediate_cache_builder.build_full()` — intermediate cache rebuild (196 intermediates per ticker)
6. `matrix_builder.get_universe_matrix()` — universe matrix rebuild (graceful skip if expr cache missing)
7. Railway API `/api/universe/refresh-earnings` — earnings dates
8. `market_cache_builder.append_new_bars()` — market cache
9. `fetch_fundamentals` functions — fundamentals cache
10. `seed_vault.backup()` — Railway backup

**Outputs:** None directly — delegates to each step's producer

---

### seed_vault.py
**Location:** `scripts/seed_vault.py`
**Spec:** `LOCALIZE.md`
**What it does:** Backup/restore SQLite tables and JSON grind files to/from Railway.

**Inputs (backup mode):**
- SQLite `scanperfect.db` (all tables)
- JSON files in: `local_runner/cache/`, `data/exit_grind/`, `data/signal_exit_grind/`, `data/signal_filter/`, `data/profit_grind/`

**Outputs (restore mode):**
- Recreates SQLite tables from Railway data
- Downloads JSON files from Railway `file_mirror` table to local paths

**Called by:** `nightly.py` step 10

---

### file_mirror.py
**Location:** `local_runner/file_mirror.py`
**What it does:** Uploads a single file to Railway's `file_mirror` table.

**Called by:** Almost every grinder after saving results — `pyramid_grinder.py`, `signal_filter.py`, `signal_exit_grinder.py`, `entry_grinder.py`, `entry_candle_scorer.py`, `ev_grinder.py`, `profit_grinder.py`, `consensus_engine.py`, `fetch_fundamentals.py`, `exit_grinder.py`

---

### grind_uploader.py
**Location:** `local_runner/grind_uploader.py`
**What it does:** Uploads grind results to Railway's v2 cycle system with validation, hashing, and retry queue.

**Called by:** `pyramid_grinder.py` (signal grind + refinement results)

**Outputs:**
- `local_runner/pending_uploads/*.json` (retry queue for failed uploads)

---

### agent.py
**Location:** `local_runner/agent.py`
**What it does:** Polling agent — checks Railway for pending jobs, runs grinders as subprocesses.

**Calls:**
- `matrix_builder._universe_matrix_fresh()` — checks if nightly rebuild needed
- `nightly.main()` — triggers nightly rebuild
- `matrix_builder.get_universe_matrix()`, `get_example_matrix()`
- `spiderweb.SpiderwebSearch` — for legacy grind jobs
- Railway API for job polling, status updates, heartbeats

---

### pipeline_agent.py
**Location:** `local_runner/pipeline_agent.py`
**What it does:** Alternative agent focused on pipeline step execution. Polls Railway for pipeline jobs, runs steps as subprocesses.

**Calls:**
- `grind_uploader.retry_pending()` — retries failed uploads on startup
- Railway API for pipeline job polling and status updates

---

### bulk_mirror.py
**Location:** `scripts/bulk_mirror.py`
**What it does:** Walks `local_runner/cache/` and `data/`, uploads every `.json` to Railway.

**Inputs:** All `.json` files in cache and data directories

---

### upload_vetting_data.py
**Location:** `scripts/upload_vetting_data.py`
**What it does:** Uploads filtered signals + exit conditions to Railway for web display.

**Inputs:**
- `data/signal_filter/filtered_{setup}.json`
- `data/signal_exit_grind/signal_exit_{setup}.json`

---

### fetch_missing_market.py
**Location:** `scripts/fetch_missing_market.py`
**What it does:** Fetches only missing instruments and merges into existing `market_ohlcv.pkl`.

**Inputs/Outputs:** `local_runner/cache/market_ohlcv.pkl`
**Calls:** `market_cache_builder.all_instruments()`, `_fetch_one()`

---

## Layer 5 — Compute Libraries (no file I/O, called by grinders)

### expression_engine.py
**Location:** `scripts/expression_engine.py`
**What it does:** Computes individual expression values from OHLCV DataFrames. Core engine used by cache builders and grinders.

**Called by:** `expr_cache_builder.py`, `market_cache_builder.py`, `matrix_builder.py`, `exit_compute.py`
**Depends on:** `profiling_engine.py`

---

### profiling_engine.py
**Location:** `scripts/profiling_engine.py`
**What it does:** TA indicator library (SMA, EMA, RSI, ATR, etc.) using pandas Series.

**Called by:** `expression_engine.py`, `expr_cache_builder.py`, `backtest_conditions.py`

---

### backtest_conditions.py
**Location:** `scripts/backtest_conditions.py`
**What it does:** Computes expression series from an ExpressionEngine instance. Handles boolean conditions, counting, recency ops.

**Called by:** `expr_cache_builder.py`, `market_cache_builder.py`, `exit_compute.py`
**Depends on:** `expression_engine.py`, `profiling_engine.py`

---

### exit_expressions.py
**Location:** `scripts/exit_expressions.py`
**What it does:** Generates the exit expression library (~6,410 expressions). Trade-management expressions relative to entry bar.

**Called by:** `expr_cache_builder.py` (for extension expressions), `exit_grinder.py`

---

### exit_compute.py
**Location:** `scripts/exit_compute.py`
**What it does:** `ExitExprEngine` class — computes exit expression values relative to an entry bar.

**Called by:** `exit_grinder.py`
**Depends on:** `expression_engine.py`, `lsp_detector_v2.py`, `algo_line_detector.py`, `profiling_engine.py`, `backtest_conditions.py`

---

### lsp_detector_v2.py
**Location:** `scripts/lsp_detector_v2.py`
**Spec:** `EXPRESSION_ENGINE_V2.md`
**What it does:** Detects liquidity/structure pivots (LSP) from OHLCV data. Computes LSP-related expression series.

**Called by:** `expr_cache_builder.py`, `exit_compute.py`

---

### algo_line_detector.py
**Location:** `scripts/algo_line_detector.py`
**What it does:** Detects H- and L+ trendlines from high-volume candles.

**Called by:** `expr_cache_builder.py`, `exit_compute.py`

---

### local_db.py
**Location:** `scripts/local_db.py`
**What it does:** SQLite helper functions. Used by Railway server and analysis API.

**Depends on:** SQLite `scanperfect.db`

---

### analysis_api.py
**Location:** `scripts/analysis_api.py`
**What it does:** Orchestrates profiling, discovery, and outcome analysis. Mostly legacy — references `profiling_engine.ProfilingEngine`, `discovery_engine.DiscoveryEngine`, `outcome_engine.OutcomeEngine`.

---

## Layer 6 — UI + Server

### scanperfect.py
**Location:** repo root
**Spec:** `UI_FLOW.md`
**What it does:** PySide6 desktop application. Reads everything locally — no HTTP, no server.

**Reads:**
- SQLite `scanperfect.db` — setups, examples, pending_examples, rejected_signals, earnings_dates
- `local_runner/cache/universe_ohlcv_daily.pkl` — OHLCV for charts
- `local_runner/cache/pyramid_{setup}_*.json` — not directly, but via cluster files
- `local_runner/cache/raw_signal_clusters_{setup}.json` — cluster data
- `local_runner/cache/refinement_{setup}_*.json` — depth progression
- `local_runner/cache/ev_{setup}_*.json` — EV scores
- `local_runner/cache/entry_scores_{setup}.json` — entry candle scores
- `local_runner/cache/profit_{setup}.json` — profit grinder results
- `local_runner/cache/scan_settings_{setup}.json` — user slider settings
- `data/signal_filter/filtered_{setup}.json` — filtered signals for vetting
- `data/signal_exit_grind/signal_exit_{setup}.json` — exit condition
- `data/profit_grind/profit_{setup}.json` — profit results (alternate path)
- `data/vetting/vetting_{setup}.json` — vetting decisions
- `data/pipeline_state.json` — pipeline step statuses
- `data/pipeline_logs.json` — pipeline logs

**Writes:**
- `data/vetting/vetting_{setup}.json` — vetting decisions
- `local_runner/cache/scan_settings_{setup}.json` — slider settings
- SQLite — example inserts, pending example updates, rejected signal inserts

---

### server.py
**Location:** repo root
**What it does:** FastAPI server. Runs on Railway (production) and locally (development). Manages SQLite, serves APIs for examples, vetting, pipeline control.

**Reads/Writes:** SQLite `scanperfect.db`, `data/pipeline_state.json`, `data/pipeline_logs.json`, vetting JSONs, grind result JSONs
**Local mode:** Loads daily OHLCV pickle into memory

---

### restore_scanperfect.py
**Location:** repo root
**What it does:** Restores `scanperfect.py` from a known good git commit. Emergency recovery script.

---

### scanperfect_patch.py
**Location:** repo root
**What it does:** Patches `scanperfect.py` to add the setup creation dialog. One-time migration script.

---

## Critical Dependency Chains

These are the chains where a change at the top silently breaks everything below. The auditor must check all downstream files when any component in a chain is modified.

### Chain 1: Expression Library → Everything
```
brute_expressions.py (expression list)
  → expr_cache_builder.py (per-ticker .npz)
    → matrix_builder.py (universe_matrix.pkl)
      → pyramid_grinder.py (all grind results)
        → signal_exit_grinder.py → signal_filter.py → ev_grinder.py → profit_grinder.py
  → market_cache_builder.py (market .npz)
    → ev_grinder.py
```
**If expression list changes:** Full expr cache rebuild (~163 GB), market cache rebuild, matrix rebuild, then all grinders must re-run. Fingerprint system detects this automatically.

### Chain 2: OHLCV Pickle → Cache → Grinders
```
cache_builder.py (daily .pkl)
  → expr_cache_builder.py (.npz files)
  → pyramid_grinder.py
  → signal_filter.py
  → all other grinders
  → scanperfect.py (charts)
```
**If pickle format changes:** Every downstream consumer breaks. Format is `{ticker: DataFrame}` with columns `date, open, high, low, close, volume, dvol_20d`.

### Chain 3: Grinder Pipeline (sequential)
```
pyramid_grinder.py (signal conditions → pyramid_*.json)
  → signal_exit_grinder.py (exit condition → signal_exit_*.json)
    → pyramid_grinder.py again (cluster gathering → raw_signal_clusters_*.json)
      → pyramid_grinder.py again (refinement → refinement_*.json)
        → signal_filter.py (full scan → filtered_*.json)
        → entry_candle_scorer.py (entry scores → entry_scores_*.json)
        → ev_grinder.py (correlative scoring → ev_*.json)
          → profit_grinder.py (exit optimization → profit_*.json)
```
**Each step reads the previous step's JSON output.** If JSON keys change, downstream steps break.

### Chain 4: SQLite → Everything
```
scanperfect.db
  ├── examples table → pyramid_grinder, signal_filter, signal_exit_grinder, entry_grinder, entry_candle_scorer, consensus_engine, profit_grinder
  ├── setups table → pyramid_grinder, signal_filter, signal_exit_grinder, entry_grinder (for direction)
  ├── EODHD exchange-symbol-list → cache_builder (for ticker list)
  ├── rejected_signals table → profit_grinder, scanperfect.py
  └── pending_examples table → scanperfect.py, agent.py
```

### Chain 5: Standalone Live Scan (not in nightly)
```
cache_builder.py (daily .pkl)
  → intermediate_cache_builder.py (.im files, 196 intermediate columns per ticker)
    → scan_engine.py (reads .im, applies locked conditions, produces signals via CLI)
```
**This chain is independent of the grinder's `.npz` expression cache, and is NOT part of nightly.** Nightly only refreshes the upstream caches (daily .pkl + .im files); `scan_engine.py` is invoked separately via its own CLI when a watchlist is wanted. The live scan uses a stripped-down 196-intermediate format because it only needs the ~42 dispatchable expression types, not all 15,805. Grinders cannot use `.im` (they need the full expression library). These are two parallel caches with different purposes: `.im` is live-scan-only, `.npz` is grinder-only. Do not conflate them.

---

## File Path Quick Reference

| Path | Format | Producer | Key Consumers |
|------|--------|----------|---------------|
| `local_runner/cache/universe_ohlcv_daily.pkl` | pickle dict `{ticker → DataFrame}` | cache_builder | expr_cache_builder, all grinders, scanperfect |
| `local_runner/cache/universe_ohlcv_weekly.pkl` | pickle dict `{ticker → DataFrame}` | cache_builder | expr_cache_builder |
| `local_runner/cache/universe_ohlcv_monthly.pkl` | pickle dict `{ticker → DataFrame}` | cache_builder | expr_cache_builder |
| `local_runner/cache/expr_series/{TICKER}.npz` | float16 npz (float32 on load) | expr_cache_builder | all grinders |
| `local_runner/cache/expr_series/{TICKER}.append` | raw float16 binary (n_rows × 16,001 cols) | expr_cache_builder (forward-prop) | expr_cache_builder (load_ticker_cache), signal_filter |
| `local_runner/cache/expr_series/{TICKER}.append_dates` | text (one date per line) | expr_cache_builder (forward-prop) | expr_cache_builder (load_ticker_cache), signal_filter |
| `local_runner/cache/expr_series/{TICKER}.lookback` | raw float16 binary (504 × 196) | setup_forward_prop; sliding updates by nightly append | expr_cache_builder (forward-prop) |
| `local_runner/cache/expr_series/{TICKER}.state` | JSON (335 float64 scalar values + variable-length keys: `moc_levels`, `reversal_profile_state`, `ext50_trendline_state.ext50_history`) | setup_forward_prop; overwritten nightly by forward-prop | expr_cache_builder (forward-prop) |
| `local_runner/cache/expr_series/_manifest.json` | JSON | expr_cache_builder | expr_cache_builder (self) |
| `local_runner/cache/market_ohlcv.pkl` | pickle dict | market_cache_builder | market_cache_builder (self), fetch_missing_market |
| `local_runner/cache/market_series/{INST}.npz` | float32 npz | market_cache_builder | ev_grinder |
| `local_runner/cache/market_series/_manifest.json` | JSON | market_cache_builder | ev_grinder |
| `local_runner/cache/universe_matrix.pkl` | pickle dict | matrix_builder | pyramid_grinder |
| `local_runner/cache/brute_expressions.json` | JSON | brute_expressions | matrix_builder |
| `local_runner/cache/fundamentals_cache.json` | JSON | fetch_fundamentals | ev_grinder |
| `local_runner/cache/pyramid_{setup}_*.json` | JSON | pyramid_grinder | signal_exit_grinder, signal_filter, consensus_engine |
| `local_runner/cache/raw_signal_clusters_{setup}.json` | JSON | pyramid_grinder | entry_grinder, entry_candle_scorer, ev_grinder, profit_grinder, scanperfect |
| `local_runner/cache/refinement_{setup}_*.json` | JSON | pyramid_grinder | entry_candle_scorer, ev_grinder, consensus_engine, scanperfect |
| `local_runner/cache/ev_{setup}_*.json` | JSON | ev_grinder | profit_grinder, scanperfect |
| `local_runner/cache/entry_scores_{setup}.json` | JSON | entry_candle_scorer | profit_grinder, scanperfect |
| `local_runner/cache/profit_{setup}.json` | JSON | profit_grinder | scanperfect |
| `local_runner/cache/scan_settings_{setup}.json` | JSON | scanperfect (UI) | scanperfect (UI) |
| `local_runner/cache/consensus_*.json` | JSON | consensus_engine | grinder re-runs |
| `local_runner/cache/entry_grinder_{setup}.json` | JSON | entry_grinder | scanperfect (indirect) |
| `data/scanperfect.db` | SQLite | server, scanperfect | everything |
| `data/signal_exit_grind/signal_exit_{setup}.json` | JSON | signal_exit_grinder | pyramid_grinder, signal_filter, entry_grinder, scanperfect |
| `data/signal_filter/filtered_{setup}.json` | JSON | signal_filter | scanperfect |
| `data/signal_filter/classified_{setup}.json` | JSON | signal_filter | — |
| `data/exit_grind/exit_grind_{setup}.json` | JSON | exit_grinder | — |
| `data/vetting/vetting_{setup}.json` | JSON | scanperfect (UI) | scanperfect, server |
| `data/pipeline_state.json` | JSON | server, scanperfect | scanperfect |
| `data/pipeline_logs.json` | JSON | server, scanperfect | scanperfect |
