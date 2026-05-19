# Regime Meter — Component Spec

## Purpose

Daily, pre-open, market regime read for swing-trading posture. Two scopes (overall market AND per-sector) across two output types:

1. **Forward-projection cones — overall market AND per-sector**: heatmaps of historically-similar-day forward paths. Built for SPY and QQQ (overall market) and the 11 SPDR sector ETFs (sector breakdown). Tells you where the market and each sector is statistically likely to go from here given today's regime.
2. **Per-sector 4-cell setup-class grids**: fade-long, fade-short, breakout-long, breakout-short. Each cell shows win-rate decile under a baseline exit and a ranked list of management techniques.

Regime-similarity-driven: today's 24-column market-state vector is matched against history via K-nearest-neighbors. History depth: as far back as data allows per column, with variable-dimension distance handling so dot-com era anchors are usable. Every aggregate the dashboard surfaces is conditional on similar-day historical behavior, not unconditional averages.

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

- **Step 2 — UI design and local implementation**:
  - **Shipped (Dashboard v1)**: Claude Design produced an HTML/React/CSS dashboard system under `regime_meter/dashboard/` — per-sector cones (DENSITY / FAN / PATHS viz toggle), header strip (target date, picked horizon, KS match strength, sector bias bar), 4×3 grid layout with legend + neighbor-distance histogram. `build_dashboard.py` inlines data + JSX so the page opens via `file://`; a Windows `.lnk` launches it chromeless in Edge for a desktop-app feel.
  - **Step 2 extensions** are in Pending Build below.
- **Step 3 — setup-class heatmaps** (per-sector 4-cell grid):
  - Cells = (mechanic × direction): fade-long, fade-short, breakout-long, breakout-short
  - Breakout-long firing rule (locked): D1 Darvas / N-bar high
  - Breakout-short firing rule: pending Dan's spec
  - Fade-long firing rule: pending Dan's spec
  - Fade-short firing rule (locked): extension past statistical peak on 50-ext — anchored on existing extension chart features
  - Management technique candidate set: pending Dan's spec
  - Cell win-rate definition (locked): % positive under a baseline exit of 1R target, 1-ADR stop
  - Cell "best management technique" metric (locked): maximum captured MFE on historically-similar-day firings
  - Display per cell (locked): win-rate decile (0–9) + full ranked list of management techniques (top-3 or all)
  - Low-sample cell handling: pending Dan's decision (hide / mark / always show)
- **Three-view dashboard toggle — specific views** (locked as a requirement; intent pending Dan's clarification): shipped Claude Design dashboard interpreted "three toggleable views" as viz-style (DENSITY / FAN / PATHS); original May 16/17 picker context suggests time-window oriented (today-snapshot / recent-trend / historically-similar-days). Pending Dan's confirmation of which intent the build should target.

## Pending build

Dashboard v1 (cone grid + header strip from Claude Design) is shipped. Everything below extends it. Phases are ordered by dependency — later phases depend on data structures or scripts produced by earlier phases.

### Phase A — Spec consolidation
Lock the Step 3 wording from May 16/17 transcripts (done in this doc). Reflect Dashboard v1 shipping in Step 2 (done). No code work — this section is the audit trail.

### Phase B — Cache extension to inception
Goal: every regime-vector input instrument extends back to the earliest date EODHD has data, not the current 2016-01-04 fetch floor. Required so dot-com era (1998-2002) is usable as anchor history — the current macro analogue.

- Refetch every instrument used by `regime_vector.py` (SPY, QQQ, 11 SPDR sectors, DXY.INDX, GLD, BTC-USD.CC, VIX.INDX, VIX3M.INDX, HYG, IEF, RSP, T10Y2Y_CALC) from its EODHD-earliest date.
- Write to a worktree-local extended cache file: `regime_meter/cache/market_ohlcv_extended.pkl`. Main repo's `market_ohlcv.pkl` is NOT modified — extension stays worktree-local until verified end-to-end, then proposed for absorption.
- Add **XLRE chain-link from XLF** (mirror the existing XLC-from-XLK pattern): pre-2015-10-08, XLRE close = XLF close × (XLRE[2015-10-08] / XLF[2015-10-08]). Without this, XLRE pre-2015 is empty and the SPDR sector dispersion column gets a discontinuity at the carve-out.
- Re-extend `regime_meter/cache/vix_daily.parquet` against the longer VIX history.
- Verify NAAIM ingestion captures full history back to July 2006 (the source's earliest publication). If naaim.org publishes only recent xlsx, combine multiple historical files.
- Skip extension of `universe_ohlcv_daily.pkl` (breadth universe). Out of scope per Dan's instruction — market + sectors only.

### Phase C — NaN-tolerant regime vector
Goal: regime vector can be computed for any date with partial column availability instead of raising on first missing input.

- `regime_vector.py` returns a vector with NaN for any column whose inputs aren't available at the target date, instead of raising. Drop the all-non-NaN assertion.
- `regime_vector_history.py` walks from the earliest date where the resulting vector has ≥ 12 non-NaN columns (the minimum threshold; below that the regime vector is too thin to encode a regime). Persists every row including its column-availability mask. Drop the "auto-probe earliest viable date walking from 2016-01-04" — replace with "walk from earliest date with any column populated, persist rows with ≥ 12 cols."
- `normalization_build.py` per-column mean/std uses only the dates where that column is non-NaN. Output JSON records the first non-NaN date per column.

### Phase D — Distance + similarity refactor (the "non-naive" part)
Goal: distance metric handles variable column counts without biasing toward sparser anchors. Per-sector admissibility for forward paths.

- `regime_meter_today.py` distance metric: **mean squared difference over the intersection of columns available in BOTH today's vector AND the anchor's vector**, then sqrt for Euclidean-like scale. Replaces sum-squared Euclidean — kills the "fewer columns mechanically lower distance" bias.
- Hard floor: anchors with fewer than 12 of 24 columns excluded from candidate pool entirely.
- **Per-sector anchor admissibility**: engine picks top-K candidate anchors using MSD across all available columns. For each sector, filter that candidate list to anchors where the sector has price data at the anchor date AND for the +H trading days after. Effective K varies per sector — sectors with longer histories see deeper anchor pools.
- Payload schema additions: per-anchor column count, per-sector effective K, anchor-era distribution (count of anchors per decade or 3-year bucket).

### Phase E — SPY/QQQ overall-market cones
Goal: dashboard's first-class "where is the overall market headed" cones, alongside the sector breakdown.

- Extend `forward_paths_build.py` to also build forward-path tensors for SPY and QQQ. Stored alongside sector tensors in `regime_meter/cache/sector_forward_paths/{SPY,QQQ}.npz`. Treat them as just-another-sector for path computation.
- Extend `regime_meter_today.py` payload to surface SPY/QQQ paths in a separate `overall_market_paths` dict (distinct from `sector_paths`) so the dashboard can render them as a hero row, not buried in the sector grid.

### Phase F — Baseline % up reference
Goal: every "% of paths ending positive" number has unconditional baseline context ("vs baseline +Npp").

- New build script: `baseline_hit_rate.py`. For each instrument (SPY, QQQ, 11 sectors) × each candidate horizon (5, 10, 20, 40), compute the unconditional % of historical N-day forward windows that ended positive over the full available history.
- Persist as `regime_meter/cache/baseline_hit_rates.json`.
- `regime_meter_today.py` includes the relevant baseline per instrument × picked-horizon in the payload alongside the conditional % up.

### Phase G — Percentile context precompute
Goal: every conditional % up has a percentile rank in the distribution of all historical conditional % up values for that instrument × horizon.

- New build script: `conditional_distribution_build.py`. Walks every historical anchor date through the regime engine (using the Phase D refactored MSD-with-admissibility logic). For each anchor + each instrument + the picked horizon: compute the conditional % up. Aggregate into a distribution per instrument × horizon.
- Persist as `regime_meter/cache/conditional_distributions.npz`.
- `regime_meter_today.py` includes today's percentile rank per instrument in the payload.
- Expensive one-shot precompute; re-runs only when the underlying history/regime-vector pipeline changes meaningfully, not daily.

### Phase H — Regime-context panel data
Goal: dashboard can show today's raw regime context (breadth, VIX percentile, NAAIM, cross-asset ratios) as plain-English rows so user can sanity-check what the engine sees.

- Extend `regime_meter_today.py` payload to expose the raw 24-column regime vector values for today, along with column name and human-readable label per column.
- No new build script needed; this is purely a payload extension.

### Phase I — Dashboard updates
Goal: surface everything from Phases E–H + the engine-v2 metadata from Phase D in the existing dashboard. Resolve the three-view toggle question.

- Render SPY/QQQ overall cones as a hero row above the sector grid (or sized larger to distinguish).
- Each cone shows: median final % (existing), % up at picked horizon (new), vs-baseline (+Npp) (new), percentile rank (new), effective K used (new — may be < 30 for younger sectors).
- New regime-context panel: rows showing today's breadth %, VIX percentile, VIX/VIX3M, NAAIM, cross-asset ratios (XLY/XLP, HYG/IEF, RSP/SPY), DXY/Gold/BTC 200-MA distance, 2s10s. Plain-language labels.
- Anchor-era distribution mini-viz somewhere in the header (e.g., decade buckets showing where today's analogs cluster — "anchors cluster in 1999-2001 + 2018").
- Resolve the three-view toggle (Dan clarifies time-window vs viz-style intent) and ship that resolution.
- Update `build_dashboard.py` to inline the extended payload structure.

### Phase J — refresh.py orchestration
Goal: morning scheduled task runs every build step in correct order, including new phases.

- Add new build scripts to `refresh.py`'s step list in dependency order.
- Confirm `build_dashboard.py` runs as the final step so the chromeless Edge launcher always opens latest data.
- Phase G (percentile precompute) wired as one-shot, not daily.

### Dependency map
- B blocks C, D, E, F, G, H
- C blocks D, H
- D blocks E, G
- E blocks F (path tensors include overall market) and is co-requisite of G
- F is independent of G
- H is independent after C
- I depends on E, F, G, H
- J depends on all build scripts existing
