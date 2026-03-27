# EV Grinder — Phase 3 Correlative Scoring Engine

**Created:** 2026-03-14
**Updated:** 2026-03-26
**Status:** Complete (inc 1-6 + tree A/B). Setup-agnostic — runs on any setup type. Output saved locally, mirrored to Railway as backup.
**Script:** `scripts/ev_grinder.py`
**Pipeline step:** `ev_grind` (wired in scanperfect.py PySide6 app)
**Usage:** `python scripts/ev_grinder.py --setup <setup_type>`

---

## What It Does

The EV grinder takes the classified signal set from Phase 2 (signal grind + refinement grind) and scores every signal with three numbers:

- **Predicted win rate** — how likely this signal is to win, based on market conditions and stock characteristics at the time it fired
- **Predicted MFE (median winner move)** — how big the move is likely to be if it wins
- **EV** — expected value: `(WR × MFE) − ((1−WR) × 1.0 ADR assumed stop)`

It does NOT filter signals. Every signal that passed Phase 2 stays on the watchlist. The EV grinder ranks them so the best ones float to the top.

It also owns the **refinement depth replay** — reconstructing which refinement conditions eliminated which losing clusters, so the UI can offer a real-time refinement depth slider.

The output powers two UI sliders that together define the live nightly scan parameters:
- **Slider 1 (Refinement Depth):** How many of the 100 refinement conditions to enforce
- **Slider 2a/2b (Setup/Market Features):** Independent aggressiveness sliders for setup-specific and market features

---

## What It Replaces

- `market_grinder.py` — tested 256 instruments × 15,805 expressions for regime correlation
- `setup_grinder.py` — tested 6 OHLCV-derived features for setup-specific correlation
- The planned "combined optimizer" that would have merged the two

All three are replaced by a single unified engine where all features compete on equal footing.

---

## Inputs

### 1. Raw Signal Clusters (pre-refinement)
- **File:** `local_runner/cache/raw_signal_clusters_{setup}.json`
- **Per cluster:** `cluster_id`, `ticker`, `rightmost.date`, `rightmost.bar_idx`, `rightmost.close`, `leftward[]`, `classification`, `move_adr`, `adr_at_signal`, `entry_high`, `ceiling`, `is_example`

### 2. Refinement Output (post-refinement)
- **File:** `local_runner/cache/refinement_{setup}_*.json` (latest by timestamp)
- **Per signal:** `ticker`, `signal_date`, `bar_idx`, `close`, `classification`, `move_adr`, `adr_at_signal`, `entry_high`, `is_example`
- **Also contains:** `refinement_conditions_only` (conditions in lock order), `signal_conditions`, `all_conditions`

### 3. Market Series Cache
- **Location:** `local_runner/cache/market_series/*.npz`
- **Manifest:** `local_runner/cache/market_series/_manifest.json`
- **Content:** 256 instruments × ~16,051 expressions each. One .npz per instrument containing `data` array (n_bars × n_exprs) and `dates` array.
- **Expression ordering:** Defined in manifest `expr_names` array — same order as `brute_expressions.generate_all()`
- **Size:** ~80MB per .npz, ~20GB total

### 4. 5yr OHLCV Cache
- **File:** `local_runner/cache/universe_ohlcv_5yr.pkl`
- **Content:** Pickle dict, ticker → DataFrame with date/open/high/low/close/volume
- **Coverage:** ~4,169 tickers, 5 years daily data
- **Used for:** Computing 6 setup-specific OHLCV features

### 5. Fundamentals Cache
- **File:** `local_runner/cache/fundamentals_cache.json`
- **Content:** Per-ticker: `sector`, `industry`, `shares_outstanding`, `float_shares`
- **Coverage:** ~4,111 tickers with data. Remainder are errors (ETFs, SPACs, etc).
- **This is all we have.** No EPS, no revenue, no quarterly data.

---

## Outputs

### Primary Output File
- **Path:** `local_runner/cache/ev_{setup}_inc6_{timestamp}.json`
- **Mirrored to Railway as backup via file_mirror.py`
- **Read by:** UI (SPY overlay chart + dual sliders + stats bar)

### What the Output Contains

```
{
  "setup": "brko",
  "increment": 6,
  "created_at": "...",
  "total_time_s": 1234.5,
  "refinement_file": "refinement_brko_cl1958_pk39_20260326_162505.json",
  "clusters_file": "raw_signal_clusters_brko.json",

  // ── VERIFICATION ──
  "verification": {
    "depth_replay_passed": true,
    "features_passed": true,
    "feature_comparison": { ... },  // per-feature validation (DTSS only, empty for others)
    "dedup_corr_check_pre": {"passed": true, "violations": 0},
    "dedup_corr_check_post": {"passed": true, "violations": 0},
    "examples_scored": 46,
    "examples_total": 46
  },

  // ── FEATURE COVERAGE ──
  "feature_coverage": {
    "price": 4685, "adr": 4685, "dollar_volume_20d": 4685,
    "days_since_ipo": 4685, "rs_d1": 4681, "rs_w1": 3348,
    "market_cap": 3909, "volume_float_ratio": 3874,
    "rs_vs_sector": 3906, "sector_rs_vs_spy": 3906
  },

  // ── SLIDER 1 DATA: Refinement Depth ──
  "refinement_depth_map": {
    "conditions_in_order": [
      {
        "idx": 0, "depth": 1,
        "name": "ns_l_minl35_adr14",
        "low": 0.886, "high": 7.871,
        "clusters_killed": [12, 45, 67, ...],
        "cumulative_losers_remaining": 420,
        "cumulative_winners": 365,
        "cumulative_total": 785,
        "cumulative_wr": 0.465,
        "cumulative_peak": 8,
        "cumulative_avg": 2.1
      },
      ... // one per refinement condition
    ]
  },

  "depth_stats": [
    {"depth": 0, "total": 4685, "winners": 2385, "losers": 2300,
     "wr": 0.509, "peak_day": ..., "avg_day": ..., "avg_week": ...,
     "avg_month": ..., "avg_year": ...},
    {"depth": 1, ...},
    ...
  ],

  // ── SCREENING STATS ──
  "screening": {
    "setup": {"n_features": 10, "n_survivors_pre": 2, "n_survivors_post": 2},
    "market": {
      "n_instruments": 256, "n_expressions_per_instrument": 16051,
      "n_features_tested": ..., "n_survivors_pre": 51200,
      "n_survivors_post": 51200,
      "thresholds": {"wr_pp": 10, "mfe_adr": 1.0, "min_per_bucket": 8},
      "elapsed_s": 408.2, "n_errors": 0
    },
    "pre_refinement": {
      "n_signals": 4685, "n_setup_survivors": 2,
      "n_market_survivors": 51200, "n_total_survivors": 51202
    },
    "post_refinement": {
      "n_signals": 4342, "n_setup_survivors": 2,
      "n_market_survivors": 51200, "n_total_survivors": 51202
    }
  },

  // ── DEDUP STATS ──
  "dedup": {
    "pre_refinement": {
      "input": 51202, "after_pass1": ..., "after_pass15": ...,
      "output": ..., "dropped": ..., "drop_pct": ...,
      "corr_threshold": 0.95, "n_setup_kept": ..., "n_market_kept": ...,
      "n_instruments_kept": ...,
      "pass1_time_s": ..., "pass15_time_s": ...,
    },
    "post_refinement": { ... }
  },

  // ── SLIDER 2 DATA: Features (separate pre and post) ──
  "features_pre": [
    {
      "name": "SPY__ext_avgc50_adr14",
      "source": "market",           // "market" | "setup_ohlcv" | "setup_fundamentals"
      "instrument": "SPY",           // null for setup features
      "expression": "ext_avgc50_adr14", // null for setup features
      "screen_type": "both",         // "wr_only" | "mfe_only" | "both"
      "wr_spread": 0.25,             // D10−D1 win rate spread
      "mfe_spread": 2.1,             // D10−D1 median winner move spread (ADR)
      "direction": "ascending",      // "ascending" = higher is better
      "weight": 0.25,                // screening strength
      "decile_boundaries": [9 cutpoint values],
      "decile_wr": [10 values, D1 through D10],
      "decile_mfe": [10 values, null where <3 winners],
      "n_per_decile": [10 counts],
      "col_idx": 0
    },
    ...
  ],
  "features_post": [ ... ],  // same structure, post-refinement signal set

  // ── SIGNAL DATA (pre-refinement — all signals) ──
  "signals": [
    {
      "ticker": "AAOI",
      "date": "2024-02-12",
      "close": 21.01,
      "classification": "AUTO_WIN",
      "is_example": false,
      "move_adr": 5.865,
      "adr_at_signal": 1.669,
      "entry_high": 24.75,
      "cluster_id": 42,

      // Slider 1: depth at which this loser's cluster is eliminated
      // null = winner (always alive) or loser surviving all conditions
      "killed_at_depth": null,

      // Additive model scores
      "quality_score": 58.3,
      "setup_score": 65.1,
      "market_score": 51.5,
      "predicted_wr": 0.72,
      "predicted_mfe": 6.1,
      "ev": 3.13,

      // Tree model scores (if ev_tree_scorer available)
      "tree_quality_score": 55.2,
      "tree_setup_score": 60.3,
      "tree_market_score": 50.1,
      "tree_predicted_wr": 0.68,
      "tree_predicted_mfe": 5.8,
      "tree_ev": 2.74
    },
    ...
  ],
  "signals_post": [ ... ],  // same structure, post-refinement signal set only

  // ── VALIDATION ──
  "validation": {
    "calibration_pre": [
      {"decile": 1, "n": ..., "avg_quality_score": ...,
       "predicted_wr": ..., "actual_wr": ..., "wr_error": ...,
       "predicted_mfe": ..., "actual_mfe": ..., "avg_ev": ...},
      ... // 10 rows
    ],
    "calibration_post": [ ... ],
    "calibration_rmse_wr_pre": 0.114,
    "calibration_rmse_wr_post": 0.090
  },

  // ── REDUNDANCY ANALYSIS ──
  "redundancy": {
    "features_pre_only": [...],     // survived pre but not post = refinement captured
    "features_post_only": [...],    // survived post but not pre = rare
    "features_both": [...],         // genuine additive value
    "n_pre_only": ..., "n_post_only": ..., "n_both": ...
  },

  // ── TREE MODEL (if available) ──
  "tree_model": {
    "pre_refinement": {"cv_wr": {"cv_auc": ...}, ...},
    "post_refinement": { ... }
  },

  // ── LIVE SCAN CONFIG ──
  "scan_config": {
    "signal_conditions_count": 87,   // read dynamically from refinement JSON
    "refinement_conditions_count": 100,
    "default_refinement_depth": 100,
    "quality_score_range": {"min": 20.3, "max": 75.9},
    "setup_score_range": {"min": 15.2, "max": 82.1},
    "market_score_range": {"min": 18.7, "max": 71.3},
    "assumed_stop_adr": 1.0
  },

  // ── SUMMARY ──
  "summary": {
    "pre_refinement_signals": 4685,
    "post_refinement_signals": 4342,
    "refinement_conditions": 100,
    "clusters_killed": 343, "examples": 46,
    "total_features_tested": ...,
    "screening_survivors_pre": 51202,
    "screening_survivors_post": 51202,
    "deduped_survivors_pre": ...,
    "deduped_survivors_post": ...,
    "scoring_features_pre": ...,
    "scoring_features_post": ...
  }
}
```

---

## Feature Universe

### Market Regime Features (~4.1M)

256 instruments × ~16,051 expressions per instrument.

Each instrument's expression value on the signal date. Naming convention: `{instrument}__{expression}` (e.g., `SPY__ext_avgc50_adr14`, `UVXY__obv_slope_30`).

Instrument categories (from `market_cache_builder.py`):
- Broad market: SPY, QQQ, IWM, DIA
- Breadth/participation: RSP, QQEW, MDY, IJH, IJR, etc.
- Style/factor: IWF, IWD, MTUM, QUAL, VLUE, USMV, etc.
- Volatility: VIX, VIX3M, VVIX, SKEW, UVXY, SVXY, etc.
- Rates/yields: TNX, TYX, FVX, IRX
- Treasury ETFs: SHY, IEF, TLT, EDV, ZROZ, etc.
- Credit: HYG, JNK, LQD, AGG, EMB, etc.
- Risk-off: GLD, SLV, GDX, etc.
- Dollar: UUP
- Oil/energy: USO, CL=F, XOP, etc.
- Commodities: DBC, UNG, etc.
- Metals/materials: CPER, HG=F, LIT, XME, etc.
- Sectors (SPDR): XLK, XLF, XLE, XLV, XLI, XLY, XLP, XLB, XLRE, XLU, XLC
- Industry groups: SMH, SOXX, KRE, XBI, XHB, XRT, etc.
- Tech thematic: IGV, SKYY, HACK, CIBR, etc.
- Global macro: EEM, EFA, EWJ, EWZ, FXI, INDA, etc.
- Speculative: ARKK, BITO, GME, etc.
- Leveraged sentiment: TQQQ, SQQQ, UPRO, etc.
- Breadth internals (Stooq): NYMO, TICK, NYADV, TRIN, etc.
- Macro (FRED): HY spread, IG spread, 10Y-2Y, NFCI, etc.

Price-only instruments (VIX, yields, futures, FRED, Stooq) skip volume-based expressions.

### Setup-Specific Features — OHLCV-Derived (6)

Computed from the signal ticker's 5yr OHLCV data at the signal date:

| Feature | Computation | Source |
|---------|-------------|--------|
| `price` | Close at signal bar | OHLCV |
| `adr` | 14-bar average daily range | OHLCV |
| `dollar_volume_20d` | 20-day average (close × volume) | OHLCV |
| `days_since_ipo` | Bar index of signal bar (first bar in cache = 0) | OHLCV |
| `rs_d1` | 5-day rolling vol-adjusted intraday momentum, stock minus SPY (daily) | OHLCV |
| `rs_w1` | Same formula on weekly bars | OHLCV |

**RS formula:** `((C/O - 1) × 100) averaged over 5 bars × (avg_price / ATR50)`. Stock value minus SPY value. Computed on both daily and weekly timeframes.

These 6 features were validated against the DTSS setup_grinder run (2026-03-13). For new setup types, validation is skipped if no reference file exists (see TODO).

### Setup-Specific Features — Fundamentals-Derived (4)

Computed from fundamentals cache + OHLCV:

| Feature | Computation | Coverage |
|---------|-------------|----------|
| `market_cap` | `shares_outstanding × close` at signal date | ~2,782 tickers |
| `volume_float_ratio` | Signal-date volume / `float_shares` | ~2,752 tickers |
| `rs_vs_sector` | Ticker RS − avg RS of same-sector tickers on that date | ~2,791 tickers |
| `sector_rs_vs_spy` | Avg sector RS − SPY RS on that date | ~2,791 tickers |

Sector mapping comes from Yahoo Finance (`fundamentals_cache.json`). 11 sectors. Coverage is ~67% of universe.

### Features NOT Included (not sourced)

- ~~EPS growth QoQ / trailing 4Q~~ (scrapped — requires paid API)
- ~~Revenue growth QoQ / trailing 4Q~~ (scrapped — requires paid API)
- Float absolute level (have float_shares, but using volume/float ratio instead)
- Industry-level RS (have sector-level only)

These require a separate data pipeline (earnings API, quarterly reports). Not built, not planned for V1.

---

## Method — 8 Internal Steps

### Step 1: Build Feature Matrix

For each signal, look up every feature's value on the signal date.

**Market features:** Load each instrument's .npz, binary-search the dates array for the signal date (or most recent prior date within 5 days for holidays/calendar differences), grab the expression values at that row. Each instrument contributes ~16,051 feature columns.

**Setup OHLCV features:** Load the signal ticker's OHLCV from the 5yr cache. Compute price, ADR, dollar volume, days since IPO, RS daily, RS weekly at the signal bar index. SPY OHLCV needed for RS computation.

**Setup fundamentals features:** Look up sector/shares/float from fundamentals cache. Compute market cap (shares × close), volume/float ratio (signal-date volume / float). Sector RS features need all same-sector tickers' RS values aggregated — compute after individual RS is done.

**Processing strategy:** Do NOT build a full n_signals × 4M matrix in RAM. Process one market instrument at a time. Each instrument: load .npz, look up values for all signals, screen immediately (Steps 2-3), keep only survivors (capped at 200 per instrument). Discard the rest. Peak RAM per worker: ~80MB (one .npz) + signal lookup (tiny).

### Step 2: Univariate WR Screening

For each feature independently:
1. Exclude signals with NaN for this feature
2. If >50% NaN or any decile would have <8 signals, skip
3. Bucket remaining signals into 10 deciles by feature value
4. Compute win rate per decile
5. If D10-D1 spread ≥ 10 percentage points, feature survives

Catches both directions: features where high values predict wins AND features where low values predict wins.

### Step 3: Univariate MFE Screening

Same decile bucketing, but compute median `move_adr` among winners only per decile. Feature survives if D10-D1 spread ≥ 1.0 ADR.

### Step 4: Union Survivors

A feature passes if it cleared either the WR screen OR the MFE screen. Tagged as `wr_only`, `mfe_only`, or `both`.

### Step 5: Deduplication (3-pass)

Three-pass dedup, progressively reducing the survivor set:

**Pass 1 — Within-instrument (parallel).** For each instrument, dedup its survivors against each other using greedy Pearson correlation (threshold 0.95, min 50 overlap). Catches near-identical expressions like SMA20/SMA21/EMA20. Embarrassingly parallel across instruments. Typical reduction: 51K → 25K.

**Pass 1.5 — Same-expression (instant).** If the same expression name survived on multiple instruments (e.g., `slope_ratio_xavgc8_xavgc50` on VYM and NOBL), keep only the one with the strongest screening score. O(n) grouping, no correlation needed. Typical reduction: 25K → 2K. This is the biggest crush step.

**Pass 2 — Cross-instrument (batched).** Greedy dedup across all remaining survivors using exact vectorized Pearson correlation. Pre-allocates arrays, uses numpy dot products. Catches features from different instruments that are correlated (e.g., SPY_SMA20 ≈ QQQ_SMA20). Typical reduction: 2K → 1.5-1.8K.

### Step 6: Percentile Scoring + Category-Balanced Weighting

For each surviving feature:
- Compute percentile rank (0-100) for each signal using scipy.stats.rankdata
- Flip descending features so higher always = better
- Store decile boundaries (9 cutpoints) + WR/MFE per decile (10 values each) for interpolation

Category-balanced weighting: market features collectively get 50% of total weight, setup features get 50%. Within each category, features compete by individual screening strength (max of WR spread and normalized MFE spread). This prevents 1,800+ market features from drowning out 3 setup features by headcount.

### Step 7: Score Every Signal (Vectorized)

For each signal:
1. quality_score = category-balanced weighted average of percentile scores (0-100)
2. For predicted WR: un-flip percentile to raw distribution position, interpolate each feature's decile WR curve at that position (vectorized via np.searchsorted), take weighted average
3. Same for predicted MFE using decile MFE curves
4. EV = (predicted_wr × predicted_mfe) − ((1 − predicted_wr) × 1.0)

All computation is vectorized — loop over ~1,800 features, each doing numpy array ops on all signals at once. No Python loop over signals.

### Step 8: Validation

Sort all signals by predicted WR, split into deciles. For each decile, compare predicted avg WR vs actual WR. Same for MFE. Report calibration RMSE for both.

If predicted 85% WR signals actually win ~85%, the model is calibrated.

---

## Refinement Depth Replay

The refinement grinder found N conditions via beam search. The EV grinder replays these to determine which condition killed which losing clusters, using a greedy peel algorithm that finds the optimal ordering.

**Algorithm:**
1. Load all clusters with all their bar indices (rightmost + leftward)
2. Load expression cache
3. For each bar in each losing cluster, pre-compute pass/fail for every refinement condition
4. Start with all conditions active. Greedy peel: remove the one condition whose removal adds the fewest surviving losers. Record it. Repeat until all conditions are removed.
5. Reverse the peel order → optimal add order (conditions that kill the most losers are added first)
6. Walk the add order: condition at depth D kills a cluster when, after applying conditions 1 through D, ALL bars in that cluster fail at least one condition
7. Record: condition at depth D killed clusters [X, Y, Z]
8. Compute cumulative stats at each depth: total signals, winners, losers remaining, WR, peak/day, avg/day

This greedy peel produces a better ordering than replaying conditions in their original lock order — it maximizes the kill rate at each depth level.

**Per-signal output:** Each loser signal gets a `killed_at_depth` integer — the depth (1-N) at which its cluster was fully eliminated. Winners get `null` (always alive). Losers that survive all conditions also get `null`.

---

## Slider 1 — Refinement Depth

**Range:** 0 (pre-refinement, all signals) to N (full post-refinement, N = number of refinement conditions)

**UI behavior:** As slider moves right (0→100), losers are progressively eliminated. Winners never change. Stats bar updates: peak/day, avg/day, avg/week, avg/month, avg/year, total signals, win rate.

**How signals appear/disappear:** Each loser has `killed_at_depth`. If slider position ≥ `killed_at_depth`, signal is dead. Otherwise alive.

**SPY chart:** Dead signals fade out or disappear. Live signals show as circles — size = predicted MFE, color = predicted WR (green=high, red=low).

**Stats source:** `depth_stats` array, precomputed for every depth 0-100. Instant lookup, no client-side computation.

**Live scan uses:** The slider position = how many refinement conditions the nightly scan applies.

---

## EV Quality Filter (Independent Setup + Market Sliders)

**Range:** Continuous 0-100 each, mapped to the score ranges in the output data.

**What it does:**

Every signal has three scores:
- `quality_score` (0-100) — category-balanced 50/50 blend of setup and market
- `setup_score` (0-100) — weighted average of setup-only feature percentiles (price, ADR, dollar volume, RS, days since IPO, sector, float)
- `market_score` (0-100) — weighted average of market-only feature percentiles (deduped instrument expressions across 256 market instruments)

The Scan Tuning workspace has two independent sliders — one for setup features, one for market features. This lets you crank market aggressiveness (only trade in perfect market conditions) while leaving setup loose, or vice versa. The `quality_score` blended score is still available but the independent sliders give finer control.

**How scores are computed:**
1. For each surviving feature, compute every signal's percentile rank (0-100) using scipy.stats.rankdata
2. Flip descending features (lower raw value = better) so higher percentile always means better outcome
3. Setup weights: normalize setup feature weights to sum to 1.0 → `setup_score`
4. Market weights: normalize market feature weights to sum to 1.0 → `market_score`
5. Blended: `quality_score` = 50% setup + 50% market (same as before)

**Predicted WR and MFE** are computed separately via vectorized interpolation of each feature's decile WR/MFE curves at each signal's raw percentile position. Weighted average across features → predicted_wr and predicted_mfe. EV = (WR × MFE) - ((1-WR) × 1.0 ADR stop).

**Client-side computation:** Filter signals where `setup_score >= setup_slider` AND `market_score >= market_slider`. Two comparisons per signal = sub-millisecond.

**DTSS calibration reference (2026-03-15, 893 signals / 467 post-refinement):**
- Pre-refinement (893 signals): D1 actual WR = 19.1% → D10 = 64.1% (+45pp spread)
- Post-refinement (467 signals): D1 = 56.5% → D10 = 93.5% (+37pp spread)
- Post-refinement top 50%: 234 signals, 86.8% actual WR, ~39/year

**Live scan uses:** The slider positions = minimum setup_score and market_score for tonight's watchlist.

---

## SPY Overlay Visualization

The UI renders all signals on a horizontally scrollable SPY daily chart.

Each signal is a circle positioned at its date on the SPY X-axis:
- **Circle size:** Predicted MFE (bigger = higher expected move)
- **Circle color:** Predicted WR (green gradient = higher WR, red gradient = lower WR)
- **Tooltip:** Ticker, date, classification, predicted WR, predicted MFE, EV, move_adr (actual)

Both slider types affect which circles are visible. As Slider 1 (depth) increases, loser circles disappear. As Sliders 2a/2b increase, circles below the setup_score or market_score thresholds disappear.

**Stats bar (always visible):**
- Peak signals/day
- Average signals/day
- Average signals/week
- Average signals/month
- Average signals/year
- Total signals
- Win rate

Stats update in real-time as sliders move.

SPY OHLCV data is fetched separately (in the local 5yr OHLCV pickle or market cache). The EV grinder output just needs signal dates + scores.

---

## Processing Architecture

### Phase A — Setup Features (serial, fast, <15s)

Load 5yr OHLCV cache once. For each signal, compute 6 OHLCV features. Load fundamentals cache, compute 4 fundamentals features. Result: 10 features × n_signals.

### Phase B — Market Feature Screening (parallel, ~5-10 min)

Split 256 instruments across workers (ProcessPoolExecutor).

Each worker:
1. Loads one .npz (~80MB)
2. Builds date→row index for O(1) lookups
3. For each of ~16,051 expressions: looks up value at each signal's date, buckets into deciles, computes WR spread and MFE spread
4. Caps survivors at 200 per instrument (by screening strength)
5. Returns ONLY the survivors (feature name + values for all signals + screening stats)

**Both signal sets (pre and post refinement) are processed simultaneously per instrument.** Each worker screens features against both signal sets in one pass. This halves disk I/O since each .npz is loaded once, not twice.

**RAM per worker:** ~80MB (one .npz) + signal lookup arrays (tiny). With 16 workers: ~1.3GB peak. Total with main process OHLCV cache (~2GB): ~3.5GB. Safe on 16GB+ desktop.

### Phase C — Dedup + Score (serial + parallel, ~5-10 min)

Three-pass dedup (see Step 5). Pass 1 is parallel across instruments, Pass 1.5 is instant grouping, Pass 2 is batched vectorized correlation. Then percentile rank, category-balanced weighted scoring, decile interpolation for WR/MFE, validation.

### Phase D — Refinement Replay (serial, ~3-4 min)

Greedy peel of refinement conditions — removes conditions one at a time, finding the one whose removal adds the fewest surviving losers. Reverses the peel order to get optimal add order. Builds per-condition elimination map, `killed_at_depth` per loser signal, `depth_stats` array. Time scales with n_conditions × n_losing_clusters.

### Phase E — Tree Model A/B (optional, ~1-2 min)

If `ev_tree_scorer.py` is available, runs XGBoost cross-validated model on the same feature set for A/B comparison against the additive model. Outputs tree-based scores per signal. Non-fatal if unavailable.

### Estimated Total Runtime: ~15-25 minutes

---

## Edge Cases and Guardrails

- **NaN handling:** If >50% of signals have NaN for a feature, skip it. Signals with NaN get percentile 50 (neutral). Minimum 8 signals per decile or skip.
- **Date matching:** Market .npz dates are strings. Build dict for O(1) lookup. If signal date not in instrument's dates (holidays, different calendar), try up to 5 prior calendar days.
- **100% example pass:** All examples must get scored in both runs. Hard fail if any example is missing a score.
- **No mid-run aborts:** Log issues, skip bad features, keep going.
- **Setup feature validation:** For DTSS, spot-checks against preserved `setup_dtss_20260313_135931.json`. For other setup types, validation is skipped (no reference file). See TODO.
- **Per-instrument cap:** Each instrument keeps at most 200 survivors from screening (ranked by strength). Full cross-instrument dedup happens in the 3-pass dedup phase.

---

## Build Increments

### Increment 1: Script Skeleton + Data Loaders + Refinement Replay ✅

- Parse CLI args (`--setup <setup_type>`)
- Find and load latest refinement JSON + raw clusters JSON for any setup type
- Normalize both signal sets into common format: list of dicts with `ticker`, `date`, `bar_idx`, `close`, `classification`, `move_adr`, `is_example`, `cluster_id`
- Greedy peel algorithm for optimal condition ordering (not lock order replay)
- Build `killed_at_depth` per loser, `depth_stats` array, `refinement_depth_map`
- Print verification: depth 0 = all signals, depth N = post-refinement count
- **Test:** Run it, verify counts match refinement output exactly

### Increment 2: Setup Feature Computation ✅

- Compute 6 OHLCV features for all signals
- Compute 4 fundamentals features
- **Test:** For DTSS, spot-check against preserved `setup_dtss_20260313_135931.json`. For other setups, validation skipped.

### Increment 3: Market Feature Screening (Parallel) ✅

- Worker function: loads one .npz, screens ~16,051 features against both signal sets
- Per-instrument cap at 200 survivors (by screening strength)
- Orchestrator: runs workers across 256 instruments with ProcessPoolExecutor
- Returns survivors per instrument with values for all signals
- **Test:** Timing, survivor counts per instrument, RAM stays stable. Cross-check SPY features against regime model output.

### Increment 4: Union + Dedup ✅

- Combine market survivors + setup survivors
- Three-pass dedup: Pass 1 within-instrument (parallel), Pass 1.5 same-expression (instant), Pass 2 cross-instrument (batched vectorized Pearson)
- **Test:** Before/after counts, verify no >0.95 correlations remain

### Increment 5: Continuous Percentile Scoring + Signal EV ✅

- Percentile rank per signal per feature (scipy.stats.rankdata), direction-flipped
- Category-balanced weighting: 50% market / 50% setup
- Vectorized decile interpolation for predicted WR/MFE (np.searchsorted, no Python signal loop)
- quality_score (0-100), setup_score, market_score, predicted_wr, predicted_mfe, ev per signal
- feature_percentiles stripped from output (keeps file size manageable)
- Calibration check: D1 vs D10 actual WR printed
- **DTSS result:** Pre D1=19.1% → D10=64.1%. Post D1=56.5% → D10=93.5%. 65/65 examples scored.

### Increment 6: Decile Calibration + Redundancy Analysis ✅

- 10-row calibration table per signal set (pre + post refinement): predicted WR vs actual WR per quality_score decile, plus actual MFE
- Redundancy analysis: features in both pre+post sets (genuine), pre-only (refinement captured), post-only
- Output: `validation.calibration_pre/post` + `validation.calibration_rmse_wr_*` + `redundancy` dict
- File: `ev_{setup}_inc6_*.json`, saved locally, mirrored to Railway as backup
- **DTSS result:** WR calibration RMSE: 0.114 pre, 0.090 post. 247 features in both sets, 1,569 pre-only, 1,693 post-only.

### Increment 5b: Tree Model A/B Comparison ✅

- XGBoost + SHAP tree-based scoring as alternative to additive model (`scripts/ev_tree_scorer.py`)
- Cross-validated (5-fold) on same feature set, reports CV AUC
- Per-signal tree scores: `tree_quality_score`, `tree_setup_score`, `tree_market_score`, `tree_predicted_wr`, `tree_predicted_mfe`, `tree_ev`
- A/B comparison: D10-D1 spread for both models, disagreement analysis (>15pp WR diff), who's right more often
- Non-fatal: if `ev_tree_scorer` import fails, additive model runs alone
- Output: `tree_model` dict in output JSON with CV stats

### Increment 7: UI — SPY Chart + Scan Tuning Sliders (✅ DONE 2026-03-20)

- Scrollable SPY chart with signal bubbles (green=winner sized by move_adr, red=loser)
- Slider 1: refinement depth (reads `depth_progression` + `killed_at_depth`)
- Slider 2a: setup feature floor (reads `setup_score` per signal)
- Slider 2b: market feature floor (reads `market_score` per signal)
- Slider 3: WR floor (reads `predicted_wr` per signal)
- Entry/Exit tab system — entry sliders + exit strategy selection
- Stats bar always visible
- Slider positions auto-saved to scan_settings JSON on workspace close, restored on open

---

## Live Nightly Scan Integration

After the EV grinder has run and the user sets Scan Tuning sliders:

1. **Nightly scan** runs tonight's bars against signal conditions + refinement conditions (first N, per depth slider)
2. For each signal that fires: compute feature values (market cache lookup + OHLCV + fundamentals)
3. Compute setup_score and market_score using stored percentile scoring equation
4. Apply filters: reject signals below setup_score floor, market_score floor, or WR floor (per Scan Tuning slider positions from `scan_settings_{setup}.json`)
5. Compute EV score using stored feature weights + scoring curves
6. Rank by EV, highest to lowest
7. Top of list = what you trade tomorrow

Scoring is milliseconds per signal. All lookups from local caches.

---

## Key Design Decisions

- **Scoring, not filtering.** Phase 3 does not remove signals from the pipeline. It ranks them. The sliders let the user choose their own filtering threshold.
- **Additive model (primary) + Tree model (A/B).** Primary scorer: each feature contributes independently via percentile-weighted decile interpolation. XGBoost tree model runs as A/B comparison — captures interactions the additive model misses. Both sets of scores are saved per signal. The additive model is the production scorer; the tree model is for validation and future comparison.
- **Decile bucketing captures nonlinearity.** A feature that only matters at extremes is visible in the D10 vs D1 spread. Linear correlation would miss this.
- **Category-balanced weighting (50/50 market/setup).** Without balancing, 1,800+ market features drown out 3 setup features by headcount. Each category gets 50% of total weight. Within each category, features compete by screening strength.
- **Continuous percentile scoring, not discrete quartile levels.** setup_score and market_score are 0-100 per signal. Scan Tuning sliders set continuous thresholds. No arbitrary bucketing of signals into quality levels.
- **1.0 ADR assumed stop for EV calculation.** Losers don't have meaningful move_adr (setup broke). The loss side uses a fixed 1 ADR assumption. Adjustable parameter, not re-run required.
- **Pre AND post refinement.** Both runs tell different stories. Pre-refinement reveals genuine correlative features. Post-refinement reveals what's left after chart-level filtering. Features surviving both are the most valuable.
- **One output file, four sliders.** Everything the UI needs is in one JSON. No server round-trips for slider interactions. Client-side computation is trivially fast.
- **Setup-specific features are NOT from the expression cache.** The signal grind already mined all 16K expressions — anything in the cache that separates winners from losers would already be a signal/refinement condition. Setup features must come from outside the cache.
- **Whatever the sliders are set at = what the live nightly scan uses.** This is not a visualization-only tool. The slider positions (saved in `scan_settings_{setup}.json`) define production parameters.

---

## TODO

### TODO-1: Docstring example (cosmetic)
**File:** `scripts/ev_grinder.py` line 7
**Issue:** Docstring says `--setup dtss`. Change to `--setup <setup_type>`.
**Priority:** Low.

### TODO-2: Generic setup feature validation
**File:** `scripts/ev_grinder.py` lines 410, 1855-1859
**Issue:** `validate_setup_features()` hardcodes a DTSS reference URL (`setup_dtss_20260313_135931.json`) and gates validation with `if setup_type == "dtss"`. For non-DTSS setups, validation is silently skipped.
**Fix:** Look for a reference file matching `setup_{setup_type}_*.json` (local cache or Railway). If found, validate against it. If not found, skip with a note. After each setup type's first successful EV grind, save a snapshot as the reference file for future runs. Makes validation automatic for any setup type that has a prior run.
**Priority:** Medium. Not breaking anything — BRKO ran clean without it — but missing a safety net.

### TODO-3: Signal conditions count (FIXED)
**Issue:** Was hardcoded to 87 (DTSS value). Now reads dynamically from refinement JSON via `len(ref_data.get("signal_conditions", []))`.
**Status:** Fixed 2026-03-26. Noting for history.
