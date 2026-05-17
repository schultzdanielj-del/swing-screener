# Regime Meter — Component Spec

## Purpose

Daily, after-close, per-sector market regime read for swing-trading posture. Two outputs:

1. **Per-sector forward-projection cones**: for each of 11 SPDR sector ETFs, a heatmap of historically-similar-day forward paths. Tells you where the market is statistically likely to go from here given today's regime.
2. **Per-sector 4-cell setup-class grids**: fade-long, fade-short, breakout-long, breakout-short. Each cell shows win-rate decile under a baseline exit and a ranked list of management techniques.

Regime-similarity-driven: today's 23-column market-state vector is matched against 10 years of history via K-nearest-neighbors. Every aggregate the dashboard surfaces is conditional on similar-day historical behavior, not unconditional averages.

Setup-agnostic by design — bypasses real setup detection. Uses synthetic-firing mechanics over the tradable universe to measure what's currently paying in current market conditions.

## EXACT spec

Not yet built. See Pending build.

## Details you need to know

- **Storage**: `regime_meter/cache/` inside this worktree (separate from main-repo caches; merged into main repo only after end-to-end verification).
- **Data sources** (read-only):
  - `local_runner/cache/market_series/` — per-instrument expression cache, ~272 instruments, 2016-onward
  - `local_runner/cache/market_ohlcv.pkl` — raw market OHLCV
  - `local_runner/cache/universe_ohlcv_daily.pkl` — full tradable universe OHLCV (~11,500 tickers)
  - `local_runner/cache/expr_series/SPY.npz` and `QQQ.npz` — ext zone display labels only (universe expression cache; 2020-onward)
- **History window**: 2016-01-01 onward (matches `HISTORY_START` in `market_cache_builder.py`).
- **One new data source required**: NAAIM Exposure Index, weekly xlsx from naaim.org. Worktree-local ingestion via `regime_meter/naaim_ingest.py` — discovers the current week's xlsx URL by scraping naaim.org each run, downloads, forward-fills weekly Wednesday readings across SPY's trading-day calendar, writes `regime_meter/cache/naaim_daily.parquet`. Does NOT modify the main repo's `market_cache_builder.py`. All other 23 columns derive from main-repo caches read-only.
- **Refresh cadence**: daily after market close. Hooks into `local_runner/nightly.py` once the pipeline is end-to-end verified.
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

## Pending build (step 1)

Build the regime-similarity engine and per-sector forward-projection cones. Fully scoped; no remaining design decisions.

### Regime vector — 24 columns

The similarity-math input. Z-scored over the 10-year history once; all subsequent distance computations use z-scored values.

| Category | Column | Source |
|---|---|---|
| Extension | SPY ext50 continuous | `market_series/SPY.npz` column `ext_avgc50_adr14` |
| Extension | SPY ext200 continuous | `market_series/SPY.npz` column `ext_avgc200_adr14` |
| Extension | QQQ ext50 continuous | `market_series/QQQ.npz` column `ext_avgc50_adr14` |
| Extension | QQQ ext200 continuous | `market_series/QQQ.npz` column `ext_avgc200_adr14` |
| Trend slope | SPY — linear regression slope of log(close) over 20 bars | derive from `market_series/SPY.npz` close |
| Trend slope | QQQ — same | derive from `market_series/QQQ.npz` close |
| Trend slope | DXY — same | derive from `market_series/DXYdot_INDX.npz` close |
| Trend slope | Gold — same | derive from `market_series/GLD.npz` close |
| Trend slope | Bitcoin — same | derive from `market_series/BTCdash_USDdot_CC.npz` close |
| Volatility | VIX rolling 5-year percentile rank | derive from `market_series/VIXdot_INDX.npz` close |
| Volatility | VIX / VIX3M ratio | derive from `market_series/VIXdot_INDX.npz` and `market_series/VIX3Mdot_INDX.npz` |
| Breadth | % of tradable-filter universe above 50-MA | derive from `universe_ohlcv_daily.pkl` |
| Sector rotation | Std-dev of 11 SPDR sector ETF 20-day returns | derive from 11 sector `.npz` closes |
| Sector rotation | XLY / XLP ratio | derive |
| Cross-asset | HYG / IEF ratio | derive |
| Cross-asset | 2s10s yield spread | `market_series/T10Y2Y_CALC.npz` close |
| Macro position | DXY % distance from 200-MA | derive |
| Macro position | Gold % distance from 200-MA | derive |
| Macro position | Bitcoin % distance from 200-MA | derive |
| Stockbee breadth | 10-day ratio of (stocks up 4% daily) / (stocks down 4% daily) | derive from `universe_ohlcv_daily.pkl` (tradable filter) |
| Stockbee breadth | Count of stocks up 25%+ over last quarter (63 bars) | derive from `universe_ohlcv_daily.pkl` (tradable filter) |
| Stockbee breadth | Count of stocks down 25%+ over last quarter (63 bars) | derive from `universe_ohlcv_daily.pkl` (tradable filter) |
| Stockbee breadth | RSP / SPY ratio | derive |
| Positioning / sentiment | NAAIM Exposure Index (raw weekly value, forward-filled to daily) | `regime_meter/cache/naaim_daily.parquet` via `regime_meter/naaim_ingest.py` |

**Display-layer columns** (not in similarity math, surfaced as plain-English labels in the dashboard only):

- SPY ext50 zone label — chop / upside_1 / upside_2 / downside_1 / downside_2 (read from `expr_series/SPY.npz` level columns)
- QQQ ext50 zone label — same for QQQ

### Tradable filter

Applied to all 4 universe-aggregate columns (% above 50-MA, 10-day 4% up/down ratio, stocks up 25%+ in quarter, stocks down 25%+ in quarter). Same filter across all four for time-series consistency.

- Close ≥ $1.00
- 20-day average dollar volume ≥ $4,000,000 (`dvol_20d` column in OHLCV pickle)
- 20-bar ADRP ≥ 1.8% (TC2000-style: `(mean(H/L) − 1) × 100`)

### Sector ETF list

11 SPDR sector ETFs: XLE, XLB, XLI, XLY, XLP, XLV, XLF, XLK, XLU, XLRE, XLC.

### Similarity engine

- Z-score every column over the full 10-year history once; persist per-column mean + std
- Distance metric: Euclidean over z-scored vector
- K (number of nearest historical neighbors): default 30, tunable
- Each match carries a distance value; dashboard surfaces match-strength confidence derived from the distance distribution

### Forward-projection cones

- For each of 11 SPDR sector ETFs, for every historical anchor day, compute forward log-return paths at horizons {5, 10, 20, 40 bars}
- Path normalization: log-return relative to anchor day's close (anchored at 0)
- Cone heatmap: 2D density (x = bars forward, y = log-return) over the K similar-day paths, colored by fraction of paths passing through each cell. Median path overlaid as a line.

### Horizon selector (empirically discovered, signal-vs-noise)

- For each candidate horizon, compare the distribution of K similar-day forward paths against the unconditional 10-year forward-path distribution at that horizon
- Pick the horizon at which the conditional-vs-unconditional divergence is largest. Divergence metric: KS-statistic by default, KL-divergence as alternative (decided at implementation).
- System reports the picked horizon and the divergence magnitude (confidence indicator displayed next to each cone)

### Build checklist

1. **NAAIM ingestion** (prerequisite, worktree-local): `regime_meter/naaim_ingest.py` discovers the current xlsx URL on naaim.org each run, downloads, parses with alias-tolerant header detection, forward-fills weekly Wednesday readings across SPY's trading-day calendar (read from main-repo `market_ohlcv.pkl` read-only), writes `regime_meter/cache/naaim_daily.parquet`. Default re-fetches network each run; `--no-fetch` re-derives from cached raw xlsx. Verify 2006+ history available before downstream work.
2. Build `regime_vector(date) -> 24-column vector` assembly. Two parts:
   - **2a** (worktree-local precompute): `regime_meter/breadth_ingest.py` reads the main-repo `universe_ohlcv_daily.pkl` read-only, applies the per-bar tradable filter, and writes the 4 universe-aggregate breadth columns (`pct_above_50ma`, `stockbee_4pct_ratio_10d`, `stockbee_25pct_up_count_63d`, `stockbee_25pct_down_count_63d`) to `regime_meter/cache/breadth_daily.parquet` over SPY's trading-day calendar. Default re-runs full rebuild each invocation.
   - **2b**: Build `regime_vector(date) -> 24-column vector` function that assembles the 24 columns from NAAIM parquet (item 1), breadth parquet (2a), market_series .npz (extension + yield-spread columns), and raw closes from `market_ohlcv.pkl` (trend slopes, ratios, %-distance-from-200MA, VIX percentile). Validates non-NaN before returning.
3. Run that function over every trading day 2016-01-01 → today. Persist as `regime_vector_history.parquet`.
4. Compute z-score normalization (per-column mean + std). Persist as `regime_vector_normalization.json`.
5. For each of 11 SPDR sector ETFs: compute forward log-return paths for horizons {5, 10, 20, 40 bars} from every historical anchor day. Persist as `sector_forward_paths/{sector}.npz`.
6. Implement K-NN similarity lookup over z-scored vector.
7. Implement signal-vs-noise horizon selector.
8. Output for today: similar-day list, picked horizon, per-sector forward-path heatmap data (sufficient to render later in step 2's UI).

### Storage layout

Within `regime_meter/cache/` (worktree-local):

- `naaim_daily.parquet` — daily forward-filled NAAIM Exposure Index, two columns (date, naaim_mean), built by `regime_meter/naaim_ingest.py`
- `breadth_daily.parquet` — daily universe-aggregate breadth, five columns (date + the four aggregates listed in build-checklist item 2a), built by `regime_meter/breadth_ingest.py`
- `regime_vector_history.parquet` — one row per trading day, 24 columns + date
- `regime_vector_normalization.json` — per-column z-score parameters (mean, std)
- `sector_forward_paths/{sector}.npz` — forward log-return tensors per sector (one file per SPDR)
