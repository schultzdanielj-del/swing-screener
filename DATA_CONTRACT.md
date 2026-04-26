# Data Contract — ScanPerfect Schema & Data Flow

Authoritative reference for SQLite schemas, cache file formats, and data flow rules. Build from this. Do not assume anything not written here. When this file conflicts with code, fix the code or fix the doc — never both silently.

---

## Design Principles

- **Local is authoritative.** SQLite DB (`data/scanperfect.db`) and local cache files (`local_runner/cache/`) are the single source of truth. All compute runs locally, all results write locally.
- **Railway is a seed vault.** Daily backup of DB tables + grind result JSONs to Railway via `scripts/seed_vault.py`. Railway has no compute, no UI, no pipeline logic. It exists for disaster recovery and for Claude to read grind results during chat sessions.
- **Grind results are local JSON files.** The grinders write timestamped JSON to `local_runner/cache/` (or `local_runner/cache/consensus/` for consensus-pipeline runs via `--output-dir`). These are the authoritative outputs. They are also mirrored to Railway's `file_mirror` table as a backup copy, except for consensus runs which suppress the mirror.
- **The PySide6 app reads local files directly.** No HTTP layer, no server process, no API calls.
- All timestamps are UTC ISO-8601 strings: `"2026-04-11T14:30:22Z"`.
- All dates (signal dates, entry dates) are `"YYYY-MM-DD"` strings.
- **Cross-source alignment uses dates, never bar indices.** OHLCV cache, expression cache, `.im` intermediate cache, and market cache all have different start dates and lengths. Carrying a bar index from one cache to another silently produces wrong values. Every cross-source lookup goes through dates.
- **OHLCV and expression caches must stay in sync on ticker membership.** Delisted tickers are removed from the OHLCV pickle on nightly sync; the expression cache follows suit on the next manual `expr_cache_builder.py --build`.

---

## SQLite Tables (`data/scanperfect.db`)

### `setups`
Setup type definitions. Seeded with DTSS, 3-4DB, HTF, BF, BASE.

| Column | Type | Notes |
|--------|------|-------|
| setup_type | TEXT PK | e.g. `"dtss"` |
| name | TEXT | Display name |
| description | TEXT | Pattern description (feeds AI reviewer) |
| direction | TEXT | `"long"` or `"short"` |
| created_at | TEXT | |

### `examples`
Ground truth. One row per validated setup example.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| setup_type | TEXT | |
| ticker | TEXT | |
| chart_date | TEXT | date the chart pattern is visible |
| entry_date | TEXT | date of the actual entry bar |
| created_at | TEXT | |

Unique: `(setup_type, ticker, entry_date)`

### `pending_examples`
AI vet queue. YES picks awaiting AI review.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| setup_type | TEXT | |
| ticker | TEXT | |
| signal_date | TEXT | |
| entry_date | TEXT | |
| status | TEXT | `pending` / `reviewed` |
| ai_verdict | TEXT | `APPROVE` / `REJECT` |
| ai_reasoning | TEXT | |
| created_at | TEXT | |
| reviewed_at | TEXT | |

### `rejected_signals`
Signals marked NO. Prevents re-surfacing in vetting. Also used by profit grinder to exclude vetted-NO signals from the population.

### `earnings_dates`
Cached earnings dates per ticker.

### `file_mirror`
Backup copies of grind result JSONs. Used by seed vault for Railway backup. The PySide6 app does NOT read from this table — it reads local files directly.

### `nightly_watchlist`
Future: ranked signals from the live nightly scan.

---

## Local Cache Files (`local_runner/cache/`)

These are the authoritative outputs and inputs for all downstream consumers. The PySide6 app reads them directly.

### OHLCV data

| File | Producer | Contents |
|---|---|---|
| `universe_ohlcv_daily.pkl` | `cache_builder.py` | All daily OHLCV for ~11,500 tickers (Common Stock + ETF from EODHD across NYSE, NASDAQ, NYSE ARCA, BATS, NYSE MKT/AMEX). Universe synced nightly — new IPOs added, delisted tickers removed. Dict `{ticker: DataFrame}` with columns `date, open, high, low, close, volume, dvol_20d`. **2026-04-22 adjustment policy:** OHLC is forward-split-adjusted (continuous across split boundaries, IBKR-style) but NOT dividend-adjusted; `volume` is forward-split-adjusted by EODHD/yfinance and passed through unchanged; `dvol_20d` is the 20-bar rolling mean of `close * volume`, smooth across split boundaries. Distribution ex-dates show real price drops (no back-adjustment). |
| `universe_ohlcv_weekly.pkl` | `cache_builder.py` | Same as daily, weekly bars. Same 2026-04-22 adjustment policy. Nightly append detects splits over the gap window and routes affected tickers to a full-history HTF refetch so weekly stays consistent with daily across split boundaries. |
| `universe_ohlcv_monthly.pkl` | `cache_builder.py` | Same, monthly bars. Same 2026-04-22 adjustment policy and HTF split-detection behavior as weekly. |
| `ticker_reference.json` | `cache_builder.py` | First trade date per ticker (for validation). |

### Expression cache (`expr_series/`)

| File | Producer | Contents |
|---|---|---|
| `expr_series/{TICKER}.npz` | `expr_cache_builder.py --build` | Per-ticker expression series, 16,216 columns × N bars (post-2026-04-25 feature build: 16,039 baseline + 12 Levels + 104 Trendlines + 61 MOC). Float16 on disk, cast to float32 on load. History begins at `EXPR_CACHE_START = 2020-01-02`. Frozen by full rebuild — never modified by append. |
| `expr_series/{TICKER}.append` | `expr_cache_builder.py --append` / `forward_prop_engine.py` | Raw float16 binary, one row per appended bar, **16,412 cols wide** (16,216 expression columns + 196 intermediate columns for forward-prop state). Grinders read only the first 16,216 columns via `load_ticker_cache()`. **Fills are best-effort**: any per-expression compute failure silently leaves that cell as NaN. For consensus grinding, run a full `--build` to guarantee complete values — do not rely on `.append`. |
| `expr_series/{TICKER}.append_dates` | `expr_cache_builder.py --append` | Date strings for appended rows, one per line. Paired with `.append`. |
| `expr_series/{TICKER}.lookback` | `forward_prop_engine.py` / `setup_forward_prop.py` | Last 504 rows × 196 intermediate columns, float16 raw binary. Sliding window for forward-prop state. |
| `expr_series/{TICKER}.state` | `forward_prop_engine.py` / `setup_forward_prop.py` | JSON. 335 float64 scalar state values (daily + HTF + ext struct + 18 added by post-2026-04-25 feature build) PLUS variable-length keys: `moc_levels` (list of active MOC level dicts), `reversal_profile_state` (per ext source: L_grid + cumulative crossings/returned + 14-bar pending queues), `ext50_trendline_state.ext50_history` (full ext50 history through .npz end-bar). Overwritten per append. |
| `expr_series/_manifest.json` | `expr_cache_builder.py` | Expression fingerprint, date range, per-ticker bar counts. Used by `ExprSeriesCache.is_valid()` to detect library changes. |

### Intermediate cache (`intermediate_series/`)

| File | Producer | Contents |
|---|---|---|
| `intermediate_series/{TICKER}.im` | `intermediate_cache_builder.py` | Binary: 4-byte uint32 header (row count), then `n_rows × 196 × float16` data, then `n_rows × 10` bytes of YYYY-MM-DD date strings. This cache is **separate from and independent of** `expr_series/`. It stores only 196 numeric intermediates (SMA, ATR, RSI, etc.) and is used by `scan_engine.py` for the nightly live-scan path. Grinders do NOT read `.im` files — they need the full 15,805-expression library which `.im` does not provide. |

### Market cache (`market_series/`)

| File | Producer | Contents |
|---|---|---|
| `market_ohlcv.pkl` | `market_cache_builder.py` | Raw OHLCV for all market instruments (US ETFs/stocks + EODHD indices + EODHD crypto + yfinance futures + FRED macro series). Exact instrument count is authoritative in `market_cache_builder.all_instruments()` — check the code, not this doc. |
| `market_series/{INST}.npz` | `market_cache_builder.py` | Per-instrument expression series. Float32. |
| `market_series/_manifest.json` | `market_cache_builder.py` | Metadata. Used by `ev_grinder.py`. |

### Universe matrix

| File | Producer | Contents |
|---|---|---|
| `universe_matrix.pkl` | `matrix_builder.py` | Precomputed D1 (last-bar) matrix: `{universe_matrix: (n_tickers, n_exprs) float32, universe_tickers: [...], expr_names: [...], expr_categories: [...]}`. Rebuilt nightly after the expression cache is updated. Used by `pyramid_grinder.run_d1_tier()` and `prefilter_candidates()`. |

### Grinder outputs

| File Pattern | Producer | Contents |
|---|---|---|
| `pyramid_{setup}_{mode}[_refinement]_sig{total}_pk{peak}_{timestamp}.json` | `pyramid_grinder.py` (signal grind) | Condition set + summary + per-tier breakdown. `mode` is `mp` (multi-pass) or `sp` (single-pass). `_refinement` present only on refinement runs. |
| `permuted_{setup}_mp_*.json` | `pyramid_grinder.py --permute` | Permuted-run output. Separate prefix prevents any loader from accidentally grabbing a permuted result as real conditions. |
| `raw_signal_clusters_{setup}_{timestamp}.json` | `pyramid_grinder.py` (cluster gathering) | All signal clusters with WIN/LOSS classification. Top-level fields: `setup_type`, `forward_window`, `direction`, `setup_class`, `winner_threshold_adr` (breakout only), `loser_threshold_adr` (breakout only), `breakeven_bars` (breakout only), `n_examples_validated`, `grinder_bugs`, cluster counts, and `clusters` array. |
| `raw_signal_clusters_{setup}.json` | `pyramid_grinder.py` | Latest pointer (copy of most recent timestamped file). |
| `refinement_{setup}_{description}_{timestamp}.json` | `pyramid_grinder.py --blackout` (refinement grind) | Winner/loser/eliminated signals + combined conditions + `depth_progression` for UI slider. Schema per `REFINEMENT_GRINDER.md`. |
| `consensus/pyramid_{setup}_mp_*.json` | `pyramid_grinder.py --output-dir .../consensus/` | Real-run outputs during a consensus pipeline run. Same schema as standard grind output. Railway mirror + upload suppressed. |
| `consensus/permuted_{setup}_mp_*.json` | `pyramid_grinder.py --permute --output-dir .../consensus/` | Permuted-run outputs during consensus. |
| `consensus_signal_{setup}.json` | `consensus_engine.py --stage signal` | Locked consensus conditions + z-score + stability metrics. Written only when z > 3. |
| `ev_{setup}_inc6_{timestamp}.json` | `ev_grinder.py` | Scoring equation + per-signal WR/MFE/EV + feature importances. |
| `entry_scores_{setup}.json` | `entry_candle_scorer.py` | Per-winner entry_candle_score + combined_score for vetting sort. |
| `profit_{setup}_{timestamp}.json` | `profit_grinder.py` | Exit expression candidates + weighted stats + equity curves + per-trade detail. |
| `profit_{setup}.json` | `profit_grinder.py` | Latest pointer. |
| `scan_settings_{setup}.json` | Scan Tuning UI | Locked slider settings read by the nightly scan. |
| `fundamentals_cache.json` | `fetch_fundamentals.py` | Per-ticker sector, shares outstanding, float. |

Other cache directories:
- `data/signal_exit_grind/signal_exit_{setup}.json` — latest pointer to signal exit conditions
- `data/signal_exit_grind/signal_exit_pool_{setup}.json` — L14 labeler output for the breakout classifier (HTF, BF, BASE). Schema: `{setup_type, grinder_type: "signal_exit_pool_l14_labeler", timestamp, adr_source, trade_lifetime_cap_bars, T_threshold_adr, T_setting_example, n_clusters_pool, n_entered, n_skipped_not_entry, n_skipped_missing_data, n_examples_entered, n_wild_entered, n_wild_win, n_wild_loss, wild_win_rate, examples_lock_passed, n_examples_lock_pass, halted, halt_reason, cluster_meta[]}`. `T_setting_example` is `{cluster_id, ticker, horizon, stop_hit_bar, eff_horizon, mfe_during_life}` for the example that anchors the threshold. Each `cluster_meta` entry (one per pool cluster, in pool order, including SKIPPED rows): `{cluster_id, ticker, is_example, tag, signal_bar_idx, status, reason, entry_k, entry_bar, entry_date, cap_bar, cap_date, cap_cause, horizon, eff_horizon, adr14_at_entry, effective_entry, stop, stop_hit_bar, mfe_during_life, mfe_full_window, final_label}`. `final_label ∈ {"WIN", "LOSS", null}` (null for non-ENTERED rows). Distinct from `signal_exit_{setup}.json` (legacy per-example exit-rule fit, still consumed by pyramid cluster gathering / signal_filter / entry_grinder / UI until pipeline reorder).
- `data/signal_filter/filtered_{setup}.json` + `classified_{setup}.json` — full-universe scan outputs
- `data/exit_grind/exit_grind_{setup}.json` — trade-management exit grinder output (separate from signal exit grinder)
- `data/vetting/vetting_{setup}.json` — UI vetting state

---

## Data Flow

```
SQLite (examples, setups, rejected_signals)
    │
    ├→ pyramid_grinder (signal grind)
    │     uses: expr cache .npz, OHLCV pkl, universe matrix
    │     writes: pyramid_*.json
    │
    ├→ signal_exit_grinder
    │     uses: expr cache, pyramid_*.json
    │     writes: signal_exit_{setup}.json
    │
    ├→ pyramid_grinder (cluster gathering via --scan-only)
    │     uses: pyramid_*.json, signal_exit_{setup}.json, OHLCV
    │     writes: raw_signal_clusters_{setup}.json
    │
    ├→ pyramid_grinder (refinement via --blackout)
    │     uses: raw_signal_clusters, expr cache
    │     writes: refinement_*.json
    │
    ├→ consensus_engine (signal stage + refinement stage)
    │     uses: consensus/pyramid_*.json + consensus/permuted_*.json
    │     writes: consensus_signal_{setup}.json
    │
    ├→ signal_filter (full-universe scan, post-consensus)
    │     uses: consensus_signal_*, expr cache, OHLCV
    │     writes: filtered_*.json, classified_*.json
    │
    ├→ entry_candle_scorer
    │     uses: refinement_*, raw_signal_clusters
    │     writes: entry_scores_{setup}.json
    │
    ├→ ev_grinder
    │     uses: refinement_*, raw_signal_clusters, market cache, OHLCV, fundamentals
    │     writes: ev_*.json
    │
    └→ profit_grinder
          uses: ev_*, entry_scores, raw_signal_clusters, expr cache, OHLCV
          writes: profit_*.json
```

Nightly live-scan path (separate from the grinder cache):
```
cache_builder → universe_ohlcv_daily.pkl
    → intermediate_cache_builder → intermediate_series/*.im
    → scan_engine → nightly signals (in-memory, logged by nightly.py)
```

All grinder outputs are mirrored to Railway's `file_mirror` table for backup. Consensus-pipeline outputs (written via `--output-dir local_runner/cache/consensus/`) suppress the mirror. Seed vault backs up SQLite tables to Railway nightly.
