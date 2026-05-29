# SIGNAL_GRINDER.md

Authoritative spec for the signal grinder and multi-run consensus pipeline built on top of it.

**Scripts:** `local_runner/pyramid_grinder.py`, `scripts/run_consensus_pipeline.py`, `scripts/consensus_engine.py`

Active status tracking lives in `CONSENSUS.md`. This file describes the system's intent and behavior; the code is the source of truth for implementation details (no line numbers in this spec — they always go stale).

---

## Why the consensus layer exists

The beam search is non-deterministic and the search space is enormous: 15,805 expressions explored against ~68–120 examples. A single run finds ~87 conditions, many of which may be statistical coincidences — noise in where the examples happen to land across the expression space.

Two problems need solving:

1. **Search instability.** The beam search is greedy and order-dependent. Two runs with the same inputs find different conditions. Which run is right?
2. **Overfitting.** How many of the conditions describe the real setup vs. fit 68 data points by chance?

Problem 1 is solved by **stability selection** (Meinshausen & Bühlmann 2010): run the search N times with 50% universe subsampling and keep only conditions that appear consistently.

Problem 2 is solved by **permutation testing**: run the same search N times against fake examples drawn randomly from the tradable universe. The condition count from the permuted runs is the noise floor. A z-score comparison between real and permuted runs tells you whether the real conditions are statistically distinguishable from what the search would return against pure noise. `z > 3` is required to proceed.

No EPV formula, no made-up ratios, no hard depth caps. The system generates its own significance threshold from its own data.

---

## Single-grind behavior (`run_pyramid`)

The single-grind pipeline is the bootstrapping path. It is used both as a standalone tool (produce a winner pile for vetting while the example set is small) and as the core search called by the consensus orchestrator.

### Inputs

1. **Examples** — loaded from SQLite `examples` table (ticker + `entry_date` pairs). OHLCV for each example comes from the 5yr OHLCV pickle, not Railway.
2. **5yr OHLCV cache** — `local_runner/cache/universe_ohlcv_daily.pkl`. Full daily history since `HISTORY_START` for the full universe.
3. **Expression cache** — per-ticker `.npz` files in `local_runner/cache/expr_series/`, 15,805 expressions per bar, stored as float16 and cast to float32 on load. **Required** — no fallback computation path exists. If the cache is missing or stale, grinding fails.
4. **Expression library** — `generate_all()` from `brute_expressions.py`. Returns dicts with `name`, `compute`, `category`.
5. **Universe matrix** — precomputed D1 (last-bar) matrix from `matrix_builder.get_universe_matrix()`. Used for the D1 tier and as the domain for the 85% pre-filter.

### Example preparation

For each example, `run_pyramid` finds the last OHLCV bar strictly before `entry_date` — this is the "signal bar". Examples with no bar before entry, or tickers not in the 5yr cache, are dropped.

**Cross-source alignment uses dates, never bar indices.** For each example, the signal-bar date is resolved in the ticker's expression-cache `dates` array to produce `cache_scan_idx` — the example's position in *cache-relative* coordinates. This is essential because the OHLCV cache and the expression cache have different start dates (the expression cache is shorter; it skips early bars for indicator warm-up). Using OHLCV bar indices to index the expression cache silently produces wrong values. This bug was the root cause of historical tiers being no-ops before 2026-04-11 and is a class of error that must never be reintroduced — every cross-source lookup goes through dates.

Examples whose signal date isn't in the expression cache are dropped with a warning before grinding starts.

### Per-bar tradable filter

Before any tier runs, `compute_tradable_masks()` builds one boolean mask per ticker, aligned to expression-cache coordinates. A bar is tradable if the ticker met **all three** criteria at that bar:

- Close ≥ $1.00
- 20-day average dollar volume ≥ $4M
- 20-bar ADRP ≥ 1.8% (TC2000-style: `(mean(H/L) − 1) * 100`)

The filter is **per-bar, not per-ticker**. A ticker that is a penny stock today but was tradable in 2022 still contributes its 2022 bars to the historical search. A ticker that is tradable today but wasn't in 2020 contributes only its recent bars. This matches historical scanning behavior and matters for condition derivation: using a ticker's entire history just because it's tradable today would inject illiquid-historical-period signals into the search.

The mask is passed to tier workers and AND-ed into `pass_mask` during tier matrix building. Tickers with zero tradable bars are skipped before their `.npz` is loaded — a meaningful I/O win because the full OHLCV universe contains thousands of bars from tickers that never qualify.

### Example ranges (bounding box)

For each expression, `compute_example_ranges()` extracts the value at each example's `cache_scan_idx` from the expression cache. An expression's bounding box is:

```
low  = min(values) - margin
high = max(values) + margin
margin = (max - min) * 0.05   # 5% default
```

Expressions where **any** example has NaN at its scan bar are dropped from the candidate pool — that expression cannot be guaranteed to pass for that example, so it cannot become a locked condition without breaking the 100% example pass rule. (See NaN asymmetry below.)

The consensus pipeline overrides the 5% margin to 0 for grinding (`--zero-margin`). Consensus applies a 5% margin to the final locked conditions at the end of stability selection instead — the margin lives at the output, not at the search.

### Pre-filter (85% threshold)

`prefilter_candidates()` drops expressions whose bounding box passes too much of the D1 universe matrix. If ≥ 85% of tickers pass, the expression is too broad to filter anything usefully and is excluded from the candidate pool. NaN values count as passing. Expressions not in the D1 universe matrix (HTF, LSP, algo) bypass the pre-filter and are kept for evaluation at their proper tier.

Under `--subsample`, the pre-filter loads the full universe matrix independently — it always uses the complete universe, never the subsampled one. This is intentional and slightly conservative: an expression that passes 85% of the full universe is still dropped even if it might pass less of the 50% subsample. Cheaper to be conservative here than to let through high-pass-rate expressions that would waste beam search time.

### Multi-pass logic

The default grinder runs three sequential passes over different expression timeframes, locking conditions from each pass as constraints on the next. The pass definitions:

| Pass | Expression filter | Tiers |
|------|-------------------|-------|
| Pass 1 (Daily + LSP + Algo) | NOT `htf_weekly`, NOT `htf_monthly` | D1 → 1wk → 1mo → 6mo → 1yr → 5yr |
| Pass 2 (Weekly HTF) | `htf_weekly` only | 1mo → 6mo → 1yr → 5yr |
| Pass 3 (Monthly HTF) | `htf_monthly` only | 6mo → 1yr → 5yr |

Each pass:
1. Filters the full expression list to its timeframe.
2. Computes fresh example ranges over its filtered expressions.
3. Runs through its tier sequence in order.
4. Conditions found in earlier passes are locked — they constrain the universe for later passes but cannot be replaced.

Conditions accumulate across passes. Each pass's candidate pool excludes expression names already locked.

Single-pass mode (`--single-pass`) runs all 15,805 expressions in one pipeline through the full tier sequence. It exists for comparison and legacy compatibility.

### D1 tier

`run_d1_tier()` operates on the precomputed D1 universe matrix (one row per ticker, last-bar values). Scoring is by pass rate (fraction of universe that survives all conditions). Uses `SpiderwebSearch` from `spiderweb.py` (not `PeakSpiderweb`). D1 depth is capped at 15 regardless of the user's `--depth` setting — more than 15 conditions on a single day's snapshot overfits to one bar.

D1 respects universe subsampling: after loading the full universe matrix, rows are filtered to only the tickers present in `universe_cache` (which is subsampled upstream when `--subsample` is set).

### Historical tiers (1wk, 1mo, 6mo, 1yr, 5yr)

`run_historical_tier()` builds a parallel matrix of surviving ticker-day rows across the tier's window, then runs `PeakSpiderweb` over it.

Matrix build is parallelized via `ProcessPoolExecutor(max_workers=cpu_count() - 1)`. **A fresh worker pool is spawned per tier.** Workers have no persistent state across tiers — each ticker's `.npz` is loaded and decompressed afresh for every tier that needs it. This is a known inefficiency and a target for optimization (see `CONSENSUS.md` candidate optimizations), but the optimization work is deferred and must preserve full-data search semantics when applied.

Workers receive a "slim cache" (bar counts per ticker), the full locked-conditions list, the candidate expression indices for the tier, the window size, and the pre-computed tradable masks. For each assigned ticker the worker:

1. Loads the ticker's expression cache via `ExprSeriesCache.get_ticker()`.
2. Builds a `pass_mask` in cache-relative coordinates: starts with the tier's time window (`[max(50, cache_n_bars - n_bars_window), cache_n_bars)`), applies the tradable mask, then applies every locked condition from prior tiers/passes. NaN values fail the mask (NaN is not "unknown passes" here — it's "can't verify, exclude").
3. For every surviving bar, extracts the values for the tier's candidate expressions.
4. Returns `(row_dates, candidate_values)` to the parent.

The parent vstacks all worker output into a `(n_surviving_rows, n_candidates)` float32 matrix and hands it to `PeakSpiderweb`.

Tickers with zero tradable bars, fewer than 50 cache bars, or no surviving rows after locked conditions return `(ticker, [], None)` and contribute nothing.

### Beam search (`PeakSpiderweb`)

**Score metric:** `max(daily_signal_counts)` — the peak number of signals on any single date across the input matrix. Lower is better. The search is minimizing peak/day subject to the locked-in example pass rate.

**Algorithm:**

1. Precompute `cand_passes[ci, :]` — a boolean array per candidate, true where the row's value is within `[low, high]` or is NaN.
2. "Valid candidates" are those that filter at least one row (the expression has some effect).
3. **Seed level:** score each valid candidate individually by peak. Take the best `beam_width * 2`, trim to `beam_width`.
4. **Deepen** (level 2 through `depth`):
   - For each beam node, for each valid candidate not already in that node's condition set, compute the new row mask, score by peak, and append to `next_level`.
   - Deduplicate by sorted condition tuple.
   - Cap expansion at `beam_width * 8` per level (the cap breaks the *outer* node loop, so later nodes in the beam may not get expanded — the cap is a known source of indirect non-determinism).
   - Sort next level by peak, trim to `beam_width`.
   - Stop conditions: (a) peak ≤ target, (b) zero signals remain, (c) peak didn't improve from previous level.

**Leave-one-out filter power:** for each condition in the final best path, compute the total signal count with that one condition removed. `filter_power = (signals_without - signals_with_all) / signals_with_all`. Pure numpy, instant. Lets downstream tools prune weak conditions from the JSON output without a full rescan.

### NaN handling asymmetry

NaN values are treated differently depending on context. The rule is "search treats NaN as unknown-passing; locked conditions and validation treat NaN as unknown-failing":

| Context | NaN behavior |
|---------|--------------|
| Example range computation | Expression dropped if **any** example is NaN (can't guarantee pass) |
| Pre-filter 85% pass rate | NaN = passes (counts as in-range) |
| PeakSpiderweb seed/deepen | NaN = passes (counts as in-range) |
| Tier matrix locked-condition filter | NaN = **fails** (row excluded from matrix) |
| Universe scan (`signal_filter.py`) | NaN = **fails** (bar excluded from signals) |
| `validate_examples` | NaN = **fails** (example fails condition) |

Consequence: a condition that the beam search thinks is effective may behave slightly differently when applied as a locked condition in a later tier or during the full universe scan. The search can find a condition that looks clean during deepening but is more restrictive when re-applied downstream. This is a known trade and is not a bug — it's what allows the search to make progress through bars with sparse expression coverage.

### Condition output schema

Each condition dict in the grinder's JSON output:

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

`low` / `high` come from example ranges with the applicable margin. `tier` indicates which tier found the condition. `compute` is the full expression spec from `brute_expressions.py` — downstream consumers can re-evaluate the condition without re-deriving the spec.

### Final validation

`validate_examples()` verifies 100% of examples pass all conditions at their scan bar after each tier and at the end of the grind. Validation reads from the expression cache using `cache_scan_idx`. Check: `val >= low AND val <= high` (NaN fails). If a tier's conditions break validation, those tier conditions are rolled back and the grinder continues to the next tier. If the final set breaks validation, the result is **not saved** — the whole grind is discarded.

Under `--permute` (permutation-test mode with fake examples), final validation is skipped because fake examples aren't meant to pass.

### Output file naming

```
pyramid_{setup}_{mode}[_refinement]_sig{total}_pk{peak}_{YYYYMMDD_HHMMSS}.json
```

- `mode`: `mp` (multi-pass, default) or `sp` (single-pass, `--single-pass`)
- `_refinement`: present only when the run is a refinement grind (`--blackout`)
- `total` / `peak`: summary of the final signal population
- Permuted runs use the prefix `permuted_*` instead of `pyramid_*` to prevent any loader from accidentally grabbing them as real conditions.

Consensus pipeline runs write to `local_runner/cache/consensus/` (via `--output-dir`), which also suppresses the Railway mirror and upload. Standard single-grind runs write to `local_runner/cache/` and are mirrored + uploaded.

### `--runs` flag

Runs the grinder N times in the same process with the same inputs. Each run is independent, produces its own timestamped JSON, and is printed in a summary table at the end. Used for quick comparison of within-run variance. Not used by the consensus pipeline (the orchestrator spawns separate subprocesses per grind).

### Non-determinism sources

The beam search is greedy and deterministic given the same input matrix. Between runs on the "same" data, variance comes from:

1. `ProcessPoolExecutor` + `as_completed()` — worker futures complete in non-deterministic order, so the row order in the stacked tier matrix differs between runs. Different row order → different tie-breaking in the beam.
2. The `beam_width * 8` expansion cap — ties at the cut boundary can be resolved differently across runs depending on the `next_level` order.
3. `seen` set deduplication uses Python set iteration, which is insertion-ordered but not shuffle-stable under different input orders.
4. Cache state drift between runs (e.g. overnight appends between two grinds).

Consensus is designed to turn this variance into a signal-vs-noise test: 15 real grinds produce slightly different results; the conditions that appear in most runs are the stable ones.

---

## Multi-run consensus pipeline

Built and wired through Increment 10. Orchestrated by `scripts/run_consensus_pipeline.py`. Mini-scale test runner is `scripts/test_consensus_pipeline.py`.

### Pipeline shape

```
Step 1  : 15 real + 15 permuted signal grinds, interleaved         (~8 hr at current speed)
Step 2  : Signal consensus engine — count, bootstrap z, gate       (seconds)
Step 3  : Deterministic full scan with locked signal conditions    (~15 min)
Step 3.5: Re-grind exit condition on consensus signal population   (~5 min)
Step 4  : Refinement grind × 10 runs                               (~5–15 min)
Step 5  : Refinement consensus engine                              (seconds)
```

EV grinder and profit grinder are **not** part of this pipeline. They are deferred to the future "live EV ranked watchlist" build.

### Consensus-specific grinder flags

All flags are additive; omitting them runs the classic single-grind path unchanged.

- `--seed N` — RNG seed for subsampling, pass ordering, and fake-example generation. Required for reproducibility across real/permuted pairs.
- `--subsample 0.5` — filter the universe to a random 50% after cache load. All tiers see the same subset within a run.
- `--permute` — replace the real example set with randomly-drawn tickers+bars from the tradable universe. Beam search is unaware. Output is written as `permuted_*.json`. Final validation is skipped.
- `--pass-order 2,1,3` — reorder the multi-pass definitions. When not specified but a seed is given, the orchestrator shuffles pass order per run.
- `--zero-margin` — overwrite example ranges with exact min/max (5% margin is applied only at consensus output time).
- `--no-peak-target` — set peak_target to 0, disabling early termination. Beam search runs to the natural ceiling (no improvement possible at the current beam level).
- `--scan-only --conditions-file PATH` — load locked conditions from the file, skip the beam search entirely, run the raw-signal-clusters gathering step only. Used in Step 3.
- `--skip-gather --subsample-losers --conditions-file PATH` — refinement-grind path. Loads signal conditions from file instead of searching them.
- `--output-dir PATH` — write grind output to a specified directory and suppress the Railway mirror+upload.

### Step 2: Signal consensus engine

`scripts/consensus_engine.py --stage signal`. Four phases:

- **Phase A — Count real.** Read all 15 real-run JSONs matching `pyramid_{setup}_mp_*.json` from the consensus dir. Count how many runs each condition appeared in.
- **Phase B — Count permuted.** Same for `permuted_{setup}_mp_*.json`.
- **Phase C — Bootstrap z-score.** `R = number of unique conditions appearing in ≥ X fraction of real runs`. Bootstrap `P_i` from the permuted runs (draw 15 with replacement, count conditions ≥ threshold) × 1000 iterations. `z = (R - mean(P_boot)) / std(P_boot)`. Edge cases for `std_P = 0` handled explicitly.
- **Phase D — Gate.** `z > 3` → proceed. `2 < z ≤ 3` → judgment call (proceed with caution or vet more). `z ≤ 2` → stop, vet more examples.
- **Phase E — Lock conditions.** Take conditions above the frequency threshold. Apply 5% margin to the locked bounds arithmetically from any real run's JSON (all 15 runs computed ranges on the same examples with zero margin, so values are identical across runs).

Output: `local_runner/cache/consensus_signal_{setup}.json`. Written only if `z > 3`. If gate fails, standard cache dir is untouched.

### Early abort checkpoint

After 3 real + 3 permuted runs (~2 hours in at overnight settings), the orchestrator computes a rough heuristic: `(mean_real_conds - mean_perm_conds) / mean_perm_conds`. If < 0.5, abort — the full run would fail the z-gate. This is not the consensus engine; the bootstrap needs more than 3 samples. It's a practical kill switch so you don't burn overnight hours on a doomed run.

### Theoretical grounding

- Meinshausen & Bühlmann (2010) *Stability Selection*, JRSS-B. The subsample-and-aggregate principle that makes the real grinds robust.
- Permutation testing (Efron, Good). Standard across genomics, neuroimaging, and ML for determining a search algorithm's noise floor — the distribution of results when applied to label-shuffled data is the null hypothesis, and real-data results are measured against it.
- `z > 3` is a universal statistical convention (≈ 99.7% confidence), not a project-specific parameter. Nothing to tune.

Why these work for this system specifically: the beam search doesn't care whether examples are real or fake — it finds combinations of conditions that look effective against whatever 68-point target it's given. Fake examples produce a distribution of "how many conditions does the search find from pure noise?" Real examples produce a different distribution. The distance between them, normalized by the variance of the noise, is exactly the z-score. No curve-fitting, no EPV formula, no manual threshold tuning — the permutation test is the EPV.

---

## Key design decisions

- **100% example pass rate, no exceptions.** Every locked condition must be satisfied by every example at its scan bar. A grind that fails final validation is discarded, not saved with a warning.
- **5% margin on locked bounds, 0% during consensus grinding.** The sharper bounding box makes the search more selective during stability testing; the looser margin at the output handles sampling error in the small example set.
- **Date-based lookups for all cross-source alignment.** OHLCV and expression cache have different start dates — bar indices are never carried across sources. Every example carries a `cache_scan_idx` resolved from its signal date.
- **Tradable filter is per-bar, not per-ticker.** Historical bars from currently-illiquid tickers are kept if they were tradable at that time; present-day tradable tickers only contribute bars from dates where they qualified.
- **Consensus pipeline runs isolated from the standard cache directory.** All grind JSONs, permuted runs, and intermediate outputs write to `local_runner/cache/consensus/` (or `.../consensus/test/` for the test runner). Only the final consensus output reaches the standard cache. If the z-gate fails, nothing in the live pipeline is touched.
- **Single-run bootstrapping path must never regress.** `pyramid_grinder.py --setup X` with no consensus flags is how new setups go from ~20 examples to ~100+ before consensus is useful. All consensus flags are additive.

---

## Pending research — overfit-protected pyramid on §6-prefiltered universe

The pyramid + stability-selection consensus pipeline (15 real + 15 permuted, z>3 gate) was designed to handle pyramid's beam-search non-determinism. It has not been measured on BF. PRESIGNAL_GRINDER.md §6 stack admits ~14.5% of universe and is forward-stable but too loose to deploy alone. Open question: does §6-then-pyramid-stability-selection produce a forward-stable, deployable condition set — §6 picks structurally BF-shaped candidate bars, pyramid + stability selection picks the precise condition set those candidates must satisfy.

### Approach

1. §6 stack wild scan over the universe produces a per-ticker, cache-aligned bool mask marking signal bars (E−1) where the stack passes. Bars at labelled example signal bars are force-included in the mask to preserve pyramid's 100% example-pass invariant.
2. From the worktree orchestrator, monkeypatch `pyramid_grinder.compute_tradable_masks` to AND the §6 mask into its per-ticker output (only narrowing, never relaxing). No `local_runner/` edits.
3. Run the existing Increment 10 stability-selection pipeline against the pre-filtered universe: 15 real grinds (subsample=0.5, all 45 examples, seeds 0..14, zero_margin=True) + 15 permuted grinds (same subsample but permute=True, seeds 100..114). Each grind operates on a candidate pool ~6.9× smaller than normal.
4. Reuse `scripts/consensus_engine.py` (read-only import) for z-score / gating. z > 3 required to lock conditions; 5% margin applied at output by consensus_engine.

### Open questions

- With the candidate universe pre-restricted to BF-shaped bars, do pyramid's standard params (peak_target=3, beam=50, depth=10) still find non-trivial conditions, or does the pre-filter compress the search to where few additional axes carve usefully?
- Permuted runs draw fake examples from the full universe (per consensus pipeline default) but search the §6-filtered candidate pool. Real-vs-permuted z-gate measures significance at that filtered scope, which matches the deployment use case.

### Validation

Walk-forward on the chronological 30/15 split: build §6 + run stability selection on training-30 only, test held-out 15 against the locked condition set.

### Rejected approach: pyramid example-subsample consensus (2026-04-28)

10 pyramid runs each on a different random 80% subsample of training examples; conditions intersected by name. Failed: only 2 of ~40 conditions per run survived 10/10 strict intersection (the 2 broadest in pyramid's repertoire). Walk-forward 14/15 coverage was misleading — the 2-cond union-band filter admitted ~26% of the wild universe (lift only ~3.6× over random). Root cause: the 10 runs varied two independent variance sources at once — example-subsample variance (intended) AND pyramid beam-search non-determinism. Stability selection was always the right tool for the latter. Run-level data in worktree `.claude/worktrees/presignal-quality-research/research/` (bf_pyramid_consensus_*).
