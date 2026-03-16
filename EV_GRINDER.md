# EV Grinder — Phase 3 Correlative Scoring Engine

**Created:** 2026-03-14
**Status:** Inc 1-5 complete. Script functional, output mirrored to Railway.
**Script:** `scripts/ev_grinder.py`
**Pipeline step:** `ev_grind` (wired in `pipeline_agent.py`)

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
- **Slider 2 (EV Quality):** How aggressively to filter signals by correlative feature quality

---

## What It Replaces

- `market_grinder.py` — tested 256 instruments × 15,805 expressions for regime correlation
- `setup_grinder.py` — tested 6 OHLCV-derived features for setup-specific correlation
- The planned "combined optimizer" that would have merged the two

All three are replaced by a single unified engine where all features compete on equal footing.

---

## Inputs

### 1. Raw Signal Clusters (pre-refinement)
- **File:** `local_runner/cache/raw_signal_clusters_dtss.json`
- **Content:** 893 clusters (365 WIN, 528 LOSS)
- **Per cluster:** `cluster_id`, `ticker`, `rightmost.date`, `rightmost.bar_idx`, `rightmost.close`, `leftward[]`, `classification`, `move_adr`, `adr_at_signal`, `entry_high`, `ceiling`, `is_example`

### 2. Refinement Output (post-refinement)
- **File:** `local_runner/cache/refinement_dtss_cl102_pk5_20260313_122818.json`
- **Content:** 467 signals (365 WIN, 102 LOSS) + 426 eliminated losers
- **Per signal:** `ticker`, `signal_date`, `bar_idx`, `close`, `classification`, `move_adr`, `adr_at_signal`, `entry_high`, `is_example`
- **Also contains:** `refinement_conditions_only` (100 conditions in lock order), `signal_conditions` (87 conditions), `all_conditions` (182 combined)

### 3. Market Series Cache
- **Location:** `local_runner/cache/market_series/*.npz`
- **Manifest:** `local_runner/cache/market_series/_manifest.json`
- **Content:** 256 instruments × 15,805 expressions each. One .npz per instrument containing `data` array (n_bars × 15,805) and `dates` array.
- **Expression ordering:** Defined in manifest `expr_names` array — same order as `brute_expressions.generate_all()`
- **Size:** ~80MB per .npz, ~20GB total

### 4. 5yr OHLCV Cache
- **File:** `local_runner/cache/universe_ohlcv_5yr.pkl`
- **Content:** Pickle dict, ticker → DataFrame with date/open/high/low/close/volume
- **Coverage:** ~4,167 tickers, 5 years daily data
- **Used for:** Computing 6 setup-specific OHLCV features

### 5. Fundamentals Cache
- **File:** `local_runner/cache/fundamentals_cache.json`
- **Content:** Per-ticker: `sector`, `industry`, `shares_outstanding`, `float_shares`
- **Coverage:** 2,791/4,169 with sector, 2,752/4,169 with float, 2,782/4,169 with shares. 831 errors (ETFs, SPACs, etc)
- **This is all we have.** No EPS, no revenue, no quarterly data.

---

## Outputs

### Primary Output File
- **Path:** `local_runner/cache/ev_{setup}_{timestamp}.json`
- **Mirrored to:** Railway via `file_mirror.py`
- **Read by:** UI (SPY overlay chart + dual sliders + stats bar)

### What the Output Contains

```
{
  "setup": "dtss",
  "created_at": "...",
  "refinement_file": "refinement_dtss_cl102_pk5_20260313_122818.json",
  "clusters_file": "raw_signal_clusters_dtss.json",

  // ── SLIDER 1 DATA: Refinement Depth ──
  "refinement_depth_map": {
    "conditions_in_order": [
      {
        "idx": 0,
        "name": "ns_l_minl35_adr14",
        "low": 0.886,
        "high": 7.871,
        "clusters_killed": [12, 45, 67, ...],
        "cumulative_losers_remaining": 420,
        "cumulative_winners": 365,
        "cumulative_total": 785,
        "cumulative_wr": 0.465,
        "cumulative_peak": 8,
        "cumulative_avg": 2.1
      },
      ... // 100 entries, one per refinement condition
    ]
  },

  "depth_stats": [
    {"depth": 0, "total": 893, "winners": 365, "losers": 528,
     "wr": 0.409, "peak": 12, "avg_day": 2.5, "avg_week": ...,
     "avg_month": ..., "avg_year": ...},
    {"depth": 1, ...},
    ...
    {"depth": 100, "total": 467, "winners": 365, "losers": 102,
     "wr": 0.782, "peak": 5, "avg_day": 1.3, ...}
  ],

  // ── SLIDER 2 DATA: EV Quality Filter ──
  // Surviving features with scoring curves
  "features": [
    {
      "name": "SPY__ext_avgc50_adr14",
      "source": "market",           // "market" | "setup_ohlcv" | "setup_fundamentals"
      "instrument": "SPY",           // null for setup features
      "expression": "ext_avgc50_adr14", // null for setup features
      "screen_type": "both",         // "wr_only" | "mfe_only" | "both"
      "wr_spread": 0.25,             // Q4−Q1 win rate spread
      "mfe_spread": 2.1,             // Q4−Q1 median winner move spread
      "direction": "ascending",      // "ascending" = higher is better, "descending" = lower is better
      "weight": 0.25,                // screening strength, used for weighted scoring
      "decile_boundaries": [9 cutpoint values],
      "decile_wr": [10 values, D1 through D10],
      "decile_mfe": [10 values, D1 through D10, null where <3 winners],
      "n_per_decile": [10 counts],
      "weight": 0.25
    },
    ...
  ],

  // ── SIGNAL DATA (one entry per all 893 pre-refinement signals) ──
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

      // Slider 1: when does this signal die as refinement depth increases?
      // null = winner (always alive) or loser that survives all 100 conditions
      // integer = depth at which this signal's cluster is fully eliminated
      "killed_at_depth": null,

      // Slider 2: continuous quality score (0-100)
      "quality_score": 58.3,

      // EV scores (computed from pre-refinement model)
      "predicted_wr": 0.72,
      "predicted_mfe": 6.1,
      "ev": 3.13
    },
    ...
  ],

  // ── SCREENING STATS ──
  "screening": {
    "pre_refinement": {
      "n_signals": 893,
      "n_features_tested": 4045266,
      "n_wr_survivors": ...,
      "n_mfe_survivors": ...,
      "n_union": ...,
      "n_after_dedup": ...,
      "thresholds": {"wr_pp": 10, "mfe_adr": 1.0, "dedup_corr": 0.95}
    },
    "post_refinement": {
      // same structure, 467 signals
    }
  },

  // ── VALIDATION ──
  "validation": {
    "pre_refinement": {
      "deciles": [
        {"decile": 1, "n": 89, "predicted_wr": 0.30, "actual_wr": 0.28,
         "predicted_mfe": 4.1, "actual_mfe": 3.8},
        ...
      ],
      "calibration_rmse_wr": ...,
      "calibration_rmse_mfe": ...
    },
    "post_refinement": { ... }
  },

  // ── REDUNDANCY ANALYSIS ──
  "redundancy": {
    "features_pre_only": [...],     // survived pre but not post = refinement captured it
    "features_post_only": [...],    // survived post but not pre = rare
    "features_both": [...],         // genuine additive value
  },

  // ── LIVE SCAN CONFIG ──
  "scan_config": {
    "signal_conditions_count": 87,
    "refinement_conditions_count": 100,
    "default_refinement_depth": 100,
    "quality_score_range": {"min": 20.3, "max": 75.9},
    "assumed_stop_adr": 1.0
  }
}
```

---

## Feature Universe

### Market Regime Features (~4,045,280)

256 instruments × 15,805 expressions per instrument.

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

These 6 features were validated in the setup_grinder run (2026-03-13). Results preserved in `setup_dtss_20260313_135931.json` — use for spot-check validation.

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

- EPS growth QoQ / trailing 4Q
- Revenue growth QoQ / trailing 4Q
- Float absolute level (have float_shares, but using volume/float ratio instead)
- Industry-level RS (have sector-level only)

These require a separate data pipeline (earnings API, quarterly reports). Not built, not planned for V1.

---

## Method — 8 Internal Steps

### Step 1: Build Feature Matrix

For each signal, look up every feature's value on the signal date.

**Market features:** Load each instrument's .npz, binary-search the dates array for the signal date (or most recent prior date for holidays/calendar differences), grab the expression values at that row. Each instrument contributes 15,805 feature columns.

**Setup OHLCV features:** Load the signal ticker's OHLCV from the 5yr cache. Compute price, ADR, dollar volume, days since IPO, RS daily, RS weekly at the signal bar index. SPY OHLCV needed for RS computation.

**Setup fundamentals features:** Look up sector/shares/float from fundamentals cache. Compute market cap (shares × close), volume/float ratio (signal-date volume / float). Sector RS features need all same-sector tickers' RS values aggregated — compute after individual RS is done.

**Processing strategy:** Do NOT build a 893 × 4M matrix in RAM. Process one market instrument at a time. Each instrument: load .npz, look up values for all signals, screen immediately (Steps 2-3), keep only survivors. Discard the rest. Peak RAM per worker: ~80MB (one .npz) + signal lookup (tiny).

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

### Step 5: Deduplication

Among survivors, compute pairwise correlation of each feature's value series across all signals. Greedy dedup: rank survivors by screening strength (max of WR spread and MFE spread normalized), walk best to worst, drop any feature correlating >0.95 with an already-kept feature.

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

The refinement grinder found 100 conditions via beam search. They were locked in sequence: condition 1 first, condition 100 last. The EV grinder replays this sequence to determine which condition killed which losing clusters.

**Algorithm:**
1. Load all 893 clusters with all their bar indices (rightmost + leftward)
2. Load expression cache
3. For each bar in each cluster, check if it passes condition 1's bounding box
4. A losing cluster is "killed" by condition N when: after applying conditions 1 through N, ALL bars in that cluster fail at least one condition. Condition N is the one that tipped it over.
5. Record: condition N killed clusters [X, Y, Z]
6. Also record cumulative stats at each depth: total signals, winners, losers remaining, WR, peak/day, avg/day

This gives the UI everything it needs for Slider 1. Peeling off condition 100 brings back the clusters it killed. Peeling off 99 AND 100 brings back both their clusters. Etc.

**Per-signal output:** Each loser signal gets a `killed_at_depth` integer — the depth (1-100) at which its cluster was fully eliminated. Winners get `null` (always alive). Losers that survive all 100 conditions also get `null`.

---

## Slider 1 — Refinement Depth

**Range:** 0 (pre-refinement, 893 signals) to 100 (full post-refinement, 467 signals)

**UI behavior:** As slider moves right (0→100), losers are progressively eliminated. Winners never change. Stats bar updates: peak/day, avg/day, avg/week, avg/month, avg/year, total signals, win rate.

**How signals appear/disappear:** Each loser has `killed_at_depth`. If slider position ≥ `killed_at_depth`, signal is dead. Otherwise alive.

**SPY chart:** Dead signals fade out or disappear. Live signals show as circles — size = predicted MFE, color = predicted WR (green=high, red=low).

**Stats source:** `depth_stats` array, precomputed for every depth 0-100. Instant lookup, no client-side computation.

**Live scan uses:** The slider position = how many refinement conditions the nightly scan applies.

---

## Slider 2 — EV Quality Filter (Continuous Percentile Scoring)

**Range:** Continuous 0-100, mapped to the quality_score range in the output data.

**What it does:**

Every signal has a single `quality_score` (0-100) — a category-balanced weighted average of its percentile ranks across all surviving features. Market features (1,800+) collectively get 50% weight, setup features (3) get 50% weight. Within each category, features compete by individual screening strength.

The slider sets a minimum quality_score threshold. Slide right = demand higher quality = fewer signals survive.

**How quality_score is computed:**
1. For each surviving feature, compute every signal's percentile rank (0-100) within that feature's distribution using scipy.stats.rankdata
2. Flip descending features (lower raw value = better) so higher percentile always means better outcome
3. Category-balanced weighted average: market weights sum to 0.5, setup weights sum to 0.5
4. Result: single float 0-100 per signal

**Predicted WR and MFE** are computed separately via vectorized interpolation of each feature's decile WR/MFE curves at each signal's raw percentile position. Weighted average across features → predicted_wr and predicted_mfe. EV = (WR × MFE) - ((1-WR) × 1.0 ADR stop).

**Client-side computation:** Filter signals where `quality_score >= slider_value`. One number comparison per signal = sub-millisecond.

**DTSS calibration (2026-03-15):**
- Pre-refinement (893 signals): D1 actual WR = 19.1% → D10 = 64.1% (+45pp spread)
- Post-refinement (467 signals): D1 = 56.5% → D10 = 93.5% (+37pp spread)
- Post-refinement top 50%: 234 signals, 86.8% actual WR, ~39/year

**Live scan uses:** The slider position = minimum quality_score for tonight's watchlist.

---

## SPY Overlay Visualization

The UI renders all signals on a horizontally scrollable SPY daily chart.

Each signal is a circle positioned at its date on the SPY X-axis:
- **Circle size:** Predicted MFE (bigger = higher expected move)
- **Circle color:** Predicted WR (green gradient = higher WR, red gradient = lower WR)
- **Tooltip:** Ticker, date, classification, predicted WR, predicted MFE, EV, move_adr (actual)

Both sliders affect which circles are visible. As Slider 1 increases, loser circles disappear. As Slider 2 increases, circles below the quality_score threshold disappear.

**Stats bar (always visible):**
- Peak signals/day
- Average signals/day
- Average signals/week
- Average signals/month
- Average signals/year
- Total signals
- Win rate

Stats update in real-time as sliders move.

SPY OHLCV data is fetched separately (already in Railway DB or market cache). The EV grinder output just needs signal dates + scores.

---

## Processing Architecture

### Phase A — Setup Features (serial, fast, <30s)

Load 5yr OHLCV cache once. For each signal, compute 6 OHLCV features. Load fundamentals cache, compute 4 fundamentals features. Result: 10 features × 893 signals.

### Phase B — Market Feature Screening (parallel, ~5-15 min)

Split 256 instruments across workers (ProcessPoolExecutor).

Each worker:
1. Loads one .npz (~80MB)
2. Builds date→row index for O(1) lookups
3. For each of 15,805 expressions: looks up value at each signal's date, buckets into deciles, computes WR spread and MFE spread
4. Returns ONLY the survivors (feature name + values for all signals + screening stats)

**Both signal sets (pre and post refinement) are processed simultaneously per instrument.** Each worker screens features against both the 893-signal set and the 467-signal set in one pass. This halves disk I/O since each .npz is loaded once, not twice.

**RAM per worker:** ~80MB (one .npz) + signal lookup arrays (tiny). With 8 workers: ~640MB peak. Total with main process OHLCV cache (~2GB): ~3GB. Safe on 16GB+ desktop.

### Phase C — Combine + Dedup + Score (serial, fast, <1 min)

Take all survivors (market + setup + fundamentals), dedup by correlation, compute percentile ranks, category-balanced weighted scoring, validate.

### Phase D — Refinement Replay (serial, ~1 min)

Replay 100 refinement conditions against all 893 clusters. Build per-condition elimination map. Compute `killed_at_depth` per loser signal. Build `depth_stats` array.

### Estimated Total Runtime: ~10-20 minutes

---

## Edge Cases and Guardrails

- **NaN handling:** If >50% of signals have NaN for a feature, skip it. Signals with NaN get percentile 50 (neutral). Minimum 8 signals per decile or skip.
- **Date matching:** Market .npz dates are strings. Build dict for O(1) lookup. If signal date not in instrument's dates (holidays, different calendar), take most recent prior date.
- **100% example pass:** All 65 examples must get scored in both runs. Hard fail if any example is missing a score.
- **No mid-run aborts:** Log issues, skip bad features, keep going.
- **Silent failures:** Verify empirically. Spot-check feature values against preserved setup_grinder output. Spot-check regime features against preserved market_grinder output.
- **Windows multiprocessing:** All executable code inside `if __name__ == '__main__'` blocks. ProcessPoolExecutor without this guard causes recursive fork-bomb crashes.
- **Excluded tickers:** BRK-B, SMMT, VUZI not in 5yr cache. SERV, SOUN have <50 bars. Filter before grinder runs.

---

## Build Increments

### Increment 1: Script Skeleton + Data Loaders + Refinement Replay ✅

- Parse CLI args (`--setup dtss`)
- Find and load latest refinement JSON + raw clusters JSON
- Normalize both signal sets into common format: list of dicts with `ticker`, `date`, `bar_idx`, `close`, `classification`, `move_adr`, `is_example`, `cluster_id`
- Replay 100 refinement conditions against all 893 clusters using expression cache
- Build `killed_at_depth` per loser, `depth_stats` array, `refinement_depth_map`
- Print verification: depth 0 = 893, depth 100 = 467
- **Test:** Run it, verify counts match refinement output exactly

### Increment 2: Setup Feature Computation ✅

- Compute 6 OHLCV features for all 893 signals
- Compute 4 fundamentals features
- **Test:** Spot-check against preserved `setup_dtss_20260313_135931.json` (has all 893 signals with all 6 OHLCV features)

### Increment 3: Market Feature Screening (Parallel) ✅

- Worker function: loads one .npz, screens 15,805 features against both signal sets
- Orchestrator: runs workers across 256 instruments with ProcessPoolExecutor
- Returns survivors per instrument with values for all signals
- **Test:** Timing, survivor counts per instrument, RAM stays stable. Cross-check SPY features against regime model output.

### Increment 4: Union + Dedup ✅

- Combine market survivors + setup survivors
- Greedy dedup by correlation
- **Test:** Before/after counts, verify no >0.95 correlations remain

### Increment 5: Continuous Percentile Scoring + Signal EV ✅

- Percentile rank per signal per feature (scipy.stats.rankdata), direction-flipped
- Category-balanced weighting: 50% market / 50% setup
- Vectorized decile interpolation for predicted WR/MFE (np.searchsorted, no Python signal loop)
- quality_score (0-100), predicted_wr, predicted_mfe, ev per signal
- feature_percentiles stripped from output (40MB → 4.3MB)
- Calibration check: D1 vs D10 actual WR printed
- **Result:** Pre D1=19.1% → D10=64.1%. Post D1=56.5% → D10=93.5%. 65/65 examples scored.

### Increment 6: Validation + Full Output + Mirror ✅ (folded into Inc 5)

- Decile calibration built into score_signals (prints D1-D10 actual WR)
- Full JSON saved + mirrored to Railway
- Redundancy analysis: compare features_pre vs features_post in output

### Increment 7: UI — SPY Chart + Dual Sliders (separate task)

- Scrollable SPY chart with signal circles
- Slider 1: refinement depth (reads `depth_stats` + `killed_at_depth`)
- Slider 2: EV quality (reads `quality_score` per signal, continuous threshold)
- Stats bar always visible
- Slider positions saved to Railway as live scan parameters

---

## Live Nightly Scan Integration

After the EV grinder has run and the user sets slider positions:

1. **Nightly scan** runs tonight's bars against signal conditions (87) + refinement conditions (first N, per Slider 1 position)
2. For each signal that fires: compute feature values (market cache lookup + OHLCV + fundamentals)
3. Compute quality_score using stored percentile scoring equation
4. Apply EV quality filter (reject signals below quality_score threshold per Slider 2 position)
5. Compute EV score using stored feature weights + scoring curves
6. Rank by EV, highest to lowest
7. Top of list = what you trade tomorrow

Scoring is milliseconds per signal. All lookups from local caches.

---

## Key Design Decisions

- **Scoring, not filtering.** Phase 3 does not remove signals from the pipeline. It ranks them. The sliders let the user choose their own filtering threshold.
- **Additive model.** Each feature contributes independently. Well-supported by ~893 data points. Interaction terms ("UVXY matters more on high-priced stocks") not captured, but correlated features will both independently predict WR/MFE. Interactions can layer in later as examples grow.
- **Decile bucketing captures nonlinearity.** A feature that only matters at extremes is visible in the D10 vs D1 spread. Linear correlation would miss this.
- **Category-balanced weighting (50/50 market/setup).** Without balancing, 1,800+ market features drown out 3 setup features by headcount. Each category gets 50% of total weight. Within each category, features compete by screening strength.
- **Continuous percentile scoring, not discrete quartile levels.** quality_score is 0-100 per signal. Slider 2 sets a continuous threshold. No arbitrary bucketing of signals into 4 quality levels.
- **1.0 ADR assumed stop for EV calculation.** Losers don't have meaningful move_adr (setup broke). The loss side uses a fixed 1 ADR assumption. Adjustable parameter, not re-run required.
- **Pre AND post refinement.** Both runs tell different stories. Pre-refinement reveals genuine correlative features. Post-refinement reveals what's left after chart-level filtering. Features surviving both are the most valuable.
- **One output file, two sliders.** Everything the UI needs is in one JSON. No server round-trips for slider interactions. Client-side computation is trivially fast.
- **Setup-specific features are NOT from the expression cache.** The signal grind already mined all 16K expressions — anything in the cache that separates winners from losers would already be a signal/refinement condition. Setup features must come from outside the cache.
- **Whatever the sliders are set at = what the live nightly scan uses.** This is not a visualization-only tool. The slider positions define production parameters.
