# Refinement Grinder — Specification

**Created:** 2026-03-22
**Script:** `local_runner/pyramid_grinder.py` → `run_refinement()`
**Status:** Current state documented 2026-03-22. Proposed changes not yet implemented.
**Prerequisite:** Signal grind z > 3 (see `SIGNAL_GRINDER.md`)

---

## CURRENT STATE

**Last documented:** 2026-03-22 (from reading `pyramid_grinder.py`, 3,532 lines on v2)

### Entry point

`run_refinement()` (line 3042). Called from `main()` when `--blackout` flag is set (line 3445). CLI: `python local_runner/pyramid_grinder.py --setup dtss --blackout`

Default parameters when called via `--blackout`: beam=10000 (overrides default 50), depth=100 (overrides default 10), peak_target=3 (overrides default 15). These are set in `main()` at lines 3447–3449.

### Inputs

1. **Signal conditions** — loaded from latest local `pyramid_{setup}_*.json` file via `_load_signal_conditions()` (line 2223). Searches `local_runner/cache/` and `data/`, picks newest by timestamp in filename. Excludes files with "blackout" or "refinement" in the name.
2. **Exit condition** — loaded from `data/signal_exit_grind/signal_exit_{setup}.json` via `_load_exit_cond()` (line 2261). Reads `top_conditions[0]` which has `expression`, `direction`, `threshold`.
3. **Examples** — loaded from local SQLite `data/scanperfect.db`, table `examples`, columns `ticker` and `entry_date`.
4. **5yr OHLCV cache** — same as signal grind.
5. **Expression cache** — same as signal grind.
6. **Expression library** — same as signal grind (`generate_all()`, 15,805 expressions).

### How it loads the signal population

`run_refinement()` is a 3-phase function:

**Phase 1 — Gather raw signal clusters:** `_gather_raw_signal_clusters()` (line 2278)
1. Load signal conditions + exit condition + examples
2. Build a slim cache (bar count per ticker) from 5yr OHLCV
3. Import `scan_all_signals` from `scripts/signal_filter.py` — this scans the full tradable universe with signal conditions using `ProcessPoolExecutor`, reading from expression cache
4. Group consecutive passing bars into clusters (line 2387): iterate sorted `(ticker, bar_idx)`, bars with consecutive indices in the same ticker form one cluster
5. Each cluster has a `rightmost` bar (last in sequence) and `leftward` bars (all others)
6. Match clusters to examples by date proximity (within `forward_window` bars before `entry_date`)
7. Classify each cluster as AUTO_WIN or AUTO_LOSS
8. Save to `raw_signal_clusters_{setup}.json`

**Phase 1 implementation notes:**

- **Slim cache bar minimum:** Phase 1 imports `_build_slim_cache` from `signal_filter.py`, which excludes tickers with `len(df) < 100` (signal_filter.py:280). The signal grinder's tier matrix builder uses a different slim cache with a lower floor of `n_bars < 50` (pyramid_grinder.py:349). In practice this doesn't matter — 5yr cache tickers all have 1000+ bars — but the thresholds differ.
- **Scan determinism:** `scan_all_signals()` (signal_filter.py:321) iterates futures in **submission order** (`for future in futures`), NOT `as_completed()`. Combined with batch assignment being deterministic (sequential slicing of the sorted ticker list), the Phase 1 scan produces identical results given identical inputs. This is more deterministic than the signal grinder's tier matrix builder, which uses `as_completed()` (pyramid_grinder.py:1464).

**Phase 2 — Load and split piles:** `_load_refinement_piles()` (line 2871)
- Reads the cluster file produced by Phase 1
- Splits into win_clusters (AUTO_WIN) and lose_clusters (AUTO_LOSS)
- Builds `win_example_dfs`: list of `{ticker, entry_date, scan_idx, df}` from winning cluster rightmost bars — these define the bounding box
- Builds `whitelist_map`: `{ticker: set(bar_idx)}` containing ALL bars from losing clusters + leftward bars from winning clusters — the expendable set
- Builds `losing_cluster_bars`: list of lists, each inner list = `[(ticker, bar_idx), ...]` for one losing cluster — used for cluster-aware scoring

**Phase 3 — Run cluster-aware beam search** (back in `run_refinement()`, line 3100+)

### Classification pipeline (how WIN/LOSS is determined)

Inside `_gather_raw_signal_clusters()`, lines 2555–2693. Direction is hardcoded as "short" for DTSS (line 2475).

**Step 1 — Forward window computation (Pass 1):**
- Match examples to clusters with tight distance (3 bars)
- Compute `forward_window` = max(leftmost signal bar to entry bar distance) × 1.1

**Step 2 — Ceiling + exit race (Pass 2):**
For each cluster:
1. Find highest high across all signal bars in the cluster (`cluster_high`)
2. Look forward from rightmost bar by `forward_window` bars, find max high in that window (`entry_window_high`)
3. `ceiling = max(cluster_high, entry_window_high)`
4. Starting from `rightmost_bar + forward_window + 1`, race two conditions:
   - **Close > ceiling** → `AUTO_LOSS` (classification_reason: `ceiling_breach`)
   - **Exit condition fires** (expression value crosses threshold in the specified direction) → `AUTO_WIN` (classification_reason: `exit_fired`)
5. If neither fires before end of data → `AUTO_WIN` (classification_reason: `held_to_end`)

**Step 3 — Example override:**
- Clusters matched to examples are ALWAYS classified as `AUTO_WIN` regardless of the race outcome (line 2688). The race still runs so they get `ceiling`, `exit_bar`, etc. for informational purposes.

**Step 4 — move_adr computation:**
- For clusters with `exit_bar`: measure `(entry_high - exit_close) / ADR` for shorts
- Examples use entry candle high; non-examples use forward window max high
- ADR at signal bar from expression cache (`adr14`), fallback to manual 14-bar computation

### Winner bounding box computation (exact min/max, no margin)

`compute_example_ranges()` is called on `win_dfs` (line 3103), which returns ranges WITH 5% margin. These are overwritten in the next block (lines 3106–3116): for each expression, recompute to exact `min(valid)` and `max(valid)` — **no margin** for refinement.

This is a critical difference from the signal grind (which uses 5% margin). The refinement bounding box is tight because the winner set is fixed and no new winners will be added.

### Loser cluster structure (multiple bars per cluster)

A cluster = group of consecutive signal bars in the same ticker. Built by sorting all raw signals by `(ticker, bar_idx)` and grouping adjacent bars (line 2390).

Each cluster in the JSON:
```json
{
  "cluster_id": 42,
  "ticker": "AAPL",
  "rightmost": {"bar_idx": 1205, "date": "2024-06-15", "close": 185.23},
  "leftward": [
    {"bar_idx": 1203, "date": "2024-06-13", "close": 184.50},
    {"bar_idx": 1204, "date": "2024-06-14", "close": 185.01}
  ],
  "size": 3,
  "classification": "AUTO_LOSS",
  "classification_reason": "ceiling_breach",
  "ceiling": 186.50,
  "is_example": 0,
  "move_adr": null,
  ...
}
```

### Cluster-aware beam search mechanics

`ClusterAwareRefinementSearch` class (line 991):

**Score metric:** Number of losing clusters with at least one surviving row. Lower is better. A cluster is only "eliminated" when ALL its bars fail at least one condition.

**Data structures:**
- `candidate_values`: `(n_expendable_rows, n_candidates)` float matrix — expression values at each expendable bar
- `row_cluster_ids`: int array, one per row. `>=0` = losing cluster index, `-1` = winning leftward bar (sacrificial, not in any losing cluster)
- `cluster_membership`: bool matrix `(n_rows, n_losing_clusters)` — precomputed for vectorized scoring
- `cand_passes`: bool matrix `(n_candidates, n_rows)` — precomputed pass/fail per candidate expression

**Scoring:** `_cluster_score(row_mask)` → count clusters where `any(cluster_membership[row_mask], axis=0)` is True. This is vectorized — checks for each cluster whether ANY of its member rows survive.

**Algorithm:** Same beam search structure as `PeakSpiderweb` — seed from individual candidates, deepen by adding one candidate per level, keep top `beam_width` paths, stop on ceiling/zero. Only the scoring function differs (cluster count instead of peak/day).

**What goes into the expendable matrix:**
- ALL bars from losing clusters (rightmost + leftward)
- Leftward bars from winning clusters (NOT rightmost — those define the bounding box and are must-pass)
- Each bar's expression values loaded directly from expression cache
- Rows where `(ticker, bar_idx)` maps to a losing cluster get `cluster_id >= 0`
- Winning leftward bars get `cluster_id = -1`

### How clusters get eliminated (ALL bars must fail)

The `_cluster_score()` method checks cluster survival via matrix multiplication. For cluster `i` to be eliminated, every row that belongs to cluster `i` must have `row_mask[row] = False` — meaning at least one locked condition excluded that row's value from the bounding box.

A single surviving bar in a cluster keeps the entire cluster alive. This is conservative by design — it prevents false eliminations from coincidental expression values on some but not all bars of a cluster.

### Winner leftward bars in loser matrix

Leftward bars from winning clusters are included in the expendable set (line 2968–2979 in `_load_refinement_piles()`). They get `cluster_id = -1` in the cluster array. This means:
- The beam search can use them for scoring context (they contribute to row counts)
- They are NOT part of any losing cluster, so eliminating them doesn't affect the cluster score
- They are "sacrificial" — conditions can exclude them without penalty

### NaN handling asymmetry

NaN values are treated differently depending on context within the refinement pipeline. This is consistent with the signal grinder's logic — beam search treats NaN as "can't filter, don't penalize" while locked conditions and validation treat NaN as "can't verify, fail safe."

| Context | NaN behavior | Location |
|---------|-------------|----------|
| Winner range computation | Expression skipped if ANY winner is NaN | pyramid_grinder.py:222 (via compute_example_ranges) |
| ClusterAwareRefinement beam search | NaN = passes (counts as in-range) | pyramid_grinder.py:1046 |
| Phase 1 scan (signal_filter.py) | NaN = **fails** (bar excluded from signals) | signal_filter.py:255 |
| Phase 3 validation (winners pass all) | NaN = **fails** (winner fails condition) | pyramid_grinder.py:1570 |

A condition where some loser bars have NaN will appear to eliminate fewer bars during the beam search (NaN bars "pass" and survive) than it actually would during a downstream re-scan (NaN bars would fail). This makes the beam search's elimination estimates conservative — the real exclusion rate is at least as high as what the search measures.

### Combined conditions (signal + refinement)

After the beam search finds refinement conditions (lines 3321–3354):
1. Load signal conditions from the latest pyramid result
2. Load exit condition
3. Merge: start with signal conditions, then append refinement conditions. If a condition name appears in both, the refinement version replaces the signal version (tighter bounds since no margin)
4. Validate all winners pass the combined set
5. If validation fails → fall back to refinement conditions only

### Exit condition handling

The exit condition is loaded but NOT included in the refinement beam search conditions. It's used only during Phase 1 classification (the ceiling+exit race). The refinement grind finds conditions that distinguish winners from losers GIVEN the exit condition's classification.

### Validation (all winners must pass combined set)

Two validations:
1. After beam search: validate all winners pass refinement conditions only (line 3281)
2. After combining signal + refinement: validate all winners pass combined set (line 3350)

If refinement-only fails → results NOT saved, returns None.
If combined fails → falls back to refinement conditions only (line 3352).

### Depth progression output

Built from `ClusterAwareRefinementSearch` beam search levels (lines 3226–3268). Each level records:
```json
{
  "depth": 3,
  "conditions": [
    {"name": "expr_name", "low": -1.5, "high": 2.3, "category": "daily_slope"}
  ],
  "losing_clusters_surviving": 312,
  "losing_clusters_eliminated": 216,
  "winners": 365,
  "total_signals": 677,
  "wr": 0.5391,
  "elapsed_s": 45.2
}
```

This powers the Settings Lock slider — choose refinement depth, see how many losers die and what WR results. Stored in `depth_progression` key of the output JSON.

### Save format + Railway mirror

**Filename pattern:** `refinement_{setup}_cl{surviving_clusters}_pk{peak}_{YYYYMMDD_HHMMSS}.json`

Example: `refinement_dtss_cl102_pk3_20260315_160522.json`

**Top-level JSON keys:**
```
setup_type, timestamp, total_time_s, refinement: true,
n_conditions, all_conditions (combined), refinement_conditions_only,
signal_conditions, exit_condition,
params (beam_width, depth, peak_target, source: "cluster_aware_refinement_grinder"),
summary (losing_clusters_input, losing_clusters_eliminated, losing_clusters_surviving,
         final_peak, final_avg, winners_input, winners_passing),
winner_signals, loser_signals (surviving), eliminated_signals,
depth_progression
```

Key distinction: `all_conditions` = combined signal + refinement set. `refinement_conditions_only` = just the conditions found by the refinement beam search. Downstream consumers (consensus engine) should use `refinement_conditions_only`.

**Intermediate file:** `raw_signal_clusters_{setup}.json` — saved by Phase 1, contains all clusters with classification. Also has a timestamped version.

**Railway:** Mirrored via `file_mirror.mirror_file()` and uploaded via `grind_uploader.upload()` with `step_type="refinement_grind"`.

### --runs flag behavior

`--runs` does NOT apply to `run_refinement()`. When `--blackout` is set (line 3445), `main()` calls `run_refinement()` directly and exits — the `--runs` loop at line 3465 is never reached. Each refinement run is a single execution.

---

## WHERE REFINEMENT FITS IN THE PIPELINE

```
Signal grind consensus (z > 3)                    ← see SIGNAL_GRINDER.md
  → Locked signal conditions (with 5% margin)
    → Deterministic scan of tradable universe      ← see SIGNAL_GRINDER.md Step 3
      → Signal population (WIN/LOSS classified)
        → Re-ground exit condition                 ← see SIGNAL_GRINDER.md Step 3.5
          → THIS: Refinement grind input
            → EV grinder
              → Profit grinder
```

The signal population from SIGNAL_GRINDER.md Step 3 is FIXED. Same winners, same loser clusters, same classification for every refinement run. The exit condition from Step 3.5 was ground against this specific population.

---

## WHY REFINEMENT CANNOT USE PERMUTATION TESTING

The refinement grinder has asymmetric data structures:

- **Winners:** Individual signals, each with one scan bar. They define the bounding box (min/max of each expression value across all winners).
- **Losers:** Organized into clusters of consecutive bars. A cluster is only "eliminated" when ALL its bars fail at least one condition.

You cannot cleanly shuffle WIN/LOSS labels because:
- A winner signal (single bar) cannot become a loser cluster (multiple bars)
- A loser cluster cannot become a winner (which bar defines the bounding box?)
- The data structures are fundamentally different shapes

A label permutation test does not apply mechanically to this architecture.

The signal grind z > 3 already validates that the pattern is real and the signal population is legitimate. The refinement grind operates on that validated population. The overfitting risk in refinement is the beam search finding coincidental conditions — multi-run consensus + per-condition significance testing handles this without needing permutation.

---

## PROPOSED CHANGES

### Step 4: Refinement grind × 10 runs

New `--skip-gather` flag so `run_refinement()` reads the fixed cluster file from Step 3 instead of re-scanning.

**Hard error if `--skip-gather` is set and `raw_signal_clusters_{setup}.json` does not exist.** Message: run Step 3 first. No silent fallback, no re-scanning.

Run the cluster-aware beam search 10 times with identical base inputs:

- Same winner pile (fixed from Step 3 deterministic scan).
- Same winner bounding box (computed from fixed winners, **exact min/max, 0% margin** — same as today's refinement grind).
- Same expression cache.
- Beam 10,000, depth 100, no peak target — run to ceiling.
- **Random 50% subsample of loser clusters per run** (enabled by `--subsample-losers` flag). This is required because the cluster-aware beam search is fully deterministic given identical input. Without subsampling, all 10 runs produce identical results and consensus is meaningless. Each run draws a different random 50% of loser clusters (controlled by `--seed`), keeping all winners (must-pass set is never subsampled).

**Loser subsampling matrix rebuild:** The cluster file from Step 3 is loaded once at the start of Step 4. For each of 10 refinement runs:

1. Draw a random 50% of loser clusters from the loaded file.
2. Rebuild the expendable matrix from the subsampled loser clusters + all winner leftward bars. Loser cluster bars not selected in the 50% draw are simply absent from the matrix. Winner leftward bars from winning clusters are always included (winners are never subsampled).
3. Rebuild the `(ticker, bar_idx) → cluster_id` mapping for the subsampled clusters.
4. Run the cluster-aware beam search on the rebuilt matrix.

Matrix construction happens inside the run loop, not before it. The matrix is small (~400 rows per run with 50% of loser bars), so rebuilding 10 times adds negligible time. The winner bounding box does not change — same winners, same exact min/max, computed once.

Output: 10 JSON files, each with `refinement_conditions_only`. Same format as today.

### Refinement file isolation

Individual refinement consensus runs write to `local_runner/cache/consensus/`:

- Individual refinement runs: `local_runner/cache/consensus/refinement_{setup}_cl*_pk*_*.json`

Only the final refinement consensus output writes to the standard directory: `local_runner/cache/refinement_{setup}_cl*_pk*_consensus_*.json`. This prevents consensus run files from contaminating the vetting workspace or existing pipeline.

If refinement consensus produces zero surviving conditions (skip refinement), the output still writes to the standard directory — it contains the unrefined signal population with empty `refinement_conditions_only` and `depth_progression` arrays. Downstream consumers handle this: EV grinder scores all signals, Scan Tuning depth slider shows 0 levels.

### Step 5: Refinement consensus engine — two-test validation

Every refinement condition must pass BOTH tests to survive.

**Test 1 — Consensus stability:**
- Count condition frequency across 10 runs.
- Apply consensus threshold from Meinshausen proven range (0.6-0.9).
- This is a convention within a mathematically proven range, not a self-derived number.
- Conditions below threshold = artifacts of specific beam search order, discard.

**Test 2 — Per-condition binomial significance (p < 0.01):**

This is the self-referential test. For each condition that passed Test 1:

1. The winner bounding box on expression X covers range [a, b].
2. Compute what fraction F of the entire tradable universe falls within [a, b] on any given day.
3. By pure geometry, about (1 - F) of ANY random set of signals would fall outside [a, b] — not just losers.
4. Measure the actual fraction of loser bars that fall outside [a, b]. **"Loser bars" = all bars (rightmost + leftward) from ALL losing clusters in the full cluster file.** Not winning leftward bars (those aren't losers). Not just rightmost bars (all bars in a cluster participate in elimination scoring). This gives maximum statistical power for the test.
5. Run a binomial test: is the loser exclusion rate significantly greater than the universe baseline (1 - F)?

```
Expected exclusion rate = (1 - F)    [from universe baseline]
Observed exclusion rate = fraction of loser bars outside [a, b]

Binomial test: is observed significantly > expected?
p < 0.01 → condition genuinely targets losers specifically
p ≥ 0.01 → condition is just geometric exclusion from a narrow bounding box, discard
```

**Plain language example:**
If the bounding box is so narrow that 92% of the whole market falls outside it, and 93% of losers fall outside — that 1% gap is nothing, probably coincidence. But if 92% of the market falls outside and 99% of losers fall outside — that 7% gap is real. Losers specifically have expression values outside the winner range on that expression more than random signals do.

The p < 0.01 threshold is a standard statistical significance level (1% false positive rate per condition).

### Binomial test universe baseline — data source

The per-condition binomial test computes what fraction F of the tradable universe falls within the winner bounding box [a, b] on expression X.

**Data source: full 5yr expression cache history.** Not the D1 snapshot. The signal population spans 5 years. The losers span 5 years. The baseline must span the same period.

For each surviving consensus condition (expression X, bounds [a, b]):

1. Load expression X's values across all tradable universe tickers, all bars (from expression cache).
2. Count total valid (non-NaN) values: N_total.
3. Count values within [a, b]: N_inside.
4. F = N_inside / N_total.
5. Expected exclusion rate = (1 - F).
6. Compare actual loser exclusion rate against (1 - F) via binomial test.

With ~4,167 tickers × ~1,260 bars × ~15 conditions = ~79M comparisons. Simple array operations on cached .npz files. Seconds on modern hardware.

The refinement consensus engine requires access to the expression cache (`ExprSeriesCache`), not just the JSON run outputs. This is the same cache every other grinder uses — no new dependency, just documenting that the consensus engine needs it.

### Why the binomial test is self-referential

The universe baseline exclusion rate comes from the data. The loser exclusion rate comes from the data. The p-value comes from standard binomial math. No external numbers, no formulas, no assumptions about sample size needed. The data generates its own significance threshold.

### There is no third test

Once conditions are locked from tests 1 and 2, applying them to the fixed loser pile is deterministic arithmetic. Fixed conditions + fixed loser pile = one number. There is no variance to measure, no spread to check. The output is what it is.

### Step 5 output — proceed or skip refinement

- **Any conditions survive both tests?** → Apply them. The loser elimination rate is whatever it is — 15%, 40%, 80%. Any amount backed by validated conditions is genuine improvement. No minimum elimination threshold.
- **Zero conditions survive?** → Skip refinement entirely. Proceed to EV grinder on the unrefined signal population (lower WR, more signals). Refinement is an improvement layer, not a prerequisite. The pipeline works without it.

### Depth progression with consensus conditions

The depth_progression is built by the refinement consensus engine, not by individual beam search runs. After Test 1 + Test 2 produce the validated condition set:

1. Order conditions by individual filter power: for each condition independently, count how many loser clusters it eliminates on its own against the FULL loser cluster pile (not subsampled — this is the final deterministic output). Sort descending — strongest condition first.
2. Progressively apply conditions in this order to the full loser cluster pile.
3. At each level, record: conditions applied so far, losing clusters surviving, losing clusters eliminated, winner count (unchanged), total signals, WR.
4. Store as `depth_progression` array in the output JSON.

The Scan Tuning slider reads this array. Fewer levels than today (one per validated condition vs one per beam search depth) but each level is backed by a condition that passed both stability consensus and binomial significance.

The Scan Tuning slider and EV grinder's `replay_refinement_depth()` both read the `depth_progression` array length dynamically — they adapt to any number of levels.

### Output format

Same as today's `refinement_dtss_cl*_pk*_*.json`:

```
all_conditions (combined signal + refinement)
refinement_conditions_only
signal_conditions
exit_condition
winner_signals, loser_signals (surviving), eliminated_signals
depth_progression
params, summary, timestamp
```

**Signal conditions source:** When the orchestrator runs refinement, it passes `--conditions-file consensus_signal_{setup}.json`. `run_refinement()` uses the supplied conditions instead of calling `_load_signal_conditions()` internally. This populates the `signal_conditions` field in the output JSON with the consensus conditions.

### Steps 6 + 7: EV grinder + Profit grinder

No changes to either. They read the same format and don't know consensus happened.

**Signal population will be larger than today.** The EV grinder's feature screening operates on more data points per quartile, which makes decile spread estimates more reliable. The screening thresholds (≥10pp WR spread, ≥1.0 ADR MFE spread) and dedup matrix size should be monitored on the first consensus run to verify they remain within 32GB RAM limits. If the surviving feature count grows significantly, tighten screening thresholds. This is a monitor-and-adjust item, not a pre-fix.

### Orchestrator integration

The refinement grind (Steps 4-5) runs as part of the overnight orchestrator (`scripts/run_consensus_pipeline.py`). It only executes if the signal consensus z ≥ 3. By the time refinement starts, the following are guaranteed to exist:

- `consensus_signal_{setup}.json` — locked signal conditions
- `raw_signal_clusters_{setup}.json` — fixed signal population from deterministic scan
- Updated `signal_exit_{setup}.json` — exit condition ground against consensus population

If any of these are missing, the orchestrator has already stopped before reaching refinement.

Steps 4-5 produce the refinement output that Steps 6-7 (EV + profit grinder) consume. The orchestrator chains directly: refinement consensus → EV grinder → profit grinder. No manual intervention between steps.

If refinement consensus produces zero surviving conditions, the orchestrator builds the output from the unrefined signal population and continues to EV grinder. The pipeline does not stop.

### Automated test runner integration

Steps 6-7 of the test runner (`scripts/test_consensus_pipeline.py`) cover refinement:

- Step 6: Run 1 refinement with `--skip-gather` → verify output format.
- Step 7: Run refinement consensus on that 1 file → verify both tests executed, output matches format EV grinder expects.

These run only after Steps 1-5 pass.

---

## WHAT THE SIGNAL GRIND z > 3 ALREADY PROVIDES

The signal grind permutation test validates:
- The setup pattern is real
- The signal population is trustworthy
- The WIN/LOSS classification within that population reflects real outcomes
- The refinement grind operates on validated data

What signal z > 3 does NOT validate:
- Whether winners and losers are distinguishable by expression conditions
- Whether the refinement beam search finds real structure or coincidence

That's what the two-test refinement validation covers. If signal z > 3 but refinement finds zero significant conditions, it means: the pattern exists and the scan finds genuine setups, but whether each one wins or loses isn't predictable from expression conditions. Winning might depend on things the 15,805 expressions don't capture — news, earnings timing, sector rotation, pure luck.

In that case: skip refinement, proceed to EV grinder on the unrefined population. The EV grinder scores on market features and setup features which might still have predictive power.

---

## WHAT NEEDS TO CHANGE IN pyramid_grinder.py

1. **`--skip-gather` flag for refinement:** When set, skip `_gather_raw_signal_clusters()` and jump to `_load_refinement_piles()`. Hard error if cluster file doesn't exist.
2. **`--runs N` for refinement:** Currently does not apply. The orchestrator calls `run_refinement()` as subprocesses with loser subsampling, so `--runs` is not needed — the orchestrator manages the loop.
3. **Loser cluster subsampling:** `run_refinement()` needs a `--subsample-losers` flag. When set, draws 50% of loser clusters using the seed from `--seed`. When NOT set, uses all losers (today's behavior). Manual `--blackout` runs omit this flag and get the full loser set. The orchestrator always passes `--subsample-losers`.
4. **New `--seed` parameter for refinement:** Integer seed for reproducible randomization. Only affects loser subsampling when `--subsample-losers` is also set. Without `--seed`, a random seed is generated internally if `--subsample-losers` is set. Same CLI arg as signal grinder — one `--seed` parameter serves both code paths.
5. **Output directory:** Same `--output-dir` flag as signal grinder, directing consensus refinement runs to `consensus/` subdirectory. Controls where refinement grind JSONs are written. `--skip-gather` always reads cluster file from CACHE_DIR regardless of `--output-dir`. Railway mirror/upload suppressed when `--output-dir` is set (consensus runs are intermediate files).
6. **`--conditions-file` for refinement:** When `--blackout --skip-gather --conditions-file` are all set, `run_refinement()` receives the signal conditions from the file instead of calling `_load_signal_conditions()`. Used in two places inside `run_refinement()`:
   - Line 3321: building the combined signal+refinement condition set (signal conditions come from file, not auto-discovery)
   - Output JSON: the `signal_conditions` field is populated from the supplied file
   Implementation: add `signal_conditions_override` parameter to `run_refinement()`. If not None, skip `_load_signal_conditions()` call and use the override. `main()` loads the JSON when `--conditions-file` is provided and passes the extracted conditions list.

## WHAT NEEDS TO CHANGE IN consensus_engine.py

`--stage refinement` mode. This is the most complex piece — the consensus engine must assemble a complete refinement output that the EV grinder and profit grinder can read unchanged.

**Inputs:**
- 10 refinement run JSONs from `consensus/` directory (each has `refinement_conditions_only`)
- `raw_signal_clusters_{setup}.json` — the fixed cluster file from Step 3 (full loser pile for final replay)
- `consensus_signal_{setup}.json` — locked signal conditions from Step 2 (for `signal_conditions` field)
- `signal_exit_{setup}.json` — exit condition from Step 3.5 (for `exit_condition` field)
- Expression cache — for universe baseline computation (Test 2)

**Processing:**
1. Extract `refinement_conditions_only` from each of 10 run JSONs
2. Count frequencies (Test 1 — consensus stability)
3. Load expression cache, compute universe baseline F per condition (Test 2 — binomial significance)
4. Surviving conditions = those passing BOTH tests
5. For each surviving condition, copy the full condition dict (`name`, `category`, `compute`, `low`, `high`, `filter_power`) from any of the 10 runs that contains it. Bounds are exact min/max (0% margin) — identical across all 10 runs since the winner set is fixed. **No margin is applied by the refinement consensus engine** (unlike the signal consensus engine which adds 5%). Refinement uses exact bounds because winners are the fixed signal population, not a sample.
6. Order surviving conditions by individual filter power against FULL loser pile (not subsampled)
6. Build `depth_progression`: progressively apply ordered conditions to full loser pile, record stats per level
7. Replay final condition set against full loser pile to determine which clusters are eliminated vs surviving
8. Build `winner_signals`, `loser_signals` (surviving), `eliminated_signals` from cluster file classifications + elimination results

**Output:** A single JSON written to standard cache directory as `refinement_{setup}_cl{surviving}_pk{peak}_consensus_{timestamp}.json`. Must contain ALL of these keys (EV grinder and profit grinder read them):

```
setup_type, timestamp, total_time_s, refinement: true,
n_conditions,
all_conditions         — combined signal + refinement (signal from consensus file + refinement from this engine)
refinement_conditions_only  — just the conditions that survived both tests
signal_conditions      — copied from consensus_signal_{setup}.json
exit_condition         — copied from signal_exit_{setup}.json
params                 — beam_width, depth, peak_target, source: "refinement_consensus"
summary                — losing_clusters_input, losing_clusters_eliminated, losing_clusters_surviving,
                         final_peak, final_avg, winners_input, winners_passing
winner_signals         — flat list from cluster file (all AUTO_WIN rightmost bars)
loser_signals          — flat list of surviving AUTO_LOSS rightmost bars (clusters not eliminated)
eliminated_signals     — flat list of eliminated AUTO_LOSS rightmost bars
depth_progression      — ordered condition application with per-level stats
```

**Per-signal dict format** (same for winner_signals, loser_signals, eliminated_signals — must match what `run_refinement()` produces at lines 3004-3033, which is what the EV grinder's `load_refinement()` expects):

```json
{
  "ticker": "AAPL",
  "signal_date": "2024-06-15",
  "bar_idx": 1205,
  "close": 185.23,
  "is_example": 0,
  "classification": "AUTO_WIN",
  "move_adr": 3.2,
  "adr_at_signal": 1.45,
  "entry_high": 186.50,
  "exit_bar": 12,
  "exit_date": "2024-07-02"
}
```

The consensus engine builds these by reading `raw_signal_clusters_{setup}.json` and extracting from each cluster's `rightmost` bar + top-level cluster fields. Field mapping from cluster to signal dict:

| Signal field | Cluster source |
|-------------|---------------|
| `ticker` | `cluster["ticker"]` |
| `signal_date` | `cluster["rightmost"]["date"]` |
| `bar_idx` | `cluster["rightmost"]["bar_idx"]` |
| `close` | `cluster["rightmost"]["close"]` |
| `is_example` | `cluster["is_example"]` (0 or 1) |
| `classification` | `cluster["classification"]` ("AUTO_WIN" or "AUTO_LOSS") |
| `move_adr` | `cluster["move_adr"]` (null if no exit) |
| `adr_at_signal` | `cluster["adr_at_signal"]` |
| `entry_high` | `cluster["entry_high"]` |
| `exit_bar` | `cluster["exit_bar"]` (null if no exit) |
| `exit_date` | `cluster["exit_date"]` (null if no exit) |

Which list a loser cluster lands in (loser_signals vs eliminated_signals) is determined by the consensus engine's replay of validated conditions against the full loser pile in processing step 7.

`all_conditions` merge logic: start with signal conditions, append refinement conditions. If a name appears in both, refinement version replaces signal version (tighter bounds). Same merge logic as `run_refinement()` lines 3339-3343.

If zero conditions survive both tests: output still writes with empty `refinement_conditions_only`, empty `depth_progression`, all losers in `loser_signals`, none in `eliminated_signals`. Downstream consumers handle this gracefully.

---

## THEORETICAL GROUNDING

**Stability selection (Meinshausen & Bühlmann 2010):**
- Consensus threshold π ∈ (0.6, 0.9) proven to control family-wise error rate
- 10 runs is minimum viable for consensus measurement
- 50% subsampling of loser clusters provides between-run variance
- The exact threshold within 0.6-0.9 is a convention; the binomial test (Test 2) does the heavy lifting

**Binomial significance test:**
- Standard statistical test for "is the observed rate different from the expected rate?"
- Used here to test each refinement condition: does it exclude losers more than the universe baseline?
- p < 0.01 is a standard significance threshold (1% false positive rate per condition)
- No assumptions about sample size, feature count, or search algorithm needed
- Universe baseline computed from full 5yr expression cache history — same timespan as the signal population

---

## KEY DESIGN DECISIONS

1. **Refinement can't use permutation testing** because winner bounding box + loser cluster data structures are asymmetric — can't cleanly shuffle labels.
2. **The p < 0.01 binomial test is self-referential.** Universe baseline from data, loser exclusion from data, p-value from standard math. No external numbers.
3. **Refinement is optional.** If zero conditions pass both tests, skip it. Pipeline works without it.
4. **No minimum loser elimination threshold.** Any elimination backed by validated conditions is genuine. Whether it's 15% or 80% is an output, not a gate.
5. **Consensus threshold within 0.6-0.9** is a convention, not self-derived. Test 2 does the real work; Test 1 is secondary stability filter.
6. **Loser cluster subsampling per run.** Required because the beam search is deterministic given identical input. 50% of loser clusters per run, all winners always included.
7. **depth_progression ordered by individual filter power.** Canonical ordering computed by consensus engine, not inherited from any single beam search run.
8. **Consensus runs isolated in subdirectory.** Failed runs cannot contaminate the existing pipeline or vetting workspace.
9. **Universe baseline from full 5yr history.** Not D1 snapshot. Matches the timespan of the data being tested.
10. **Exit condition re-ground before refinement.** Step 3.5 ensures classification uses an exit condition matched to the consensus signal population.

---

## OPEN QUESTIONS FOR IMPLEMENTATION

1. ~~The loser subsampling seed — passed as a parameter to `run_refinement()`, or generated internally per run?~~ **RESOLVED:** `--seed` passed as CLI arg, but subsampling only happens when `--subsample-losers` is also set. Manual runs omit `--subsample-losers` and use all losers (today's behavior). The orchestrator always passes both flags.
2. Consensus threshold: exact value within 0.6-0.9 range — start with 0.7 and see?
3. For the binomial test universe baseline: load all expression cache data into memory at once, or stream per-ticker? Memory implications with ~4,167 tickers × ~1,260 bars.
4. If the signal population is much larger (2,500+ clusters instead of 893), the refinement matrix is also larger. Does beam 10,000 still finish in seconds, or does it need adjustment?
5. The `--skip-gather` flag skips Phase 1, but Phase 2 (`_load_refinement_piles()`) still loads the 5yr OHLCV cache for building `win_example_dfs`. Is that necessary, or can it read everything it needs from the cluster JSON + expression cache?
