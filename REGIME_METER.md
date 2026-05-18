# Regime Meter — Component Spec

## Purpose

Daily, after-close, per-sector market regime read for swing-trading posture. Two outputs:

1. **Per-sector forward-projection cones**: for each of 11 SPDR sector ETFs, a heatmap of historically-similar-day forward paths. Tells you where the market is statistically likely to go from here given today's regime.
2. **Per-sector 4-cell setup-class grids**: fade-long, fade-short, breakout-long, breakout-short. Each cell shows win-rate decile under a baseline exit and a ranked list of management techniques.

Regime-similarity-driven: today's 24-column market-state vector is matched against 10 years of history via K-nearest-neighbors. Every aggregate the dashboard surfaces is conditional on similar-day historical behavior, not unconditional averages.

Setup-agnostic by design — bypasses real setup detection. Uses synthetic-firing mechanics over the tradable universe to measure what's currently paying in current market conditions.

## EXACT spec

Step 1 (regime-similarity engine and per-sector forward-projection cones) is built. Step 2 (UI) and step 3 (setup-class heatmaps) remain in Pending research.

### Pipeline

Eight scripts under `regime_meter/`, all worktree-local. Each is idempotent; re-running overwrites its output.

1. `naaim_ingest.py` — scrapes the current NAAIM Exposure Index xlsx URL from naaim.org each run, downloads, alias-tolerant header detection, forward-fills weekly Wednesday readings across SPY's trading-day calendar. Writes `cache/naaim_daily.parquet`. `--no-fetch` re-derives from cached raw xlsx.
2. `vix_ingest.py` — EODHD fetch of `VIX.INDX` with enough trailing history that the 5y percentile is computable on the first SPY trading day. Writes `cache/vix_daily.parquet`. `--no-fetch` re-derives from cached raw JSON.
3. `breadth_ingest.py` — reads main-repo `universe_ohlcv_daily.pkl` read-only, applies the per-bar tradable filter, computes the four universe-aggregate breadth columns over SPY's trading-day calendar. Writes `cache/breadth_daily.parquet`.
4. `regime_vector.py` — `regime_vector(date) -> pd.Series` assembly function returning the 24 columns. Reads the three worktree parquets plus main-repo `market_ohlcv.pkl` and `market_series/{SPY,QQQ}.npz`. Asserts at load time that each `.npz`'s column count equals `len(generate_all())`. Validates non-NaN before returning.
5. `regime_vector_history.py` — auto-probes the earliest viable date by walking forward from 2016-01-04 and trying `regime_vector(date)`; starts persisting at the first non-raising date. Loops over every SPY trading day from there to today. Writes `cache/regime_vector_history.parquet`.
6. `normalization_build.py` — per-column mean + sample std (ddof=1) over the full history. Aborts if any std is degenerate. Writes `cache/regime_vector_normalization.json`.
7. `forward_paths_build.py` — for each of the 11 SPDR sector ETFs, log-return paths at horizons 1..40 bars from every anchor that has 40 future trading days available. Writes `cache/sector_forward_paths/{sector}.npz` per sector, each holding `anchor_dates`, `paths` (n × 40 float64), `bars_forward`.
8. `regime_meter_today.py` — for a target date, builds its regime vector, z-scores against persisted normalization params, finds the K nearest history anchors by Euclidean distance, picks the horizon in {5, 10, 20, 40} with the largest KS-statistic vs the unconditional pool, writes `cache/regime_meter_today.json`. Default target = latest history row; `--date YYYY-MM-DD` and `--k N` override.

### Regime vector — 24 columns

The similarity-math input. Z-scored over the full history once; all subsequent distance computations use z-scored values.

| Category | Column | Source |
|---|---|---|
| Extension | SPY ext50 continuous | `market_series/SPY.npz` column `ext_avgc50_adr14` |
| Extension | SPY ext200 continuous | `market_series/SPY.npz` column `ext_avgc200_adr14` |
| Extension | QQQ ext50 continuous | `market_series/QQQ.npz` column `ext_avgc50_adr14` |
| Extension | QQQ ext200 continuous | `market_series/QQQ.npz` column `ext_avgc200_adr14` |
| Trend slope | SPY — linear regression slope of log(close) over 20 bars | derive from `market_ohlcv.pkl` SPY close |
| Trend slope | QQQ — same | derive from `market_ohlcv.pkl` QQQ close |
| Trend slope | DXY — same | derive from `market_ohlcv.pkl` `DXY.INDX` close |
| Trend slope | Gold — same | derive from `market_ohlcv.pkl` GLD close |
| Trend slope | Bitcoin — same | derive from `market_ohlcv.pkl` `BTC-USD.CC` close |
| Volatility | VIX rolling 5-year percentile rank | derive from `regime_meter/cache/vix_daily.parquet` (extended VIX) |
| Volatility | VIX / VIX3M ratio | extended VIX divided by `market_ohlcv.pkl` `VIX3M.INDX` close |
| Breadth | % of tradable-filter universe above 50-MA | `regime_meter/cache/breadth_daily.parquet` |
| Sector rotation | Std-dev of 11 SPDR sector ETF 20-day returns | derive from sector closes in `market_ohlcv.pkl` (XLC chain-linked, see Details) |
| Sector rotation | XLY / XLP ratio | derive from `market_ohlcv.pkl` |
| Cross-asset | HYG / IEF ratio | derive from `market_ohlcv.pkl` |
| Cross-asset | 2s10s yield spread | `market_ohlcv.pkl` `T10Y2Y_CALC` close (FRED — value IS the spread in pct points) |
| Macro position | DXY % distance from 200-MA | derive from `market_ohlcv.pkl` |
| Macro position | Gold % distance from 200-MA | derive from `market_ohlcv.pkl` |
| Macro position | Bitcoin % distance from 200-MA | derive from `market_ohlcv.pkl` |
| Stockbee breadth | 10-day ratio of (stocks up 4% daily) / (stocks down 4% daily) | `regime_meter/cache/breadth_daily.parquet` |
| Stockbee breadth | Count of stocks up 25%+ over last quarter (63 bars) | `regime_meter/cache/breadth_daily.parquet` |
| Stockbee breadth | Count of stocks down 25%+ over last quarter (63 bars) | `regime_meter/cache/breadth_daily.parquet` |
| Stockbee breadth | RSP / SPY ratio | derive from `market_ohlcv.pkl` |
| Positioning / sentiment | NAAIM Exposure Index (raw weekly value, forward-filled to daily) | `regime_meter/cache/naaim_daily.parquet` |

**Display-layer columns** (not in similarity math, surfaced as plain-English labels in the dashboard only):

- SPY ext50 zone label — chop / upside_1 / upside_2 / downside_1 / downside_2 (read from `expr_series/SPY.npz` level columns)
- QQQ ext50 zone label — same for QQQ

### Tradable filter

Applied to all 4 universe-aggregate breadth columns. Same filter across all four for time-series consistency.

- Close ≥ $1.00
- 20-day average dollar volume ≥ $4,000,000 (`dvol_20d` column in OHLCV pickle)
- 20-bar ADRP ≥ 1.8% (TC2000-style: `(mean(H/L) − 1) × 100`)

### Sector ETF list

11 SPDR sector ETFs: XLE, XLB, XLI, XLY, XLP, XLV, XLF, XLK, XLU, XLRE, XLC.

### Similarity engine

- Z-score every column over the full history once (persisted in `regime_vector_normalization.json`); all distance computations use z-scored values.
- Distance metric: Euclidean over z-scored vector.
- K (number of nearest historical neighbors): default 30, tunable via `--k` CLI flag.
- Neighbor pool is restricted to history anchors that have forward paths (the last 40 SPY trading days are excluded), so K is honored even when target is near the end of history.
- Each match carries a distance value; the daily payload surfaces neighbor distances unmodified for downstream confidence rendering.

### Forward-projection cones (consumed by step 2 UI)

- Per-sector forward log-return paths at horizons 1..40 bars from every eligible anchor, anchored at 0 on day 0.
- Cone heatmap (rendered downstream): 2D density (x = bars forward, y = log-return) over the K similar-day paths, colored by fraction of paths through each cell. Median path overlaid as a line.

### Horizon selector (signal-vs-noise)

- For each candidate horizon in {5, 10, 20, 40}, pool the K similar-day forward returns across all 11 sectors at the column corresponding to that horizon.
- Pool the unconditional 11-sector forward returns at the same horizon.
- Compute the Kolmogorov-Smirnov 2-sample statistic between conditional and unconditional pools.
- Pick the horizon with the largest KS-statistic. All four per-horizon KS-stats and p-values are reported in the daily payload.

### Daily output payload

`regime_meter/cache/regime_meter_today.json`:

- `target_date`
- `k`
- `picked_horizon` (one of 5/10/20/40)
- `divergence_metric` (`"ks_2samp"`)
- `divergence_by_horizon` — per-horizon `ks_stat`, `ks_pvalue`, `n_conditional`, `n_unconditional`
- `neighbors` — list of K objects, each `{date, distance}`, sorted by distance ascending
- `sector_paths` — dict keyed by sector ticker; value is a K × picked_horizon list of log returns relative to anchor close
- `bars_forward` — 1..picked_horizon

## Details you need to know

- **Storage**: `regime_meter/cache/` inside this worktree (separate from main-repo caches; merged into main repo only after end-to-end verification).
- **Main-repo data sources** (read-only):
  - `local_runner/cache/market_ohlcv.pkl` — raw market OHLCV (~270 instruments). Source for every raw close used in the regime vector except the four extension columns and the VIX 5y percentile.
  - `local_runner/cache/market_series/SPY.npz` and `QQQ.npz` — pre-computed expression cache. Source for the four `ext_avgc{50,200}_adr14` columns only. Column indices are looked up via `from local_runner.brute_expressions import generate_all` — the `.npz` manifest's `expr_names` list is NOT used because it can drift from the live expression set.
  - `local_runner/cache/universe_ohlcv_daily.pkl` — full tradable universe OHLCV (~11,500 tickers). Read only by `breadth_ingest.py`.
- **Worktree-local data sources**:
  - `regime_meter/cache/naaim_daily.parquet` (built by `naaim_ingest.py`) — NAAIM Exposure Index, weekly xlsx from naaim.org forward-filled to daily.
  - `regime_meter/cache/vix_daily.parquet` (built by `vix_ingest.py`) — extended VIX history from EODHD. Required because the 5y trailing percentile needs deeper history than `market_ohlcv.pkl` carries.
  - `regime_meter/cache/breadth_daily.parquet` (built by `breadth_ingest.py`) — four universe-aggregate breadth columns precomputed over the SPY trading-day calendar.
- **XLC chain-link.** XLC was carved out of XLK on 2018-06-19. For dates before that, the close matrix returned by `_load_close_matrix()` uses XLK closes scaled by `ratio = XLC[2018-06-19] / XLK[2018-06-19]` so 20-bar log returns are continuous across the splice. Sector return dispersion (col 13) and the per-sector forward-path tensors both rely on this; do not change it without recomputing `sector_forward_paths/`.
- **Earliest computable date is auto-probed, not hardcoded.** `regime_vector_history.py` walks forward from 2016-01-04 and starts persisting at the first date `regime_vector()` returns without raising. With current upstream caches the binding constraint is the 200-MA distance window on DXY/Gold/BTC (all start 2016-01-04 in `market_ohlcv.pkl`). If upstream caches are extended, the start moves automatically with no code change.
- **Refresh cadence**: runs daily as its own Windows scheduled task at 7:30 AM Eastern (after the 6:45 AM `local_runner/nightly.py` task) via `refresh.py`, the worktree-local orchestrator that chains the seven pipeline scripts. A freshness gate at the top polls main-repo `market_ohlcv.pkl` for up to 60 minutes, waiting for nightly.py to land the latest bars before the steps execute — a slow nightly run delays rather than corrupts the dashboard. All step output appends to `regime_meter/cache/refresh.log`. The `regime_meter_today.json` payload is consumed by the dashboard UI around 8 AM Eastern; same-evening refresh isn't viable because EODHD's daily bars don't settle until late evening.
- **Worktree isolation**: this component lives on the `regime-meter` branch and writes only to its own subdirectory until merged.

## Known bugs

None.

## Pending research

- **Step 2 — UI design via Claude Design** (claude.ai/design). Output is HTML/web prototype. Implementation framework decided after design lands. Candidates: local web app (served from host machine), Tauri-wrapped web app (desktop feel + web frontend), or static HTML report regenerated nightly.
- **Step 3 — setup-class heatmaps** (per-sector 4-cell grid):
  - Cells = (mechanic × direction): fade-long, fade-short, breakout-long, breakout-short
  - Breakout-long firing rule: D1 Darvas / N-bar high (locked)
  - Breakout-short firing rule: pending Dan's spec
  - Fade-long firing rule: pending Dan's spec
  - Fade-short firing rule: extension past statistical peak on 50-ext — anchored on existing extension chart features. Pending confirmation.
  - Management technique candidate set: pending Dan's spec
  - Cell win-rate definition: % positive under a baseline exit (e.g. 1R target, 1-ADR stop)
  - Cell "best management technique" metric: maximum captured MFE on historically-similar-day firings
  - Display per cell: win-rate decile (0–9) + ranked list of management techniques (top-3 or full ranking)
  - Low-sample cell handling: pending decision (hide / mark / always show)

## Pending build

None. Step 1 is shipped (see EXACT spec). Step 2 and Step 3 stay in Pending research until their open design questions are resolved.
