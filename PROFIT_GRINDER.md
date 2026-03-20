# Profit Grinder — Phase 4: Exit Optimization

**Created:** 2026-03-19
**Status:** Inc 1-4 COMPLETE. Script fully rewritten.
**Script:** `scripts/profit_grinder.py`
**Pipeline step:** `profit_grind` (Step 7 in PIPELINE_V2.md)

---

## What It Does

The profit grinder finds the optimal TA-based exit conditions for maximizing trade profit across the winner signal set. It brute-forces the expression cache — same 15,805 expressions used by the signal grinder and exit grinder — testing every expression × threshold × direction against the forward price paths of all winner signals.

It answers: "Given the setup fired and you entered the trade, what TA expression condition tells you the move is over and it's time to take profit?"

This is distinct from the exit grinder (Phase 2b), which found one expression condition for **classification** — separating winners from losers. The profit grinder finds expression conditions for **profit-taking** — the optimal time to close the trade once you're in it. These may be completely different expressions.

---

## What It Replaces

The current `profit_grinder.py` on the v2 branch (as of 2026-03-19) is an ADR-based price-level simulator that brute-forces fixed stop/target/trail/trim levels. That is wrong — exits should be TA-expression-based, not arbitrary price levels. The script needs a full rewrite using the design below.

---

## Inputs

### 1. EV Grinder Output (scored signals)
- **File:** `local_runner/cache/ev_{setup}_*.json`
- **Used for:** The 893 pre-refinement signals with `classification`, `move_adr`, `entry_high`, `adr_at_signal`, `quality_score`, `killed_at_depth`, `is_example`

### 2. Entry Candle Scorer Output
- **File:** `local_runner/cache/entry_scores_{setup}.json`
- **Used for:** `entry_candle_score` per winner signal — the tradability weight

### 3. Vetting Decisions
- **Source:** Local SQLite `rejected_signals` table + examples table
- **Used for:** Vetted NO → exclude entirely. Vetted YES / examples → weight 1.0.

### 4. Expression Cache
- **Location:** `local_runner/cache/expr_series/*.npz`
- **Used for:** Forward expression values after each signal's entry bar. Same cache the exit grinder and pyramid grinder use.

### 5. 5yr OHLCV Cache
- **File:** `local_runner/cache/universe_ohlcv_5yr.pkl`
- **Used for:** Forward price bars (high/low/close) for move measurement

---

## Population

Signals where the exit condition fired (`move_adr` is not null). For DTSS: 364 winners + examples.

### Weighting

The weight determines how much each signal influences which expression condition wins and how the stats are computed. Higher weight = this signal's outcome matters more.

| Signal type | Weight |
|-------------|--------|
| Examples (validated by human) | 1.0 |
| Vetted YES (approved in vetting UI) | 1.0 |
| Vetted NO (rejected in vetting UI) | **Excluded entirely** — not in the population |
| Unvetted winners | `entry_candle_score` from entry candle scorer (raw cosine similarity, 0 to ~1) |

**Why `entry_candle_score` and not `combined_score`:** At trade time, you have the entry candle in front of you. You don't know how far the stock will move — that's the outcome. Weighting by `combined_score` (which includes `move_adr_pct`) uses future information that won't exist in live trading. `entry_candle_score` measures only "does this chart look like the kind of entry I'd actually take" — information you HAVE at trade time.

**Why not `combined_score` for another reason:** A winner that moved only 2 ADR but had a perfect entry candle is still a trade you'd enter and need a clean exit for. Penalizing it via the move_adr component in combined_score would cause the exit expression to ignore charts you'd actually trade, leading to scratched trades and a choppy equity curve in live practice.

### Trigger Requirement

The exit expression must fire on every chart you'd actually enter. There is no trigger rate floor or binning — the weighting system handles this naturally.

**Hard gate (binary, pass/fail):**
- Must trigger on 100% of examples + vetted YES signals. Any miss = candidate rejected.

**Unvetted winners — no gate, penalty via weighted scoring:**
- If the exit expression triggers: scored normally (captured move in ADR) at the signal's `entry_candle_score` weight.
- If the exit expression does NOT trigger: scored as a **1-ADR loss** at the signal's `entry_candle_score` weight.

**Why this works without a gate:** A candidate that misses high-`entry_candle_score` signals (charts that look like examples — you'd trade them) takes a heavy hit to weighted SQN/expectancy because those 1-ADR losses carry heavy weight. A candidate that misses low-`entry_candle_score` signals (charts that look nothing like examples — you'd skip them) barely feels it because the weight is negligible.

The scoring function IS the regulation. N signals = N resolution. Fully continuous, no bins, no hardcoded thresholds, self-referencing through the `entry_candle_score` weights. Different setups with different score distributions get different effective tolerances automatically.

**Improves with vetting:** More vetting → more signals at 1.0 weight (hard gate) → grind results increasingly reflect real tradable performance. The profit grinder is always runnable (even with zero vetting), but gets more accurate every time you vet.

### Stats Reporting (also weighted)

The stats panel (SQN, expectancy, equity curve, drawdown, etc.) is computed with the same `entry_candle_score` weights. The equity curve you see reflects the performance on signals you'd actually trade, not a mix of tradable and non-tradable.

---

## Core Engine

### Per signal:
1. Load ticker's `.npz` from expression cache
2. Find entry bar by date in cache dates
3. Slice forward window: entry bar through entry bar + max_forward
4. Result: a matrix of (n_forward_bars × n_expressions) — the expression values at every bar after entry

### Per candidate expression × threshold × direction:
1. For each signal, walk the forward expression series
2. Find the first bar where the expression crosses the threshold in the specified direction
3. Record: exit bar index, captured move (entry_high to exit bar close), bars held
4. Apply trigger rules: hard gate on examples/vetted YES, 1-ADR loss penalty for unvetted non-triggers

### Threshold generation:
- For each expression, gather all forward-window values across all signals
- Generate N thresholds from percentiles (e.g., 5th to 95th, N=50)
- Test each threshold in both directions (above and below)

### Expression filtering:
- Exclude boolean aggregations (ct_, st_, tir_ prefixes) — monotonically increasing, structurally wrong for exit detection
- Use `expr_col_map` to track actual cache column indices vs filtered indices

---

## Multi-Stage Trim

The grinder finds expression conditions for up to 3 exit stages:

### 1-stage (full exit):
- Find expression A that fires → sell 100% of position
- This is the core search — same as old profit_grinder

### 2-stage (trim + exit):
- Find expression A that fires first → trim X% of position
- Find expression B that fires after A → sell remaining position
- A and B can be different expressions with different thresholds
- Trim percentage is a parameter in the search grid

### 3-stage (trim + trim + exit):
- Expression A fires → trim X%
- Expression B fires after A → trim Y%
- Expression C fires after B → sell remainder
- All three can be different expressions

**Important:** Each stage's expression is evaluated only on bars AFTER the previous stage fired. Stage B doesn't start looking until Stage A has triggered. This prevents the grinder from finding conditions that fire simultaneously.

### Move measurement per stage:
- Stage A trim: captured move = entry_high to close at bar where A fired, on the trimmed portion
- Stage B trim: captured move = entry_high to close at bar where B fired, on B's portion
- Final exit: captured move on remaining position

Total trade outcome = weighted sum of all stage outcomes.

---

## Loss Assumption

1 ADR on the loss side for all stats calculations. This is not a real stop — it's the assumed loss for computing expectancy, SQN, and other stats. In practice, the loss is the entry candle high/low (handled by the trader, not the system).

Also used as the penalty for non-triggering unvetted winners: a signal where the exit expression never fires within the forward window is scored as a 1-ADR loss at its `entry_candle_score` weight.

---

## Stats Panel (per candidate exit condition)

Computed for every candidate that passes the hard gate (100% trigger on examples + vetted YES). All stats are **weighted** by `entry_candle_score`.

### Trade quality:
- SQN (sqrt(N_weighted) × weighted_mean / weighted_stdev)
- Expectancy (weighted mean captured move in ADR)
- Profit factor (weighted gross wins / weighted gross losses)
- Payoff ratio (weighted avg winner / weighted avg loser)
- Win rate (weighted — a high-weight win counts more)

### Per-trade stats:
- Avg winner move (ADR), median winner move, best winner
- Avg loser move (ADR), median loser, worst loser
- Avg bars held (winners), avg bars held (losers)
- Max consecutive winners, max consecutive losers

### Risk stats:
- Max drawdown (from weighted equity curve)
- Avg drawdown
- Max drawdown duration (trades)

### Growth stats:
- CAGR (from weighted equity curve, fixed fractional sizing)
- Sharpe (annualized weighted return / annualized weighted stdev)
- Sortino (return / weighted downside deviation)
- Calmar (CAGR / max drawdown)

### Equity simulation:
- Equity curve using fixed fractional sizing (1% risk per trade)
- Starting capital $100,000
- Trades ordered chronologically by signal date
- Each trade's P&L contribution scaled by its `entry_candle_score` weight

### Capture stats:
- MFE capture efficiency: what % of the available move (move_adr from refinement) does this exit capture
- Floor capture: worst-case capture across all signals
- Median capture

---

## Output

### File location:
- `local_runner/cache/profit_{setup}_{timestamp}.json` (archive)
- `local_runner/cache/profit_{setup}.json` (latest pointer)
- Mirrored to Railway as backup

### Output structure:

```
{
  "setup_type": "dtss",
  "timestamp": "...",
  "ev_source": "ev_dtss_inc6_*.json",
  "entry_scores_source": "entry_scores_dtss.json",
  "direction": "short",
  "n_signals": 364,
  "n_examples": 65,
  "n_vetted_yes": 0,
  "n_vetted_no_excluded": 0,
  "n_unvetted": 299,
  "n_expressions_tested": 12000,
  "loss_assumption_adr": 1.0,

  // Signal metadata for UI re-slicing
  "signals": [
    {
      "ticker": "AAOI",
      "signal_date": "2024-02-12",
      "quality_score": 53.24,
      "killed_at_depth": null,
      "move_adr": 5.865,
      "is_example": false,
      "is_vetted_yes": false,
      "weight": 0.72,
      "entry_candle_score": 0.72
    },
    ...
  ],

  // Results per trim mode
  "stage_1": {
    "mode": "1-stage",
    "n_candidates": 5000,
    "best_per_metric": {
      "sqn": {"expr_name": "...", "direction": "below", "threshold": 1.23, "value": 4.5},
      "expectancy": {...},
      "cagr": {...},
      ...
    },
    "top_candidates": [
      {
        "rank": 1,
        "expr_name": "slope_xavgc21_off7_adr14",
        "direction": "below",
        "threshold": -0.85,
        "stats": {
          "sqn": 4.5,
          "expectancy": 2.1,
          "win_rate": 0.82,
          "profit_factor": 3.4,
          ...all stats...
        },
        "trades": [
          {"ticker": "AAOI", "signal_date": "2024-02-12", "exit_bar": 12,
           "exit_date": "2024-02-28", "captured_adr": 5.2, "bars_held": 12,
           "mfe_capture_eff": 0.89, "weight": 0.72, "triggered": true},
          ...
        ],
        "equity_curve": [100000, 102100, ...]
      },
      ...top 100 candidates
    ],
    "grid": [
      // all candidates, stats only (no trades/equity)
      {"expr_name": "...", "direction": "...", "threshold": ..., "stats": {...}},
      ...
    ]
  },

  "stage_2": {
    "mode": "2-stage",
    "trim_pcts_tested": [0.25, 0.33, 0.50],
    ...same structure, but each candidate has two expressions...
  },

  "stage_3": {
    "mode": "3-stage",
    ...same structure, three expressions per candidate...
  }
}
```

---

## Parallelization

- Both stages use ProcessPoolExecutor (separate processes, real multi-core parallelism)
- 3D expression array (nv × exit_horizon × n_exprs) saved to temp .npy file
- Workers open via np.load(mmap_mode='r') — zero-copy memory-mapped access
- Small arrays (close_2d, weights, masks, final exit profiles) serialized to workers via pickle
- Default 12 workers (capped to leave headroom for OS)
- ThreadPoolExecutor does NOT work — GIL prevents parallelism for CPU-bound numpy loops
- No API calls, no network I/O — all local cache reads

---

## Runtime (actual, DTSS 2026-03-20)

- **1-stage:** 3.3 min (12 ProcessPoolExecutor workers, i5-12600k)
  - 12,878 expressions × ~100 thresholds × 2 dirs = 1.1M tested
  - Already-true-at-entry filter killed 92% before stats
  - 20,319 raw → 835 after dedup
- **2-stage:** 7.9 min (12 workers)
  - Top 300 1-stage expressions × 50 final exits × ~100 thresholds × 2 dirs × 3 trim%
  - 2.9M tested, 46,054 raw → 7,753 after dedup
  - Workers compute expectancy only — full stats on top 500 after
- **Total:** ~12 min including data loading
- **Critical:** Uses ProcessPoolExecutor (separate processes), NOT ThreadPoolExecutor (threads share GIL, no speedup for CPU-bound numpy work)
- **Memory:** ~2.25 GB for 3D expression array (364 × 120 × 12,878 float32), saved to temp .npy, workers open via np.load(mmap_mode='r')

---

## Multi-Stage Search Strategy

Running all expression × expression × expression combos for 3-stage would be computationally infeasible (12K³ = 1.7 trillion). Instead:

### Step 1: 1-stage grind
- Brute-force all expressions as single-stage full exits
- Keep top N candidates (e.g., top 200 by weighted median capture)

### Step 2: 2-stage grind
- For each of the top N exit expressions (stage 2 exit):
  - Test all expressions as stage 1 trim triggers (fires before stage 2)
  - For each trim % in [0.25, 0.33, 0.50]
  - Keep top M 2-stage combos

### Step 3: 3-stage grind
- For each top 2-stage combo:
  - Test all expressions as a middle trim trigger (fires after stage 1, before stage 3)
  - Keep top K 3-stage combos

This cascading approach keeps the search tractable while still exploring the full expression space at each stage.

---

## Build Increments

### Increment 1: Data Loading + Signal Population + Weighting + Forward Expression Matrices ✅

**What it does:**
- Load EV grinder output (latest `ev_{setup}_*.json`) — extract winner signals (move_adr not null)
- Load entry candle scorer output (`entry_scores_{setup}.json`) — extract `entry_candle_score` per signal, matched by ticker + date
- Load vetting decisions from local SQLite: examples table (weight 1.0), rejected_signals table (excluded)
- Build the weighted signal population: assign weight per signal per the weighting table (examples/vetted YES = 1.0, vetted NO = excluded, unvetted = entry_candle_score)
- Load expression cache manifest — get expression names, identify boolean aggregation prefixes (ct_, st_, tir_) to exclude
- Build `expr_col_map`: mapping from filtered expression index to actual cache column index (skipping excluded booleans)
- Load 5yr OHLCV cache — find entry bar per signal by date, extract forward close prices for move measurement
- Build forward expression matrices from expr cache: for each signal, load ticker .npz, find entry bar, slice forward window (entry bar through entry bar + max_forward). Result: per-signal matrix of (n_forward_bars × n_filtered_expressions)
- Free OHLCV cache after building forward close arrays
- Print population summary: total signals, examples, vetted YES, vetted NO excluded, unvetted, weight distribution stats, expression count (before/after boolean exclusion), forward matrix shapes

**What it does NOT do:** No grinding, no stats, no output file. Just data loading and validation.

**Test:** Run it. Verify signal count matches EV grinder output (364 winners for DTSS). Verify example count matches. Verify weight distribution looks sane (examples at 1.0, unvetted spread across 0–1). Verify expression count after filtering (should be ~12K, dropping ~3.6K boolean aggregations). Verify forward matrices have expected shapes. Verify no signals lost to missing cache data.

**What can be reused from current script:** `load_5yr_cache()`, `find_latest_ev_file()`, `load_ev_data()`, `select_signals()` (modified for weighting), CLI arg structure. Everything else is new.

---

### Increment 2: 1-Stage Expression Grind ✅

**What it does:**
- For each non-excluded expression column, gather all forward-window values across all signals
- Generate 50 thresholds from percentiles (5th to 95th) of the pooled forward values
- For each threshold × direction (above/below): walk each signal's forward expression series, find first bar where condition triggers
- Apply trigger rules: if any example or vetted YES signal doesn't trigger → reject candidate. For unvetted non-triggers → record as 1-ADR loss at their entry_candle_score weight
- For triggered signals: compute captured move (entry_high to exit bar close, in ADR), bars held
- Compute full weighted stats panel per candidate (SQN, expectancy, profit factor, win rate, equity curve, drawdown, CAGR, Sharpe, Sortino, Calmar, capture efficiency — all weighted by entry_candle_score)
- Parallelized via ProcessPoolExecutor: each worker gets a chunk of expression columns, processes all thresholds × directions for those columns across all signals
- No multi-stage yet — 1-stage full exit only (100% of position exits when condition fires)

**Test:** Run it. Verify candidate count is plausible (thousands of expressions × thresholds that pass the hard gate). Verify timing (~10-20 min estimated). Verify stats look sane (SQN, expectancy positive for top candidates, equity curves grow). Verify all examples triggered on every passing candidate. Spot-check a top candidate manually: does the expression/threshold/direction make TA sense for exits?

**What can be reused from current script:** Stats computation structure (SQN, equity curve, drawdown helpers) — but all need weighting added. Parallelization pattern (ProcessPoolExecutor with chunked work items). Nothing from the current simulate_trades or grid builders — those are all ADR price-level based and get replaced entirely.

---

### Increment 3: 2-Stage Trim Search ✅ (3-stage shelved)

**What it does:**
- Take top N candidates from 1-stage results (e.g., top 200 by weighted SQN or weighted median capture — TBD which metric to rank by for the cascade)
- **2-stage search:** For each of those top N as the final exit (stage 2): test all ~12K expressions as stage 1 trim triggers. Stage 1 expression evaluated only on bars BEFORE stage 2 fires. For each trim percentage in [0.25, 0.33, 0.50]: compute the blended trade outcome (trim portion captured at stage 1 price, remainder captured at stage 2 price). Same hard gate + weighted penalty rules. Same full stats panel per 2-stage combo. Keep top M 2-stage combos.
- **3-stage search:** For each top 2-stage combo: test all ~12K expressions as a middle trim trigger (fires after stage 1, before stage 3). Same rules. Keep top K 3-stage combos.
- Parallelized same as increment 2 — workers get chunks of expression columns for the new stage being searched

**Test:** Run 2-stage first, verify combos make sense (stage 1 fires before stage 2, trim percentages applied correctly, blended outcome math is right). Verify 2-stage SQN >= 1-stage SQN for top combos (trimming should improve consistency if the first target is well-placed). Run 3-stage, same checks. Verify timing is tractable (should be faster than 1-stage since the candidate set is already narrowed).

**What can be reused:** All of increment 2's grinding and stats infrastructure. The forward matrix data is already built. Only the per-signal exit logic changes (multi-stage walk instead of single-stage walk).

---

### Increment 4: Output Packaging + Save ✅

**What it does:**
- Package results per the output structure in the spec: signal metadata array, stage_1/stage_2/stage_3 result blocks
- For top 100 candidates per stage mode: re-run to extract per-trade detail (ticker, signal_date, exit_bar, exit_date, captured_adr, bars_held, mfe_capture_eff, weight, triggered). Build equity curve per candidate.
- For all candidates per stage mode: stats-only grid (no trades, no equity curve — too much data)
- Best-per-metric summary per stage mode (best SQN, best expectancy, best CAGR, etc.)
- Save timestamped JSON to `local_runner/cache/profit_{setup}_{timestamp}.json`
- Save latest pointer to `local_runner/cache/profit_{setup}.json`
- Mirror both to Railway via file_mirror
- Print summary report: top candidates per metric, overall timing

**Test:** Verify output JSON structure matches spec. Verify top 100 trade detail arrays have expected length (should equal signal count). Verify equity curves are monotonically computed (no NaN, no negative equity). Verify file sizes are reasonable (trade detail for 100 candidates × 364 signals could be large — check it doesn't blow up). Verify Railway mirror succeeds.

**What can be reused from current script:** `save_output()` pattern (timestamped + latest pointer + mirror), `np_fix()` JSON serializer, `package_mode_results()` structure (adapted for new stats), `print_mode_summary()` reporting pattern. The `build_trade_detail()` function pattern is reusable but internals change (expression-based exit instead of price-level exit).

---

## Key Design Decisions

- **TA-based exits only.** No fixed ADR price targets, no fixed stop losses. The chart determines the exit through expression conditions. The system measures what the chart does, not what an arbitrary price level says.
- **`entry_candle_score` weighting, not `combined_score`.** The weight reflects "would I enter this trade" — information available at trade time. Move size is the outcome, not the entry decision. Including move_adr in the weight would use future information and would cause the exit to ignore small-mover winners with great entries, leading to scratched trades in live practice.
- **No trigger gate on unvetted winners.** Instead of requiring a minimum trigger rate, non-triggering unvetted signals are scored as 1-ADR losses at their `entry_candle_score` weight. The weighted scoring function self-regulates: missing high-score signals is heavily penalized, missing low-score signals is negligible. No bins, no hardcoded thresholds, N signals = N resolution, fully continuous, self-referencing.
- **Hard gate on examples + vetted YES only.** These are confirmed tradable — the exit expression must work on every one of them. Binary pass/fail.
- **Stats also weighted.** The stats panel reflects performance on the signals you'd actually trade, not a mix of tradable and non-tradable.
- **Loss = 1 ADR for stats and for non-trigger penalty.** Not a real stop. The assumed loss side for computing expectancy and risk metrics, and the penalty applied to signals where the exit never fires.
- **Improves with vetting.** Zero vetting = usable but noisy. Heavy vetting = highly accurate. Every vetting session improves both the entry candle scorer (tighter centroid) and the profit grinder (more high-weight signals). Same flywheel as the rest of the system.
- **Cascading multi-stage search.** 1-stage narrows the candidate set, 2-stage builds on those winners, 3-stage builds on 2-stage winners. Keeps compute tractable.
- **Expression cache only.** No live computation. Same expression library, same cache, same as all other grinders.

---

## Scan Tuning Integration

The scan tuning UI reads the profit grinder output and lets you:
- Pick trim mode (1-stage, 2-stage, 3-stage)
- Pick which metric to rank candidates by (SQN, expectancy, CAGR, etc.)
- View the equity curve and trade detail for any candidate
- Lock in the chosen exit strategy for live nightly scanning

The profit grinder is a pure data engine. All decision-making happens in the UI.
