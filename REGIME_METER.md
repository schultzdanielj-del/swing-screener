# Regime Meter — Component Spec

## Purpose

Daily, pre-open, market regime read for swing-trading posture. Two scopes (overall market AND per-sector) across two output types:

1. **Forward-projection cones — overall market AND per-sector**: heatmaps of historically-similar-day forward paths. Built for SPY and QQQ (overall market) and the 11 SPDR sector ETFs (sector breakdown). Tells you where the market and each sector is statistically likely to go from here given today's regime.
2. **Per-sector 4-cell setup-class grids**: fade-long, fade-short, breakout-long, breakout-short. Each cell shows win-rate decile under a baseline exit and a ranked list of management techniques.

Regime-similarity-driven: today's 24-column market-state vector is matched against history via K-nearest-neighbors. History depth: as far back as data allows per column, with variable-dimension distance handling so dot-com era anchors are usable. Every aggregate the dashboard surfaces is conditional on similar-day historical behavior, not unconditional averages.

Setup-agnostic by design — bypasses real setup detection. Uses synthetic-firing mechanics over the tradable universe to measure what's currently paying in current market conditions.

## EXACT spec

Engine v2 — extended OHLCV cache + NaN-tolerant regime vector + shrinkage-weighted MSD distance + per-sector admissibility — is built. Dashboard v1 ships against the engine-v1 payload schema; Phase I will rebuild it against the engine-v2 schema. Step 3 (setup-class heatmaps) remains in Pending research.

### Pipeline

Ten scripts under `regime_meter/`, all worktree-local. Each is idempotent; re-running overwrites its output.

**Data ingestion (cache builders):**

1. `extended_ohlcv_ingest.py` — fetches every regime-vector input instrument from EODHD back to its earliest available date (SPY 1993-01-29, sectors 1998-12-22, DXY 1985, VIX3M 2007-11, US10Y 2006-03, etc.). 22 instruments + T10Y2Y_CALC derived inline (= US10Y − US2Y). Strips `.US` suffix on save so equity-ETF keys match the main-repo pickle naming. Writes `cache/market_ohlcv_extended.pkl`. `--force` re-fetches even if pickle exists.
2. `extended_market_series_build.py` — runs the main repo's expression library (imported as a Python module via `local_runner.brute_expressions.generate_all()`) against extended SPY and QQQ OHLCV. Writes `cache/market_series/{SPY,QQQ}.npz` and a fingerprint manifest. The four ext columns (`ext_avgc{50,200}_adr14`) populate back to ~1993-04 (SPY) / 1999-05 (QQQ) after the 50/200-bar warmup.
3. `naaim_ingest.py` — scrapes the current NAAIM Exposure Index xlsx URL from naaim.org each run, downloads, alias-tolerant header detection, forward-fills weekly Wednesday readings onto the extended SPY trading-day calendar (read from `market_ohlcv_extended.pkl`). Pre-2006-07-05 SPY dates remain NaN (no source data). Writes `cache/naaim_daily.parquet`. `--no-fetch` re-derives from cached raw xlsx.
4. `vix_ingest.py` — EODHD fetch of `VIX.INDX` back to inception (1990-01-02). Writes `cache/vix_daily.parquet`. `--no-fetch` re-derives from cached raw JSON.
5. `breadth_ingest.py` — reads main-repo `universe_ohlcv_daily.pkl` read-only, applies the per-bar tradable filter, computes the four universe-aggregate breadth columns over the main-pickle SPY trading-day calendar (2016+ only — universe extension is out of scope due to survivorship bias). Writes `cache/breadth_daily.parquet`. Pre-2016 dates remain NaN in regime-vector output for these four columns.

**Engine:**

6. `regime_vector.py` — `regime_vector(date) -> pd.Series` assembly function returning the 24 columns. NaN-tolerant: each per-column computer returns NaN when its inputs aren't sufficient (insufficient warmup, missing source date, non-positive close). The only raise is when `target` is not in SPY's trading-day calendar at all. Reads only worktree-local caches: `market_ohlcv_extended.pkl`, worktree `market_series/{SPY,QQQ}.npz`, and the three worktree parquets. XLC chain-linked to XLK pre-2018-06-19 and XLRE chain-linked to XLF pre-2015-10-08 inside the close matrix (used for the sector-dispersion column only — see Details).
7. `regime_vector_history.py` — walks every SPY trading day from extended calendar start (1993-01-29) through today. Persists every row where at least one of the 24 regime columns is non-NaN. Output parquet schema: `date` + 24 float columns + 24 boolean mask columns (`mask_<colname>` = True iff that column is non-NaN). The first ~19 SPY days are skipped because no column has finished its 20-bar warmup yet. Writes `cache/regime_vector_history.parquet`.
8. `normalization_build.py` — per-column mean + sample std (ddof=1) computed over only the rows where that column is non-NaN (each column normalizes against its own population, not against a row-intersection). Aborts if any std is degenerate. Output JSON records per-column `first_non_nan_date` and `n_non_nan_rows` alongside `mean` and `std`. Writes `cache/regime_vector_normalization.json`.
9. `forward_paths_build.py` — for each of the 11 SPDR sector ETFs, builds a forward log-return path tensor using **native** (not chain-linked) sector closes. Per-sector `anchor_dates` lengths vary: the 9 SPDR sectors carry ~6852 anchors back to 1998-12-22, XLRE carries ~2627 back to 2015-10-08, XLC carries ~1949 back to 2018-06-19. Writes one npz per sector with that sector's own `anchor_dates`, `paths` (n_sector × 40), `bars_forward`.
10. `regime_meter_today.py` — for a target date, builds its regime vector, z-scores against persisted normalization params, finds the K nearest history anchors via shrinkage-weighted MSD distance (see Similarity engine), filters per-sector by admissibility, picks the horizon in {5, 10, 20, 40} with the largest KS-statistic vs the per-sector union unconditional pool, writes `cache/regime_meter_today.json`. Default target = latest history row; `--date YYYY-MM-DD` and `--k N` override.

### Regime vector — 24 columns

The similarity-math input. Z-scored over the full history once; all subsequent distance computations use z-scored values.

All "extended pickle" references below mean `regime_meter/cache/market_ohlcv_extended.pkl` (worktree-local). All "market_series" references mean `regime_meter/cache/market_series/` (worktree-local).

| Category | Column | Source |
|---|---|---|
| Extension | SPY ext50 continuous | `market_series/SPY.npz` column `ext_avgc50_adr14` |
| Extension | SPY ext200 continuous | `market_series/SPY.npz` column `ext_avgc200_adr14` |
| Extension | QQQ ext50 continuous | `market_series/QQQ.npz` column `ext_avgc50_adr14` |
| Extension | QQQ ext200 continuous | `market_series/QQQ.npz` column `ext_avgc200_adr14` |
| Trend slope | SPY — linear regression slope of log(close) over 20 bars | derive from extended pickle SPY close |
| Trend slope | QQQ — same | derive from extended pickle QQQ close |
| Trend slope | DXY — same | derive from extended pickle `DXY.INDX` close |
| Trend slope | Gold — same | derive from extended pickle GLD close |
| Trend slope | Bitcoin — same | derive from extended pickle `BTC-USD.CC` close |
| Volatility | VIX rolling 5-year percentile rank | derive from `regime_meter/cache/vix_daily.parquet` (extended VIX, 1990+) |
| Volatility | VIX / VIX3M ratio | extended VIX divided by extended pickle `VIX3M.INDX` close |
| Breadth | % of tradable-filter universe above 50-MA | `regime_meter/cache/breadth_daily.parquet` (2016+ only) |
| Sector rotation | Std-dev of 11 SPDR sector ETF 20-day returns | derive from sector closes in extended pickle (XLC and XLRE chain-linked, see Details) |
| Sector rotation | XLY / XLP ratio | derive from extended pickle |
| Cross-asset | HYG / IEF ratio | derive from extended pickle |
| Cross-asset | 2s10s yield spread | extended pickle `T10Y2Y_CALC` close (value IS the spread in pct points, derived inline at fetch from US10Y − US2Y) |
| Macro position | DXY % distance from 200-MA | derive from extended pickle |
| Macro position | Gold % distance from 200-MA | derive from extended pickle |
| Macro position | Bitcoin % distance from 200-MA | derive from extended pickle |
| Stockbee breadth | 10-day ratio of (stocks up 4% daily) / (stocks down 4% daily) | `regime_meter/cache/breadth_daily.parquet` (2016+ only) |
| Stockbee breadth | Count of stocks up 25%+ over last quarter (63 bars) | `regime_meter/cache/breadth_daily.parquet` (2016+ only) |
| Stockbee breadth | Count of stocks down 25%+ over last quarter (63 bars) | `regime_meter/cache/breadth_daily.parquet` (2016+ only) |
| Stockbee breadth | RSP / SPY ratio | derive from extended pickle |
| Positioning / sentiment | NAAIM Exposure Index (raw weekly value, forward-filled to daily) | `regime_meter/cache/naaim_daily.parquet` (2006-07-05+) |

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

- **Per-column z-scoring against own population.** Every column's mean + std are computed over only that column's non-NaN rows (persisted in `regime_vector_normalization.json` with `first_non_nan_date` + `n_non_nan_rows` recorded per column). Each column normalizes against its own data, not against a row-intersection — so columns with shorter history aren't statistically penalized.
- **Shrinkage-weighted MSD distance.** For each (target, anchor) pair: count the columns non-NaN in BOTH (`N_obs`). The `N_obs` columns contribute their observed squared z-scored diffs. The remaining `24 − N_obs` columns each contribute the prior squared diff (`PRIOR_SQUARED_DIFF = 2.0`, mathematically derived as `E[(z_a − z_b)²]` for two independent z-scored values: `Var(z_a) + Var(z_b) = 1 + 1 = 2`). Estimated MSD is the average across all 24 slots; distance is its square root. Thin-evidence anchor distances are pulled toward the random-match baseline (sqrt(2) ≈ 1.41) — a partial-vector anchor must match both excellently AND have enough evidence to outrank a deep-evidence anchor. **No threshold on candidate pool.** Depth of evidence is built into the score.
- K (number of nearest historical neighbors): default 30, tunable via `--k` CLI flag.
- Target's own row is excluded from the candidate pool if present in history (no self-match).
- Each match carries `(distance, n_cols_non_nan)` — the daily payload surfaces both for downstream confidence rendering.

### Per-sector anchor admissibility

- After global top-K selection via shrinkage-weighted MSD, for each of the 11 SPDR sectors independently: filter the K candidates to those whose dates appear in that sector's own `anchor_dates` list (i.e. that sector has native data at the anchor AND for the next 40 trading days).
- Effective K per sector varies. The 9 SPDR sectors (XLE, XLB, XLI, XLY, XLP, XLV, XLF, XLK, XLU) have ~6852 admissible anchors back to 1998-12-22. XLRE has ~2627 back to 2015-10-08. XLC has ~1949 back to 2018-06-19.
- Pre-1999 candidate anchors have eff_K = 0 across every sector. They can still appear in the `neighbors` list (as similar regimes) but contribute no cones.

### Forward-projection cones (consumed by step 2 UI)

- Per-sector forward log-return paths at horizons 1..40 bars from every anchor where that sector has native data at anchor AND for the next 40 trading days. Native close only — chain-link is applied to the regime-vector close matrix (for the sector-dispersion scalar) but NOT to per-sector forward paths.
- Cone heatmap (rendered downstream): 2D density (x = bars forward, y = log-return) over the per-sector admissible similar-day paths (count = sector's effective K, varies by sector), colored by fraction of paths through each cell. Median path overlaid as a line.

### Horizon selector (signal-vs-noise)

- For each candidate horizon in {5, 10, 20, 40}, conditional pool = union of per-sector admissible similar-day forward returns at that horizon (variable per-sector counts depending on which neighbors are in each sector's anchor list).
- Unconditional pool = union of every sector's full `anchor_dates` × that horizon column (every available historical forward window across all 11 sectors).
- Compute the Kolmogorov-Smirnov 2-sample statistic between conditional and unconditional pools.
- Pick the horizon with the largest KS-statistic. All four per-horizon KS-stats and p-values are reported in the daily payload.

### Daily output payload

`regime_meter/cache/regime_meter_today.json`:

- `target_date`
- `k`
- `picked_horizon` (one of 5/10/20/40)
- `divergence_metric` (`"ks_2samp"`)
- `divergence_by_horizon` — per-horizon `ks_stat`, `ks_pvalue`, `n_conditional`, `n_unconditional`
- `neighbors` — list of K objects, each `{date, distance, n_cols_non_nan}`, sorted by distance ascending
- `sector_effective_k` — dict `{sector: int}` showing how many of the K candidates survived each sector's admissibility filter
- `anchor_era_distribution` — dict `{decade_label: count}` across the K neighbors. Labels: `1990s`, `2000s`, `2010s`, `2020s`.
- `sector_paths` — dict keyed by sector ticker; value is an `eff_K × picked_horizon` list of log returns relative to anchor close. Per-sector lengths vary with admissibility.
- `bars_forward` — 1..picked_horizon

## Details you need to know

- **Storage**: `regime_meter/cache/` inside this worktree. All component data (OHLCV pickle, market_series npz, parquets, JSON) lives here. The worktree is fully self-contained at runtime — the main repo's `market_ohlcv.pkl` and `market_series/` are NOT used.
- **Worktree-local data sources** (all consumed by `regime_vector.py`):
  - `regime_meter/cache/market_ohlcv_extended.pkl` (built by `extended_ohlcv_ingest.py`) — raw OHLCV for 22 instruments + `T10Y2Y_CALC` derived inline. Each instrument fetched back to its EODHD-earliest date.
  - `regime_meter/cache/market_series/{SPY,QQQ}.npz` (built by `extended_market_series_build.py`) — pre-computed expression cache against extended SPY/QQQ closes. Source for the four `ext_avgc{50,200}_adr14` columns. Column indices are looked up via `from local_runner.brute_expressions import generate_all` (main repo imported read-only as a Python module); the npz manifest's `expr_names` list is NOT used because it can drift from the live expression set.
  - `regime_meter/cache/naaim_daily.parquet` (built by `naaim_ingest.py`) — NAAIM Exposure Index forward-filled onto the extended SPY calendar. Pre-2006-07-05 dates remain NaN.
  - `regime_meter/cache/vix_daily.parquet` (built by `vix_ingest.py`) — VIX from inception (1990-01-02). Indexed by its own native dates (NOT reindexed to SPY) so the 5y trailing percentile can use every VIX bar including pre-SPY history.
  - `regime_meter/cache/breadth_daily.parquet` (built by `breadth_ingest.py`) — four universe-aggregate breadth columns over the main-pickle SPY trading-day calendar (2016+ only).
  - `regime_meter/cache/regime_vector_history.parquet` — 8363 rows from 1993-02-26 → today. Schema: `date` + 24 regime float columns + 24 `mask_<colname>` boolean columns.
  - `regime_meter/cache/regime_vector_normalization.json` — per-column `{mean, std, n_non_nan_rows, first_non_nan_date}`. Stds computed with `ddof=1`.
  - `regime_meter/cache/sector_forward_paths/{sector}.npz` — per-sector tensor with that sector's own `anchor_dates`, `paths`, `bars_forward`. Native (not chain-linked) sector closes.
- **Main-repo runtime accesses** (read-only):
  - `local_runner/cache/universe_ohlcv_daily.pkl` — full tradable universe (~11,500 tickers). Read only by `breadth_ingest.py`.
  - `local_runner/brute_expressions.py` — imported by `regime_vector.py` and `extended_market_series_build.py` as a Python module for live `generate_all()`. Not a data file.
- **Chain-link (regime-vector close matrix only).** XLC was carved out of XLK on 2018-06-19; XLRE was carved out of XLF on 2015-10-08. Inside `regime_vector.py::_load_close_matrix()`, pre-inception dates for the child use the parent's close scaled by `ratio = child[inception] / parent[inception]`. This makes the sector-dispersion column (a 20-bar log-return std across 11 sectors) continuous across the splice. **Chain-link is NOT applied to per-sector forward path tensors** — those use native closes only, so a 2010 XLRE anchor or 2017 XLC anchor doesn't exist in the forward paths file, and per-sector admissibility correctly drops those sectors as candidates for pre-inception anchors.
- **Shrinkage prior derivation.** `PRIOR_SQUARED_DIFF = 2.0` in `regime_meter_today.py` is the expected squared difference between two independent z-scored values: each column has unit variance after z-scoring, so for independent draws `E[(z_a − z_b)²] = Var(z_a) + Var(z_b) = 2`. Used as the missing-column contribution in the MSD computation. Mathematically derived, not tuned. Would only change if the underlying distribution assumption changes.
- **History earliest date is data-driven, not hardcoded.** `regime_vector_history.py` walks from the extended SPY calendar's first day (1993-01-29). The first ~19 SPY days are skipped because no column has finished its 20-bar warmup yet. From 1993-02-26 onward, every SPY day with at least one non-NaN regime column is persisted (with the per-column mask alongside).
- **No threshold on K-NN candidate pool.** Storage layer persists everything with ≥1 non-NaN column. Distance layer (shrinkage MSD) handles thin-evidence fairness mathematically via the prior contribution. Pre-2000 anchors CAN appear in top-K if their available features really match today — they just rarely do because thin anchors must overcome the prior penalty.
- **Refresh cadence**: runs daily as its own Windows scheduled task at 7:30 AM Eastern (after the 6:45 AM `local_runner/nightly.py` task) via `refresh.py`, the worktree-local orchestrator. A freshness gate at the top polls main-repo `market_ohlcv.pkl` for up to 60 minutes, waiting for nightly.py to land the latest bars before the steps execute — a slow nightly run delays rather than corrupts the dashboard. All step output appends to `regime_meter/cache/refresh.log`. The `regime_meter_today.json` payload is consumed by the dashboard UI around 8 AM Eastern; same-evening refresh isn't viable because EODHD's daily bars don't settle until late evening.
- **Worktree isolation**: this component lives on the `regime-meter` branch and writes only inside `regime_meter/`. No files are written outside the worktree.

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
- **Macro-core dual-mode engine** (considered, deferred). Alternative to the shipped shrinkage scoring: build a second K-NN engine on a 5-feature "macro-core" subset (SPY trend, VIX percentile, dollar trend, dollar 200MA, sector dispersion) that goes back to 1993. Two ranked lists rendered separately. Deferred in favor of shrinkage, which surfaces 1993+ anchors when they genuinely match, instead of by construction. Revisit if shrinkage never surfaces pre-2000 anchors despite the math allowing it.
- **Operational evaluation of shrinkage in practice.** Over time, track whether 1993-1999 anchors appear in top-K across a representative range of target dates. If never, the prior may be too penalizing for thin-evidence anchors — consider lowering the prior or implementing the dual-mode alternative then. (Today the prior is `PRIOR_SQUARED_DIFF = 2.0`, mathematically derived from z-scoring assumptions.)

## Pending build

Engine v2 (Phases A–D, plus the Phase B.5 SPY/QQQ market_series rebuild) shipped. Dashboard v1 still reads its own copy of the engine-v1 payload — Phase I will rebuild it against the engine-v2 schema after Phases E–H land. Phases are ordered by dependency.

### Phase E — SPY/QQQ overall-market cones
Goal: dashboard's first-class "where is the overall market headed" cones, alongside the sector breakdown.

- Extend `forward_paths_build.py` to also build forward-path tensors for SPY and QQQ. Treat them as just-another-sector for path computation (per-instrument `anchor_dates` from native closes — SPY back to ~1993-02-26, QQQ back to ~1999-03-10). Stored alongside sector tensors as `regime_meter/cache/sector_forward_paths/{SPY,QQQ}.npz`.
- Extend `regime_meter_today.py` payload to surface SPY/QQQ paths in a separate `overall_market_paths` dict (distinct from `sector_paths`) so the dashboard can render them as a hero row, not buried in the sector grid.
- **Locked design decision (2026-05-18): horizon picker uses sectors-only.** SPY and QQQ are rendered at the same picked horizon as the sectors but DO NOT contribute to the KS divergence calculation. Reasons: (a) SPY/QQQ are essentially weighted averages of the 11 sectors, so including them in the picker would double-count broad-market signal; (b) visual coherence — all 13 cones (11 sectors + SPY + QQQ) on the same x-axis range; (c) per-instrument KS picking would be statistically weak (K=30 vs the sector pool's K × 11 = 330). The picker stays balanced; SPY/QQQ provide overall-market visual context.

### Phase F — Baseline % up reference
Goal: every "% of paths ending positive" number has unconditional baseline context ("vs baseline +Npp").

- New build script: `baseline_hit_rate.py`. For each instrument (SPY, QQQ, 11 sectors) × each candidate horizon (5, 10, 20, 40), compute the unconditional % of historical N-day forward windows that ended positive over the full available history for that instrument.
- Persist as `regime_meter/cache/baseline_hit_rates.json`.
- `regime_meter_today.py` includes the relevant baseline per instrument × picked-horizon in the payload alongside the conditional % up.

### Phase G — Percentile context precompute
Goal: every conditional % up has a percentile rank in the distribution of all historical conditional % up values for that instrument × horizon.

- New build script: `conditional_distribution_build.py`. Walks every historical anchor date through the regime engine (using the engine-v2 shrinkage-weighted MSD distance + per-sector admissibility). For each anchor + each instrument + the picked horizon: compute the conditional % up. Aggregate into a distribution per instrument × horizon.
- Persist as `regime_meter/cache/conditional_distributions.npz`.
- `regime_meter_today.py` includes today's percentile rank per instrument in the payload.
- Expensive one-shot precompute; re-runs only when the underlying history/regime-vector pipeline changes meaningfully, not daily.

### Phase H — Regime-context panel data
Goal: dashboard can show today's raw regime context (breadth, VIX percentile, NAAIM, cross-asset ratios) as plain-English rows so user can sanity-check what the engine sees.

- Extend `regime_meter_today.py` payload to expose the raw 24-column regime vector values for today, along with column name and human-readable label per column. NaN cells stay NaN — dashboard renders them as "(no data)".
- No new build script needed; this is purely a payload extension.

### Phase I — Dashboard updates
Goal: surface everything from Phases E–H + the engine-v2 metadata in the rebuilt dashboard.

- Render SPY/QQQ overall cones as a hero row above the sector grid (or sized larger to distinguish).
- Each cone shows: median final % (existing), % up at picked horizon (new), vs-baseline (+Npp, Phase F), percentile rank (Phase G), effective K used (Phase D — varies per sector).
- New regime-context panel (Phase H): rows showing today's breadth %, VIX percentile, VIX/VIX3M, NAAIM, cross-asset ratios (XLY/XLP, HYG/IEF, RSP/SPY), DXY/Gold/BTC 200-MA distance, 2s10s. Plain-language labels.
- Anchor-era distribution mini-viz somewhere in the header (decade buckets — payload carries this from engine-v2).
- **Locked design decision (2026-05-18): three-view toggle = both, as independent toggles.** One toggle controls time-window (today-snapshot / recent-trend / historically-similar-days); a second independent toggle controls viz-style (DENSITY / FAN / PATHS). The shipped v1 dashboard's viz-style toggle was one half of the answer.
- Update `build_dashboard.py` to inline the extended payload structure.

### Phase J — refresh.py orchestration
Goal: morning scheduled task runs every build step in correct order, including new phases.

- Add `extended_ohlcv_ingest.py`, `extended_market_series_build.py`, and any Phase F/G new scripts to `refresh.py`'s step list in dependency order.
- Confirm `build_dashboard.py` runs as the final step so the chromeless Edge launcher always opens latest data.
- Phase G (percentile precompute) wired as one-shot, not daily.

### Dependency map
- E blocks F (path tensors include overall market) and is co-requisite of G.
- F is independent of G.
- H is independent (depends only on engine-v2 which is already shipped).
- I depends on E, F, G, H.
- J depends on all build scripts existing.
