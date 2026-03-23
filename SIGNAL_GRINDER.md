# Signal Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_pyramid()`
**Status:** Current state documented 2026-03-22. Proposed changes not yet implemented.

---

## WHY THIS EXISTS

The current pipeline runs the beam search once and uses all conditions it finds. With 68 examples and 15,805 expressions, a single beam search run finds ~87 conditions. Many of those conditions may be fitting noise — statistical coincidences in where the 68 examples happen to land across 15,805 dimensions.

Two problems need solving:

1. **Search instability:** The beam search is non-deterministic (greedy, order-dependent). Run it twice, get different conditions. Which run is right?
2. **Overfitting:** How many of those conditions describe the real DTSS pattern vs. noise in 68 data points?

Problem 1 is solved by multi-run consensus (Meinshausen & Bühlmann 2010 stability selection).
Problem 2 is solved by permutation testing — the standard method across genomics, neuroimaging, and ML for determining the noise floor of a search algorithm.

No EPV formula. No made-up ratios. No hard depth caps. The system generates its own answers from its own data.

---

## CURRENT STATE

**Last documented:** 2026-03-22 (from reading `pyramid_grinder.py`, 3,532 lines on v2)

### Entry point

`run_pyramid()` (line 1786). Called from `main()` (line 3422). CLI: `python local_runner/pyramid_grinder.py --setup dtss`

### Inputs

1. **Examples** — loaded from Railway API (`/api/examples/{setup_type}`), each with `ticker` and `entryDate`. OHLCV data comes from the 5yr cache, NOT Railway DB. Example loading: `load_example_data()` (line 101).
2. **5yr OHLCV cache** — `local_runner/cache/universe_ohlcv_5yr.pkl`. ~4,169 tickers. Loaded into memory via `load_5yr_cache()` (line 90).
3. **Expression cache** — per-ticker `.npz` files in `local_runner/cache/expr_series/`. 15,805 expressions. REQUIRED — no fallback computation path. Loaded via `ExprSeriesCache()`.
4. **Expression library** — `generate_all()` from `brute_expressions.py`. Returns list of expression dicts, each with `name`, `compute`, `category`.
5. **Universe matrix** — precomputed D1 (last-bar) matrix from `matrix_builder.get_universe_matrix()`. Used for pre-filter and D1 tier.

### Scan bar resolution

For each example: find the last bar BEFORE `entry_date` in the ticker's 5yr cache (line 136: `df[df["date"] < entry_dt]`, take last index). This is `scan_idx` — the signal bar. Examples where no bar exists before entry_date or ticker is not in 5yr cache are skipped.

After loading, examples are filtered to those present in the expression cache with matching bar counts (lines 1855–1874). Examples where `scan_idx >= cached_bar_count` are excluded.

### Expression cache usage

ALL grinders use the expression cache as the ONLY computation path. No `compute_series()` fallback exists. The cache provides `(dates, data)` per ticker where `data` is `(n_bars, n_expressions)` float32.

To read a value: `expr_cache.get_ticker(ticker)` → `(dates, data)`, then `data[scan_idx, col_idx]` where `col_idx` comes from `expr_cache._expr_name_to_idx`.

### Example ranges (bounding box)

`compute_example_ranges()` (line 161):
- For each expression, extract values at each example's `scan_idx` from the expression cache
- Build `example_matrix`: shape `(n_examples, n_expressions)`, float values
- For each expression where ALL examples have non-NaN values:
  - `low = min(values) - margin`, `high = max(values) + margin`
  - **Margin = 5%** of the range: `margin = (max - min) * 0.05`
- Expressions where ANY example returns NaN are excluded (cannot guarantee 100% pass)
- Output: `ranges` dict `{expr_name: (low, high)}`, `example_matrix` array

### Pre-filter (85% threshold)

`prefilter_candidates()` (line 232):
- For each expression in `ranges`, check what fraction of the D1 universe matrix falls within `[low, high]`
- NaN values count as passing (treated as "in range")
- If pass_rate ≥ 0.85 (85%), the expression is dropped — it's too broad to be a useful filter
- Expressions not in the D1 universe matrix (HTF etc.) are kept — they'll be evaluated at their proper tier
- Output: filtered `ranges` dict (subset of input)

### NaN handling asymmetry

NaN values are treated differently depending on context. This is consistent design logic — beam search treats NaN as "can't filter, don't penalize" while locked conditions and validation treat NaN as "can't verify, fail safe" — but it affects which conditions the search finds vs which survive validation.

| Context | NaN behavior | Location |
|---------|-------------|----------|
| Example range computation | Expression skipped if ANY example is NaN | pyramid_grinder.py:222 |
| Pre-filter (85% pass rate) | NaN = passes (counts as in-range) | pyramid_grinder.py:276 |
| PeakSpiderweb beam search | NaN = passes (counts as in-range) | pyramid_grinder.py:501 |
| Tier matrix locked-condition filter | NaN = **fails** (row excluded from matrix) | pyramid_grinder.py:384 |
| scan_all_signals (signal_filter.py) | NaN = **fails** (bar excluded from signals) | signal_filter.py:255 |
| validate_examples | NaN = **fails** (example fails condition) | pyramid_grinder.py:1570 |

A condition where some universe rows have NaN will appear more permissive in the beam search (NaN rows "pass") than in the downstream scan or validation (NaN rows fail). This means the beam search can select a condition that looks effective during search but performs slightly differently when applied as a locked condition in later tiers or during the full universe scan.

### Multi-pass logic (daily → weekly → monthly)

Default mode (`--multi-pass`, or no flag). Three passes defined in `MULTI_PASS_DEFS` (line 1604):

| Pass | Timeframe filter | Tiers |
|------|-----------------|-------|
| Pass 1 (Daily+LSP+Algo) | NOT `htf_weekly`, NOT `htf_monthly` | D1 → 1wk → 1mo → 6mo → 1yr → 5yr |
| Pass 2 (Weekly) | `htf_weekly` only | 1mo → 6mo → 1yr → 5yr |
| Pass 3 (Monthly) | `htf_monthly` only | 6mo → 1yr → 5yr |

Each pass:
1. Filters the full 15,805 expression list to its timeframe
2. Computes fresh example ranges for those expressions
3. Runs through its tier sequence
4. Conditions found in earlier passes are "locked" — they constrain the universe for later passes but cannot be replaced

Conditions accumulate across passes. Each pass's candidates exclude already-locked expression names.

### D1 tier logic

`run_d1_tier()` (line 1287):
- Uses `SpiderwebSearch` from `spiderweb.py` (separate module, NOT `PeakSpiderweb`)
- Operates on the precomputed D1 universe matrix (one row per ticker, last bar values)
- Scores by pass rate (what fraction of tickers pass all conditions)
- D1 depth capped at 15 (hardcoded at line 1807: `d1_depth = min(d1_depth, 15)`)
- Output: list of condition dicts with `{name, expr, category, compute, low, high, tier: "D1"}`

### Historical tier logic (1wk through 5yr)

`run_historical_tier()` (line 1388):
1. Identifies candidate expressions: those with valid ranges AND not already locked
2. Builds a parallel matrix across the full tradable universe:
   - For each ticker: load expression cache, apply all locked conditions as row filter, keep surviving bars in the tier's window
   - Window: last `n_bars` bars of the ticker's history (or all bars if `n_bars=0` for 5yr)
   - First 50 bars always skipped (warmup)
   - Blackout mask applied: post-entry bars for examples are excluded (prevents learning on in-play price action)
   - Whitelist mask: if set, only whitelisted bars count (used by refinement mode)
3. Stacks surviving rows into a matrix: `(n_surviving_bars, n_candidates)` with parallel row_dates and row_tickers
4. Runs `PeakSpiderweb` beam search on this matrix

Workers use `ProcessPoolExecutor` with `cpu_count() - 1` workers. Workers receive a "slim cache" (just bar counts per ticker) — NOT full DataFrames.

### Beam search mechanics (PeakSpiderweb)

`PeakSpiderweb` class (line 459):

**Score metric:** `max(daily_signal_counts)` — the peak number of signals on any single date. Lower is better. Goal: reduce peak/day below `peak_target` (default 15 for signal, 3 for refinement).

**Algorithm:**
1. Precompute `cand_passes[ci, :]` — boolean array per candidate, True where row's value is within `[low, high]` or NaN
2. "Valid candidates" = those that filter at least 1 row
3. **Seed level:** Score each valid candidate individually. Sort by peak score. Take top `beam_width * 2`, trim to `beam_width`
4. **Deepen** (level 2 through `depth`):
   - For each node in current beam: try adding each valid candidate not already in the node's condition set
   - Deduplicate by sorted condition tuple
   - Cap expansion at `beam_width * 8` per level — this cap breaks the **outer node loop**, not just the inner candidate loop (line 621: `if len(next_level) >= beam_width * 8: break`). Nodes later in the beam never get expanded. Because the number of children per node varies, which nodes get cut depends on the ordering of the current beam, making the cap an indirect source of non-determinism.
   - Sort by peak score, trim to `beam_width`
   - Update global best if improved
   - **Stop conditions:** (a) peak ≤ target, (b) zero signals, (c) peak didn't improve from previous level (ceiling)
5. No shuffling, no randomization, no subsampling — the search is deterministic given the same input matrix. Non-determinism comes from:
   - Input matrix varies with cache state (nightly appends)
   - `as_completed()` ordering in parallel tier matrix build (line 1464) produces non-deterministic row ordering in the stacked matrix — different row order means different tie-breaking in the beam
   - The `beam_width * 8` expansion cap (see above) amplifies row-order differences by cutting different nodes

**Leave-one-out filter power:** For each condition in the best path, compute `signals_without` (total signals if that one condition is removed). `filter_power = (signals_without - total) / total`.

### Condition output format

Each condition dict in the output JSON:
```json
{
  "name": "slope_xavgc21_off7_adr14",
  "expr": "slope_xavgc21_off7_adr14",
  "category": "daily_slope",
  "compute": {"op": "slope", "inner": {...}, "params": {...}},
  "low": -5.234,
  "high": 2.891,
  "tier": "1mo",
  "filter_power": 0.3412,
  "signals_with_all": 1218,
  "signals_without": 1634
}
```

`low` and `high` come from example ranges (with 5% margin). `tier` indicates which tier found the condition. `compute` is the full expression spec from `brute_expressions.py`.

### Validation (100% example pass)

`validate_examples()` (line 1536):
- After each tier adds conditions, validate ALL examples pass ALL conditions so far
- If validation fails: roll back that tier's conditions, continue to next tier
- After all tiers: final validation. If fails → result NOT saved, returns None
- Validation reads values from expression cache at each example's `scan_idx`
- Check: `val >= cond["low"] AND val <= cond["high"]` (NaN = fail)

### Save format + Railway mirror

**Filename pattern:** `pyramid_{setup}_{mp|sp}[_refinement]_sig{total}_pk{peak}_{YYYYMMDD_HHMMSS}.json`

Example: `pyramid_dtss_mp_sig1218_pk7_20260315_143022.json`

**Top-level JSON keys:**
```
setup_type, timestamp, total_time_s, peak_target, multi_pass, refinement,
n_conditions, all_conditions, tier_results, pass_summaries, params,
summary (final_total, final_peak, final_avg),
example_signals, examples_passing, examples_failing
```

`all_conditions` is the flat list of condition dicts (the primary output).
`tier_results` is a nested dict keyed by `{timeframe}_{tier}` (e.g. `daily_1mo`).
`pass_summaries` is a list of per-pass stats (multi-pass mode only).
`summary.final_total` / `final_peak` come from the last 5yr tier that ran.

**Railway:** Mirrored via `file_mirror.mirror_file()` and uploaded via `grind_uploader.upload()` with `step_type="signal_grind"`.

### --runs flag behavior

`--runs N` (line 3435, default 1):
- Loops `run_pyramid()` N times in `main()` (line 3465)
- Each run is independent — same inputs, same parameters
- Each run produces its own timestamped JSON (unique filename from timestamp)
- After all runs: prints a summary table comparing conditions count, total signals, peak/day, time
- `--runs` only applies to the signal grind path. NOT available for refinement (`--blackout` takes a separate code path at line 3445 and ignores `--runs`)
- Results list is local to `main()` — not saved as a combined file

### What makes the beam search non-deterministic between runs

The beam search itself is greedy and deterministic given the same matrix. Run-to-run variance comes from:
1. Matrix row ordering from `ProcessPoolExecutor` + `as_completed()` in tier matrix building — futures complete in non-deterministic order
2. The `beam_width * 8` expansion cap means ties at the cut boundary can differ
3. The `seen` set deduplication uses Python set ordering
4. If cache data changes between runs (nightly appends), the matrix changes

In practice: runs with the same cache state will show small differences in which conditions are found, how many, and in what combination — exactly the variance that consensus is designed to measure.

---

## FULL PIPELINE OVERVIEW

```
Step 1:  Signal grind × 15 real + 15 permuted (interleaved)   (~8-10 hours overnight)
Step 2:  Signal consensus engine                                (~seconds)
         → z-score gate: z > 3 required to proceed
         → If z < 3: STOP, vet more examples
Step 3:  Deterministic scan with locked signal conditions       (~15 minutes)
         → Full signal population with WIN/LOSS classification
Step 3.5: Re-grind exit condition on consensus signal population (~5 minutes)
Step 4:  Refinement grind × 10 runs                            (~5-15 minutes, see REFINEMENT_GRINDER.md)
Step 5:  Refinement consensus engine                            (see REFINEMENT_GRINDER.md)
Step 6:  EV grinder                                             (~minutes, no changes needed)
Step 7:  Profit grinder                                         (~minutes, no changes needed)
```

Steps 1-2 are one overnight. Steps 3-7 run automatically via the orchestrator, chained after Step 2 succeeds.

The vetting loop still works: winner pile from Step 3 → vet → new examples → re-run Steps 1-2 with more examples → permutation test allows more conditions → tighter system.

---

## PROPOSED CHANGES

### Step 1A: Real signal grind runs (×15)

Run `run_pyramid()` 15 times. Each run uses:

- Same 68 examples (same tickers, same entry_dates, same scan bars). Examples are never subsampled.
- Same expression cache, same beam search parameters (beam 10,000).
- **Random 50% subsample of the tradable universe** (different tickers per run, drawn from the ~4,167 tradable universe tickers from `build_tradable.py`). This is a core requirement of Meinshausen stability selection, not an optimization. Without subsampling, all runs produce near-identical results and consensus measures nothing. The 50% rate matches Meinshausen's original paper.
- **Randomized pass ordering** — the three passes (daily, weekly, monthly) run in a randomly shuffled order per run. Six possible orderings, each gets 2-3 runs across 15. A condition that only survives when its timeframe goes first appears in ~5/15 runs and fails consensus. A condition that survives regardless of ordering appears in 12+/15 and passes.
- **No peak target — run every tier to natural ceiling** (no improvement possible at the current beam level). Don't stop early. Each run finds ALL conditions that contribute any filter power. Consensus picks the stable ones afterward.
- **0% margin on bounding boxes during the grind.** The beam search works with exact example min/max. Tighter boxes produce sharper conditions. Consensus judges stability on the sharpest version of each condition. Margin is applied later at Step 2 Phase E only. **Performance note:** Tighter boxes → lower universe pass rates per expression → fewer expressions exceed the 85% pre-filter threshold → more candidates survive to beam search. More candidates means more nodes explored per level. Per-run timing may increase substantially compared to today's 5%-margin runs. The test runner (Step 1, 1+1 runs) reveals actual timing before committing to the overnight batch.
- D1 depth cap 15 still applies (prevents overfitting to a single day's snapshot).
- **D1 subsampling:** `run_d1_tier()` loads the full universe matrix from `get_universe_matrix()`, then filters its rows to only tickers present in `universe_cache`. Since the orchestrator subsamples `universe_cache` to 50% before calling `run_pyramid()`, D1 automatically sees the same ticker subset as every other tier. No new parameters, no new data paths — one filter step after the matrix load.

**Interleave with permuted runs:** Run order is real, permuted, real, permuted, real, permuted... This enables the early abort checkpoint.

Output: 15 JSON files, each containing a list of conditions with names, bounds, filter power. Same format as today's `pyramid_dtss_mp_*.json`.

**File naming convention:** Real runs use `pyramid_{setup}_mp_*.json` (unchanged). This prevents permuted files from contaminating any loading function that searches for `pyramid_*.json`.

**Universe subsampling consistency within a run:** The random 50% ticker selection happens once at the top of each `run_pyramid()` call and applies to ALL tiers and ALL passes within that run. The subsample is drawn before any tier runs. Every tier in that run sees the same universe subset. Implementation: draw the subsample, filter `universe_cache` to the selected tickers, pass the filtered cache to all downstream functions.

### Step 1B: Permuted signal grind runs (×15)

New `--permute` flag. When set:

- Generate 68 fake examples from the tradable universe (~4,167 tickers from `build_tradable.py`). For each of the 68 real examples, generate one fake replacement:
  1. Pick a random ticker from the tradable universe. NOT the full OHLCV cache — the same ticker list every grinder operates on.
  2. Verify ticker is in both the 5yr OHLCV cache and the expression cache. If not, repick.
  3. Pick a random bar index between 50 (warmup floor) and the ticker's cached bar count minus 1.
  4. Verify expression values at that bar are not all-NaN (same check `compute_example_ranges()` applies). If invalid, repick.
  5. Construct a fake `example_df` with `{ticker, entry_date: date_at_bar+1, scan_idx: bar_idx, df: ticker_ohlcv}`.
- Pass as `override_example_dfs` to `run_pyramid()` (parameter already exists).
- Everything else identical: same 50% subsampling rate, same randomized pass ordering, same beam 10,000, same exhaustion to ceiling.
- The beam search doesn't know the examples are fake. It finds conditions purely from statistical coincidence. This is the noise floor.
- Each permuted run generates a fresh set of 68 fake examples. Different permuted runs use different random fakes.

15 permuted runs for a tight null distribution.

Output: 15 JSON files, same format. **File naming convention:** Permuted runs use `permuted_{setup}_mp_*.json` — different prefix entirely so no loading function anywhere in the codebase accidentally grabs a permuted file as real conditions.

### Consensus run isolation

All consensus pipeline files (real runs, permuted runs, intermediate outputs) write to `local_runner/cache/consensus/`, NOT the standard `local_runner/cache/` directory.

- Real signal grind runs: `local_runner/cache/consensus/pyramid_{setup}_mp_*.json`
- Permuted runs: `local_runner/cache/consensus/permuted_{setup}_mp_*.json`
- Abort/gate reports: `local_runner/cache/consensus/consensus_abort_{setup}.json`, `consensus_gate_{setup}.json`

Only the final consensus output writes to the standard directory: `local_runner/cache/consensus_signal_{setup}.json`. And only when z ≥ 3.

If z < 3, nothing in the standard cache directory changes. The existing pipeline results, vetting workspace, signal population, winner pile — all untouched. The user can immediately open the vetting workspace and vet more examples using the existing data. No cleanup needed.

The consensus directory can be deleted or left alone — it doesn't affect anything.

### Test runner isolation

The test runner writes all files to `local_runner/cache/consensus/test/`. Completely isolated from both the real consensus runs and the standard pipeline. The test runner cleans this directory at the start of each test run — no stale files from previous tests.

### Early abort checkpoint (~2 hours in)

After 3 real + 3 permuted runs (interleaved, ~2 hours at overnight settings), compute preliminary z-score. This is a screening test, not a gate:

- If real runs average ~70 conditions and permuted average ~55 → z will be garbage at full count. Kill it. Go vet more examples.
- If real runs average ~70 and permuted average ~15 → safe to let the remaining 24 runs cook overnight.

Start at 4:30pm (after nightly refresh completes), checkpoint at ~6:30pm. Either abort and plan vetting, or walk away and check results in the morning.

### Step 2: Signal consensus engine

`scripts/consensus_engine.py` (full rewrite from current version).

**Phase A — Count condition frequencies (real runs):**
- Read all 15 real run JSONs from `local_runner/cache/consensus/` (files matching `pyramid_{setup}_mp_*.json`).
- Extract condition names from each.
- Count how many of 15 runs each condition appeared in.

**Phase B — Count condition frequencies (permuted runs):**
- Read all 15 permuted run JSONs from `local_runner/cache/consensus/` (files matching `permuted_{setup}_mp_*.json`).
- Same frequency counting.
- These conditions are 100% noise.

**Phase C — z-score computation:**

```
R = number of conditions that appeared in ≥ X/15 real runs (for some threshold X)
P_i = number of conditions that appeared in ≥ X/15 permuted runs (for each of 15 subsets)
mean_P = average of P_i across permuted bootstrap samples
std_P = standard deviation of P_i

z = (R - mean_P) / std_P
```

z-score edge cases:
- If std_P = 0 and R > mean_P: z = infinity. Proceed.
- If std_P = 0 and R = mean_P: z = 0. Stop.
- If std_P = 0 and R < mean_P: z = negative infinity. Stop.
- If fewer than 3 permuted runs completed: do not compute z. Report error. Null distribution unreliable.

**Phase D — Gate decision:**

| z-score | Meaning | Decision |
|---------|---------|----------|
| z > 3 | Real conditions far exceed noise floor. 99.7% confidence the pattern is real. | PROCEED to step 3 |
| z 2-3 | Signal above noise, but moderate confidence. | Judgment call — proceed with caution or vet more |
| z < 2 | Real conditions statistically indistinguishable from noise. | STOP. Vet more examples. |

z > 3 is a universal statistical convention (99.7% confidence), not a system-specific parameter.

**Phase E — Lock conditions:**

If z > 3, take the conditions from the real consensus that appeared above the frequency threshold. Recompute bounds from the full example set (consensus picks WHICH conditions, not bounds). **Apply 5% margin to the locked bounds.** This margin exists because 68 examples are a sample — the sample min/max underestimates the true range. 5% is conservative, which is correct because a missed real signal never enters the pipeline, while a false positive just gets a low EV score. The cost of missing signals is higher than the cost of extra signals.

Output: `consensus_signal_{setup}.json` written to the standard `local_runner/cache/` directory, with locked conditions + z-score + stability metrics. Condition format identical to what the rest of the pipeline expects.

### Step 3: Deterministic scan with locked conditions

New `--conditions-file` argument on `pyramid_grinder`. When provided:

- Skip the beam search entirely.
- **Override the internal `_load_signal_conditions()` call** inside `_gather_raw_signal_clusters()` with the externally supplied consensus conditions. The function's classification pipeline, clustering, ceiling+exit race all run unchanged — only the source of signal conditions changes.
- Scan the **full tradable universe** (100%, no subsampling — this is the real scan).
- Every (ticker, date) that passes all conditions = a signal.
- Group consecutive bars into clusters, classify each cluster as WIN or LOSS via the existing ceiling+exit race pipeline.
- **Forward window is recomputed** from example-to-cluster matching on the new signal population. Different consensus conditions produce different clusters, so the forward window may differ from previous runs. This is correct behavior.

The signal population from this scan is FIXED for all subsequent steps.

**Expect a larger signal population than today.** 20-25 consensus conditions vs 87 single-run conditions means fewer filters, more signals pass. Likely 2,000-3,000+ clusters instead of 893. This is expected and correct — the EV grinder handles ranking.

Output: `raw_signal_clusters_{setup}.json` in the standard cache directory, same format as today.

### Step 3.5: Re-grind exit condition on consensus signal population

The exit condition (`signal_exit_{setup}.json`) was previously ground against signal bars from a single-run pyramid result. The consensus signal population has different signal bars (different conditions, different clusters). The exit condition must be re-ground against the new population.

- Run `signal_exit_grinder.py --setup {setup}` after Step 3 completes.
- The exit grinder loads the consensus signal conditions (via the updated loading path — prefers `consensus_signal_{setup}.json` when it exists), resolves signal bars on the new population, finds the best exit expression.
- Output: updated `signal_exit_{setup}.json`, same format, overwriting the old one.
- **This must happen before Step 4.** The refinement grinder uses the exit condition for classification. A stale exit condition means misclassified signals, which means wrong winner/loser splits, which poisons everything downstream.

Runtime: ~5 minutes.

### Nightly refresh guard

The orchestrator checks for nightly refresh completion before starting. It reads `local_runner/cache/nightly_log.txt` for today's completion timestamp.

- If today's nightly completed: proceed.
- If nightly is still running or hasn't run today: do not start. Print a message and exit.

No retry loop, no waiting. Just a check. The nightly runs at 4:30pm and takes ~15 minutes. The user starts the orchestrator manually after confirming nightly is done — typically around 4:45-5:00pm.

### Vetting workflow after failed z-gate

When z < 3, the abort report (`local_runner/cache/consensus/consensus_abort_{setup}.json` or `consensus_gate_{setup}.json`) contains:
- z-score achieved
- Number of real conditions at consensus
- Number of permuted conditions at consensus (mean, std)
- Plain language recommendation: "z = 1.8. Real conditions not distinguishable from noise. Vet more examples and re-run."

The vetting workspace works unchanged. It reads from the standard cache directory which was not modified by the failed consensus run. The existing signal population, winner pile, charts — all still there. Open ScanPerfect, vet more charts, add examples, try again when ready.

No cleanup step. No rollback.

### Orchestrator run loop

The orchestrator (`scripts/run_consensus_pipeline.py`) handles the run loop directly. It calls `run_pyramid()` 30 times with per-call parameters:

- Calls 1-30: alternating real/permuted. Call 1 = real, call 2 = permuted, call 3 = real, etc.
- Each call receives: a unique random seed (for 50% universe subsampling), a randomly shuffled pass ordering, and the permute flag (True/False).
- The `--runs` flag on `pyramid_grinder.py` is not used by the orchestrator. It remains functional for manual testing.

The orchestrator manages interleaving, early abort checkpoint logic, z-gate decision, and chaining to downstream steps. These responsibilities cannot live inside `--runs` because `--runs` has no knowledge of permuted runs or z-scores.

### Per-run timing discovery

Running to natural ceiling with 50% universe and beam 10,000 has unknown per-run timing until the first run completes. The test runner (1 real + 1 permuted) reveals actual timing before the user commits to the overnight run.

The orchestrator prints estimated completion time after the first two runs finish, based on measured per-run duration × remaining runs. Example: "Run 1: 18 minutes. Estimated completion: 30 × 18 min = 9 hours. ETA: 2:15 AM."

### Overnight orchestrator

New script: `scripts/run_consensus_pipeline.py --setup dtss`

Chains the entire pipeline as one unattended overnight run:

1. Check nightly refresh completed. If not, exit with message.
2. Run Steps 1A + 1B interleaved (15 real + 15 permuted signal grinds) to `consensus/` directory.
3. Early abort checkpoint after 3+3 runs — practical heuristic, not a statistical test (n=3 per group gives enormous variance on the z estimate). If preliminary z < 1, kill the pipeline, write abort report. Stop. If z is ambiguous (1.0–1.5), err toward continuing — 12 more runs per group will sharpen the estimate.
4. If preliminary z looks viable, continue remaining 24 runs.
5. Run Step 2 (signal consensus engine). If z < 3, write gate report. Stop.
6. If z ≥ 3: write consensus output to standard cache directory. Run Step 3 (deterministic scan).
7. Run Step 3.5 (exit re-grind on consensus population).
8. Run Step 4 (refinement × 10) — see REFINEMENT_GRINDER.md.
9. Run Step 5 (refinement consensus) — see REFINEMENT_GRINDER.md.
10. Run Step 6 (EV grinder).
11. Run Step 7 (profit grinder).
12. Write summary report: `consensus_complete_{setup}.json` with z-score, condition count, signal population size, refinement results, EV grinder stats.

Start after nightly refresh (~4:45-5:00pm). Check at ~6:30pm for early abort. If still running, walk away. Morning: either an abort/gate report explaining why it stopped, or full results in Scan Tuning ready to review.

The orchestrator is a linear script with two conditional stops (early abort and z-gate). No retry logic, no parallelism, no complexity. If any step crashes, the script stops and the error is in the terminal output.

### Automated test runner

New script: `scripts/test_consensus_pipeline.py --setup dtss`

Runs a miniature version of the full pipeline (1 real + 1 permuted instead of 15+15) to verify every step produces correct output before committing to the overnight run. One command, no manual intervention. All files write to `local_runner/cache/consensus/test/`, cleaned at start of each test run.

**Test sequence:**

1. Run 1 real signal grind with 50% subsampling + randomized pass ordering → verify: output file exists, has `all_conditions` key, conditions list is non-empty, file matches `pyramid_{setup}_mp_*.json` pattern.
2. Run 1 permuted signal grind → verify: output file exists with `permuted_{setup}_mp_*.json` prefix, has conditions, example tickers differ from real examples.
3. Run consensus engine on those 2 files → verify: z-score computation ran without errors, output file has `all_conditions` and `z_score` fields. z-score won't be statistically meaningful from 1+1 but the math must execute cleanly.
4. Run deterministic scan with consensus conditions → verify: cluster file exists, has `clusters` array, each cluster has `classification` field.
5. Run exit re-grind → verify: exit file exists, timestamp is after Step 4 started, has `top_conditions` with at least one entry.
6. Run 1 refinement with `--skip-gather` → verify: output file exists, has `refinement_conditions_only` key.
7. Run refinement consensus on that 1 file → verify: both tests executed (consensus + binomial), output has `all_conditions`, `depth_progression`, `winner_signals`, `loser_signals`.
8. Run EV grinder → verify: output has `signals` array, each signal has `setup_score`, `market_score`, `killed_at_depth`.
9. Run profit grinder → verify: output has `stage_1` and `stage_2` keys.

**Behavior:** Green checkmark + one-line summary per passing step. Red X + error detail on first failure, full stop. No continuation past a failed step.

**Runtime:** ~45 minutes for 1+1 runs. Start it, walk away, come back to a pass/fail report.

### Branch strategy

Build on a feature branch off v2: `v2-consensus`. The existing pipeline on v2 remains fully functional and runnable throughout development.

**Build order on the branch:**

1. `pyramid_grinder.py` — add `--permute` flag, 50% subsampling, randomized pass ordering, 0% margin mode, no-peak-target exhaustion mode. Test: run 1 real + 1 permuted, verify output format.
2. `pyramid_grinder.py` — add `--conditions-file` flag for deterministic scan. Override internal `_load_signal_conditions()` with supplied conditions. Test: feed fake conditions, verify scan runs.
3. `pyramid_grinder.py` — add `--skip-gather` flag for refinement. Hard error if cluster file missing. Test: run refinement without prior scan, verify error message.
4. `scripts/consensus_engine.py` — full rewrite. Signal mode (z-score) + refinement mode (consensus + binomial). Test: feed 1+1 output, verify z-score math.
5. Signal conditions loading path — update `_load_signal_conditions()` to prefer `consensus_signal_{setup}.json`. Verify refinement and exit grinder find it.
6. `scripts/run_consensus_pipeline.py` — orchestrator with z-gate and chaining. Test: run miniature version end-to-end.
7. `scripts/test_consensus_pipeline.py` — automated test runner. Self-verifying.

Each increment is independently testable. No increment depends on a later one. A failure at any point doesn't break the existing v2 pipeline.

**Merge to v2:** After one full overnight run produces good results end-to-end on the branch, merge `v2-consensus` into `v2`. One codebase, not two permanent versions.

### Required wiring changes (signal grinder)

1. **Permuted file naming:** Permuted runs use `permuted_{setup}_*.json` prefix. All existing loading functions (`_load_signal_conditions()`, etc.) search for `pyramid_{setup}_*.json` and will never accidentally grab permuted files.

2. **Signal conditions loading path:** `_load_signal_conditions()` must check for `consensus_signal_{setup}.json` first. If it exists, use it. Fall back to latest `pyramid_{setup}_*.json` only if no consensus file exists. This affects: `_gather_raw_signal_clusters()`, `run_refinement()`, `signal_exit_grinder.py`, and any other consumer of signal conditions.

3. **Step 3 conditions injection:** `--conditions-file` argument passes the consensus JSON path. `_gather_raw_signal_clusters()` uses the supplied conditions instead of calling `_load_signal_conditions()` internally.

4. **Exit grinder re-run dependency:** Step 3.5 must complete before Step 4 starts. The orchestrator enforces ordering.

---

## WHY PERMUTATION TEST IS THE EPV

No EPV formula works for this system:
- PAC/VC theory gives K≤1 for 68 examples on 15,805 features (absurdly conservative, distribution-free worst-case)
- Peduzzi EPV=10 is for logistic regression, not conjunctive beam search
- Vittinghoff & McCulloch EPV=5 still doesn't apply to this architecture
- No published EPV guideline exists for "beam search conjunctive rule mining on 15,805 TA expressions with 100%-pass constraint"

The permutation test directly measures: "given MY search algorithm, MY feature space, and MY sample size, how many conditions does noise produce?" That measurement IS the EPV for this specific system.

With 68 examples: noise produces X conditions. Real produces Y. The gap (z-score) tells you if Y is real.
With 120 examples: noise floor drops (harder to find coincidences with more data constraining bounding boxes). Same real signal. Higher z-score. More conditions survive.

The noise floor naturally calibrates to example count, feature count, and search algorithm. No formula needed.

The example count does NOT gate the pipeline. You can run this with 68 examples tonight. The permutation test tells you what 68 examples can support. More examples → noise floor drops → more conditions survive → better system. But it's continuous improvement, not a binary prerequisite.

---

## WHAT NEEDS TO CHANGE IN pyramid_grinder.py

1. **New `--permute` flag:** When set, generates fake examples from the tradable universe before running. Takes the N (ticker, entry_date) pairs and randomly assigns them to different positions. Everything else stays the same.

2. **New `--subsample` parameter:** Fraction of tradable universe to include per run (default 0.5). Applied once at the top of each run, consistent across all tiers and passes.

3. **New `--randomize-passes` flag:** Shuffles the pass ordering (daily/weekly/monthly) per run.

4. **New `--no-peak-target` flag:** Disables peak target — every tier runs to natural ceiling.

5. **New `--zero-margin` flag:** Uses 0% margin on bounding boxes during the grind.

6. **`--runs N` already exists** (line 3435). Remains functional for manual testing but is NOT used by the orchestrator.

7. **New `--conditions-file` argument:** Accepts pre-supplied consensus conditions for deterministic scan (Step 3). Overrides `_load_signal_conditions()` in `_gather_raw_signal_clusters()`.

8. **New `--output-dir` argument:** Writes output to a specified directory instead of the default cache directory. Used by the orchestrator to direct consensus runs to `consensus/` subdirectory.

## WHAT NEEDS TO CHANGE IN consensus_engine.py

Full rewrite (current version has fake EPV cap logic):
- Read real + permuted run outputs from `consensus/` directory
- Count condition frequencies for both sets
- Compute z-score comparing real vs permuted
- Handle edge cases (std_P = 0, fewer than 3 permuted runs)
- Print z-score, gate decision, frequency distributions
- Output locked condition set with 5% margin if z > 3

---

## THEORETICAL GROUNDING

**Stability selection (Meinshausen & Bühlmann 2010):**
- Running a selection algorithm N times on subsampled data and keeping only features that appear consistently
- Proven to control false discoveries under general conditions
- Recommended N: 100 for lasso (fast), 10-20 for expensive searches
- 15 runs gives clear separation between stable (11+/15) and noise (4-/15)
- 15 permuted runs for tight null distribution
- Consensus threshold π ∈ (0.6, 0.9) proven to control family-wise error rate
- 50% subsampling is the standard rate from the original paper

**Permutation testing:**
- Standard across genomics (Tusher et al. 2001 SAM), neuroimaging, ML (scikit-learn permutation_test_score)
- Shuffles labels to destroy real signal, re-runs the analysis, measures what the algorithm finds from pure noise
- The null distribution is specific to the exact algorithm, feature space, and sample size — no assumptions needed
- z-score comparison to null distribution is the standard framework

**PAC learning / VC dimension (context for why formulas don't work):**
- For K conjunction conditions on d=15,805 features with N=68 examples: theoretical safe K ≤ 1
- These bounds are distribution-free worst-case, famously conservative
- Confirms that no formula can give a precise EPV for this system
- Motivates the empirical (permutation test) approach

---

## KEY DESIGN DECISIONS

1. **No EPV formula.** The permutation test IS the EPV. The only honest answer is empirical measurement.
2. **No hard depth cap.** The beam search runs to whatever depth it reaches. Capping depth with a formula would either be too conservative (throwing away real conditions) or too aggressive (keeping noise).
3. **z > 3 is the gate.** Universal statistical convention (99.7% confidence). Means "0.1% chance the real result came from noise."
4. **On-demand, not scheduled.** This runs when enough new examples have been vetted to justify the compute time. Not nightly.
5. **Number of runs:** 15 real + 15 permuted. From Meinshausen & Bühlmann (2010), with permuted count increased for tighter null.
6. **50% universe subsampling per run.** Core to stability selection. Provides the between-run variance that makes consensus meaningful.
7. **Randomized pass ordering per run.** Prevents any timeframe from having systematic priority. Consensus measures stability across orderings.
8. **0% margin during grind, 5% margin at scan time.** Sharpest possible conditions during discovery, breathing room during live use.
9. **Consensus runs isolated in subdirectory.** Failed runs cannot contaminate the existing pipeline or vetting workflow.
10. **Exit condition re-ground after consensus.** Step 3.5 ensures the exit condition matches the consensus signal population.

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. Does `--zero-margin` need to propagate to `compute_example_ranges()` as a parameter, or should it be a global mode flag?
2. ~~The subsampling draws 50% of tradable universe tickers — does the expression cache need to be pre-filtered too, or is it sufficient to filter `universe_cache` only?~~ **RESOLVED:** Filter `universe_cache` only. The expression cache doesn't need filtering — tickers not in `universe_cache` never get their `.npz` loaded. The tier matrix builder iterates `universe_cache.keys()`. D1 filters its matrix rows to match `universe_cache` after loading (see Step 1A).
3. The orchestrator calls `run_pyramid()` in-process vs spawning subprocesses — which is better for memory management across 30 runs?
4. The consensus threshold X in Phase C — sweep across multiple thresholds, or fixed at 0.7?
5. How to handle the case where the test runner passes but the full overnight run fails at step 8+ (EV grinder chokes on larger population)?
