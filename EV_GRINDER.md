# EV Grinder — Phase 3 Correlative Scoring Engine

**Script:** `scripts/ev_grinder.py`
**Usage:** `python scripts/ev_grinder.py --setup <setup_type>`
**Tree A/B helper:** `scripts/ev_tree_scorer.py` (XGBoost + SHAP)

> Status: built and producing results. Run standalone via `python scripts/ev_grinder.py --setup <setup_type>` after a refinement grind completes. **Not currently wired into the consensus pipeline orchestrator** (`run_consensus_pipeline.py`) — that's a separate integration task, not a "is it working?" question.

See `DEPENDENCY_MAP.md` for full I/O. See `SIGNAL_GRINDER.md` and `REFINEMENT_GRINDER.md` for the upstream phases that produce the inputs this grinder consumes.

---

## Purpose

Takes the classified signal set from Phase 2 (signal grind + refinement grind) and scores every signal with three numbers:

- **Predicted win rate (WR)** — how likely this signal is to win, based on market conditions and stock characteristics at the time it fired.
- **Predicted MFE** — how big the move is likely to be if it wins (median winner move in ADR units).
- **EV** — expected value, `(WR × MFE) − ((1 − WR) × 1.0 ADR assumed stop)`.

It does **NOT** filter signals. Every signal that passed Phase 2 stays on the watchlist. The EV grinder ranks them so the best ones float to the top.

It also owns the **refinement depth replay** — reconstructing which refinement conditions eliminated which losing clusters, so the UI can offer a real-time refinement depth slider.

The output powers two UI sliders for the nightly scan:

- **Slider 1 (Refinement Depth):** how many of the ~100 refinement conditions to enforce.
- **Slider 2a / 2b (Setup / Market Features):** independent aggressiveness sliders for setup vs market features.

---

## Inputs

| File | Producer | Used for |
|---|---|---|
| `local_runner/cache/raw_signal_clusters_{setup}.json` | pyramid_grinder cluster gathering | Pre-refinement signal set + classifications |
| `local_runner/cache/refinement_{setup}_*.json` (latest by timestamp) | pyramid_grinder refinement | Refinement conditions, post-refinement winner/loser/eliminated splits |
| `local_runner/cache/expr_series/*.npz` | expr_cache_builder | 15,805 per-ticker expressions for the depth replay step |
| `local_runner/cache/market_series/*.npz` + `_manifest.json` | market_cache_builder | ~256 instruments × ~16,051 expressions for market feature screening |
| `local_runner/cache/universe_ohlcv_daily.pkl` (or legacy `_5yr.pkl`) | cache_builder | OHLCV for the 6 setup-specific OHLCV features |
| `local_runner/cache/fundamentals_cache.json` | fetch_fundamentals | Sector, shares outstanding, float — for the 4 fundamentals-derived features |
| SQLite `setups` table | manual seed | Setup direction (long/short). No hardcoded directions; queried at runtime. |

---

## Pipeline

The script runs through six numbered increments plus an optional tree-model A/B comparison. Each increment produces verifiable intermediate state, written to the final JSON.

### Increment 1 — Refinement depth replay

For every losing cluster, computes `killed_at_depth`: which refinement condition (in greedy-peel order) first eliminates that cluster.

Greedy peel: starting from all conditions on, repeatedly remove the condition whose removal saves the fewest losers; record the order. The reverse is the "best to worst" lock order.

Output: per-condition `clusters_killed[]` and per-cluster `killed_at_depth`, used by the UI's refinement-depth slider to show "what happens if I enforce only the first N conditions?".

### Increment 2 — Setup feature computation

Per signal, computes 10 features using **date-based lookups** (never bar indices across data sources):

| Feature | Source | Notes |
|---|---|---|
| `feat_price` | OHLCV close at signal date | |
| `feat_adr` | 14-bar mean(H − L) | Skipped if < 14 bars of history |
| `feat_dollar_volume_20d` | 20-bar mean(close × volume) | |
| `feat_days_since_ipo` | row index of signal in OHLCV | |
| `feat_rs_d1` | (ticker daily RS) − (SPY daily RS) | TC2000-style RS: 5-bar avg intraday % move × ((C+C50)/2 / ATR50) |
| `feat_rs_w1` | (ticker weekly RS) − (SPY weekly RS) | Resampled weekly OHLCV |
| `feat_market_cap` | shares_outstanding × feat_price | From fundamentals cache |
| `feat_volume_float_ratio` | day's volume / float_shares | |
| `feat_rs_vs_sector` | feat_rs_d1 − sector mean RS that day | Sector from fundamentals |
| `feat_sector_rs_vs_spy` | sector mean RS that day | |

Coverage is reported per-feature. Signals with dates not in OHLCV cache get all-None values.

DTSS-only validator (`validate_setup_features`): pulls a preserved DTSS reference file from Railway and diffs the 6 OHLCV features against it. PASS thresholds are tight for exact values (price 0.01, ADR 0.01) and looser for derived values (RS 1.0, days_since_ipo 20.0). Other setups skip this validator.

### Increment 3 — Feature screening

Two parallel screens, both using **decile spread** (D10 − D1) as the discriminator:

- **Setup features**: 10 features, single-process, fast.
- **Market features**: ~256 instruments × ~16,051 expressions ≈ 4.1M features. Parallelized via `ProcessPoolExecutor` (one worker per instrument). Each worker loads its `.npz` via `_open_npz` (zstd-aware), looks up signal-date rows, runs `screen_features`, returns survivors.

`screen_features` per feature:
1. Skip if > 50% of signals have non-finite values.
2. Compute 9 decile boundaries (10th–90th percentile) on the valid subset.
3. Assign each signal to a decile (1–10).
4. Skip if any decile has fewer than `min_per_bucket` signals (default 8).
5. Compute D10 − D1 spread on win rate (in pp) and on median winner MFE (in ADR).
6. Survive if `wr_spread ≥ 10pp` OR `mfe_spread ≥ 1.0 ADR`.
7. Tag direction: ascending if D10 > D1, else descending.

Per-instrument cap: keep top 200 survivors per instrument, ranked by `max(wr_spread, mfe_spread / 10)`. Cross-instrument dedup happens in increment 4.

Both screens run twice: once on the full pre-refinement signal set, once on the post-refinement subset.

### Increment 4 — Cross-instrument dedup

Three-pass dedup with correlation threshold `0.95` and minimum overlap of 50 signals:

- **Pass 1 (within-instrument, parallel):** For each instrument, greedy correlation-based dedup of its own survivors. Catches `SMA20 ≈ SMA21 ≈ EMA20` overlaps.
- **Pass 1.5 (same-expression, instant):** O(n) grouping by expression name. Keep the strongest instance per expression across instruments. Crushes ~24K → ~2-3K because most survivors are the same expression on correlated instruments.
- **Pass 2 (cross-instrument, batched):** Vectorized exact Pearson correlation against all already-kept features. Pre-allocated arrays, no Python loop over signals.

Output is verified afterward: brute-force pairwise correlation check on up to 500 deduped features. Any pair with `|r| ≥ 0.95` and ≥ 50 valid overlap is a violation. Reported in the output's `verification.dedup_corr_check_pre/post` fields.

Runs once for pre and once for post.

### Increment 5 — Scoring curves + signal scoring

For every deduped feature:
1. Compute each signal's percentile rank (0–100) within the feature's valid distribution.
2. Flip descending features so higher percentile = better outcome. Missing values get neutral 50.
3. Preserve the decile WR/MFE curves from screening for downstream interpolation.

Then per signal, fully vectorized:

- `quality_score` = weighted average of all percentiles. Weights are **category-balanced**: market features as a group get 50% of total weight, setup features as a group get 50% — without this, ~1800 market features drown out ~3 setup features.
- `setup_score` = weighted average over setup features only.
- `market_score` = weighted average over market features only.
- `predicted_wr` = weighted average of decile-WR-interpolated values per feature, clipped to [0.01, 0.99].
- `predicted_mfe` = weighted average of decile-MFE-interpolated values, floored at 0.
- `ev = predicted_wr × predicted_mfe − (1 − predicted_wr) × 1.0`.

Decile interpolation uses 10 midpoints (5, 15, 25, …, 95) and clips percentiles to that range before linearly interpolating between the two surrounding deciles.

Calibration check after scoring: top 10% by `quality_score` should have higher actual win rate than bottom 10%. Warning printed if not.

### Increment 5b — Tree-model A/B (XGBoost + SHAP)

Optional. Imports `ev_tree_scorer.tree_score_signals`. Trains an XGBoost classifier on the top 100 features (by SHAP importance), produces out-of-fold (OOF) `predicted_wr` per signal, and reports a side-by-side D1 / D10 spread comparison vs the additive scorer.

Fails soft: if XGBoost isn't installed or training raises, the additive scores are kept and a warning is printed.

The output JSON adds a `tree_*` companion field per signal (`tree_quality_score`, `tree_predicted_wr`, etc.) when this step succeeds.

### Increment 6 — Decile calibration tables + redundancy analysis

**Calibration table** (per pre/post): bin signals into 10 deciles by `quality_score`, compute predicted vs actual WR and MFE per decile, plus per-decile RMSE on WR. Shows whether the score is *calibrated* (predictions match outcomes) vs merely *discriminative* (D10 > D1).

**Redundancy analysis**: which features survived screening pre-only vs post-only vs both.
- *Pre-only*: features captured by refinement (no longer additive once refinement is applied).
- *Both*: genuine additive value beyond refinement.
- *Post-only*: rare — features that only emerge as predictive after refinement strips out the big losers.

Both go into the final JSON under `validation` and `redundancy`.

### Hard fails

- All examples must be scored. If `examples_scored < examples_total`, the run aborts and writes nothing.

### Soft fails (printed warnings, run continues)

- DTSS-only validator can't reach Railway, or feature diffs exceed thresholds.
- Tree A/B import or training failure.
- Per-instrument `.npz` load failure during screening (instrument's features become NaN, dedup handles them).

---

## Output

**File:** `local_runner/cache/ev_{setup}_inc6_{timestamp}.json`
**Mirrored to Railway** via `file_mirror.mirror_file()`.
**Read by:** UI (SPY overlay chart + dual sliders + stats bar).

Top-level keys:

```
setup, increment, created_at, total_time_s
clusters_file, refinement_file
verification: { depth_replay_passed, features_passed, feature_comparison,
                dedup_corr_check_pre, dedup_corr_check_post,
                examples_scored, examples_total }
feature_coverage: { price: N, adr: N, ... } per setup feature
depth_stats: [{depth, total, winners, losers, wr, peak_day, ...}, ...]
refinement_depth_map: { conditions_in_order: [{depth, name, low, high,
                        clusters_killed, cumulative_*}, ...] }
screening: { setup, market, pre_refinement, post_refinement }
dedup: { pre_refinement, post_refinement } — pass timings + counts
validation: { calibration_pre, calibration_post, calibration_rmse_wr_* }
redundancy: { features_pre_only, features_post_only, features_both, n_* }
features_pre, features_post: full deduped feature dicts (no values arrays)
signals: pre-refinement signals with quality_score / setup_score / market_score /
         predicted_wr / predicted_mfe / ev / killed_at_depth (+ optional tree_* )
signals_post: same shape, post-refinement subset
scan_config: { signal_conditions_count, refinement_conditions_count,
               default_refinement_depth, *_score_range, assumed_stop_adr }
summary: counts of signals, features, screening survivors, deduped survivors
tree_model (optional): pre/post tree training metadata
```

---

## Key design decisions

- **Signals are scored, not filtered.** Phase 2 owns the WIN/LOSS classification. Phase 3 ranks within that population. EV grinder never adds or removes signals from the watchlist.
- **Date-based lookups for all cross-source alignment.** OHLCV, expression cache, market cache, and signal files all have different start dates. Every signal value is looked up by date, never by bar index. Holiday/calendar gaps are tolerated by walking back up to 5 calendar days when a signal date isn't found in an instrument's date array.
- **Setup direction is queried from the SQLite `setups` table.** No hardcoded directions in code. Direction-sensitivity is implicit: it shows up only as `direction: ascending|descending` per surviving feature, derived from D10-vs-D1 ordering.
- **Decile spread (not Spearman correlation, not regression).** Far more discriminating than quartiles for small example sets — random noise rarely produces a large D10 − D1 gap, while genuinely predictive features show pure extremes. Supports both WR (binary) and MFE (continuous, winners-only) on the same scale.
- **Category-balanced weighting in scoring.** Without this, the ~1800 market features statistically drown out the ~3 setup features. Each category gets 50% of total weight regardless of how many features it contains.
- **Pre and post refinement scored independently.** Pre uses all clusters; post uses only those that survived refinement. Comparing the two reveals which features were captured by refinement (pre-only) vs which add value beyond it (both).
- **`.npz` files always opened via `_open_npz`** (from `local_runner/expr_cache_builder`). Auto-detects zstd vs legacy zlib magic bytes. Never use raw `np.load` — it crashes on zstd files. (Bug 105dc6d, fixed.)
- **Tree A/B is informational.** Production sliders read the additive scores. Tree scores are written alongside for comparison and may inform future weighting changes.
- **Examples must all score.** Hard-fail if any example can't be scored. Examples are the only ground truth — silently dropping them invalidates the calibration check and the slider behavior.

---

## Open issues

- **Setup feature validator** in `validate_setup_features` is DTSS-only and depends on a preserved Railway file. Other setups skip validation entirely. The validator should be generalized or replaced with a setup-agnostic check.
- **Per-instrument 200-cap** on screening survivors is arbitrary. May exclude valid features when an instrument has many strong signals.
- **No example pass-through guarantee in screening.** Examples can in principle disappear from screening if their feature values land in deciles that get dropped for `min_per_bucket`. Currently relies on the post-scoring example check (hard fail) rather than upstream protection.
- **Railway dependency for DTSS reference** in the validator will fail when offline. Should fall back to a local copy.
- **Not wired into consensus orchestrator.** The script runs standalone and produces results today. Adding it to `run_consensus_pipeline.py` is a separate integration task — don't add without a design pass.
