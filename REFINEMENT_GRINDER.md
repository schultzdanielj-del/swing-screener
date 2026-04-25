# Refinement Grinder — Specification

**Script:** `local_runner/pyramid_grinder.py` → `run_refinement()`
**Prerequisite in the consensus pipeline:** Signal consensus engine has produced locked signal conditions with `z > 3` (see `SIGNAL_GRINDER.md`). In bootstrapping mode (pre-consensus), refinement runs against whatever signal conditions the single-run signal grind produced.

This file describes behavior and design. The code is the source of truth for implementation details — no line numbers in this spec.

---

## Current behavior

### Entry point

`run_refinement()`, invoked via `python local_runner/pyramid_grinder.py --setup <type> --blackout` (plus optional consensus flags: `--skip-gather`, `--subsample-losers`, `--seed`, `--conditions-file`, `--output-dir`).

Default parameters when called via `--blackout`: `beam_width=500` (was 10000 pre-Session-5 — reverted because the matmul vectorization that made beam=10000 viable was reverted in February; see `CONSENSUS.md` notes). `depth=100`. `peak_target=3`. The matrix-building worker code uses cache-relative coordinates throughout (Session 5 bar-count fix applied in all places — `_load_example_row`, `_build_tier_batch`, `validate_examples`, and the refinement winner filter).

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

Inside `_gather_raw_signal_clusters()`. Direction comes from the `setups` table. Setup class (fade vs breakout) is hardcoded: `FADE_SETUPS = {"dtss", "3-4db"}`; everything else is breakout. See `SIGNAL_FILTER.md` for the full spec.

**Step 1 — Forward window computation (Pass 1):**
- Match examples to clusters with tight distance (3 bars)
- Compute `forward_window` = max(leftmost signal bar to entry bar distance) × 1.1

**Step 2 — Classification (branches by setup_class):**

*Fade path (DTSS, 3-4DB):*
1. Ceiling = max high of cluster bars + forward window bars (for shorts; min low for longs)
2. Starting from `rightmost_bar + forward_window + 1`, race:
   - Close breaches ceiling → `AUTO_LOSS` reason `ceiling_breach`
   - Exit condition fires → `AUTO_WIN` reason `exit_fired`
   - Neither before end of data → `AUTO_WIN` reason `held_to_end`
3. Tie goes to stop (ceiling breach wins over exit on same bar).

*Breakout path (BRKO):*
1. Derive ADR thresholds from examples: `winner_threshold = max(entry_offset) + 1.1` ADR, `loser_threshold = max(FW_MAE) × 1.10` ADR
2. Per cluster: `winner_level = signal_close ± winner_threshold × ADR`, `loser_level = signal_close ∓ loser_threshold × ADR`
3. Race (hard stop on bar lows/highs from signal+1, exit evaluated after forward window):
   - Bar low/high breaches loser_level → `AUTO_LOSS` reason `mae_breach`
   - Exit fires, close clears winner_level → `AUTO_WIN` reason `clear_winner`
   - Exit fires, close in zone → `AUTO_WIN` reason `exit_in_zone` (scratch)
   - Neither in 120 bars → `AUTO_WIN` reason `timeout` (held)
4. Tie goes to stop (intrabar low breach wins over close-based exit on same bar).
5. No AMBIGUOUS category. All scratches and timeouts count as wins.

**Step 3 — Example override:**
- Clusters matched to examples are forced to `AUTO_WIN` regardless of race outcome. The race still runs so they get `stop_level`, `winner_level`, `exit_bar`, etc. for informational purposes.

**Step 4 — move_adr computation:**
- For clusters with `exit_bar`: measure `(entry_high − exit_close) / ADR` for shorts, reversed for longs
- Examples use entry candle high; non-examples use forward window max high (worst-fill assumption)
- ADR at signal bar from expression cache (`adr14`), fallback to manual 14-bar computation

**Scan-side example exemptions:**
- Example tickers skip the per-bar tradable filter (price/dvol/ADRP) and the 50-bar warmup mask. Required to guarantee example coverage for IPO setups (CRCL, CRWV) and high-liquidity low-ADRP tickers (MSFT, TJX).

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

## BUILD INCREMENTS

Full 11-increment build plan is in SIGNAL_GRINDER.md. Refinement-specific increments are **6** and **9**. All others are signal grinder, exit grinder, consensus engine, or orchestrator work.

### Increment 6 — `--skip-gather` + `--subsample-losers` + `--seed` + `--conditions-file` (refinement path)

Skip Phase 1. Loser subsampling. `signal_conditions_override` in `run_refinement()`.

Test:
```
# Requires: raw_signal_clusters_dtss.json exists in CACHE_DIR (from inc 5 or prior run)
COND_FILE=$(ls -t local_runner/cache/pyramid_dtss_mp_*.json | head -1)

# Without --subsample-losers (all losers, today's behavior):
python local_runner/pyramid_grinder.py --setup dtss --blackout \
  --skip-gather --conditions-file "$COND_FILE" \
  --output-dir local_runner/cache/test_consensus/
# Verify: "SKIPPING cluster gathering" message, uses all losers

# With --subsample-losers:
python local_runner/pyramid_grinder.py --setup dtss --blackout \
  --skip-gather --subsample-losers --seed 1 \
  --conditions-file "$COND_FILE" \
  --output-dir local_runner/cache/test_consensus/
# Verify: "Subsampled 50% of loser clusters" message, fewer losers

# Different seed produces different conditions:
python local_runner/pyramid_grinder.py --setup dtss --blackout \
  --skip-gather --subsample-losers --seed 2 \
  --conditions-file "$COND_FILE" \
  --output-dir local_runner/cache/test_consensus/
# Verify: different refinement_conditions_only than seed 1

# Verify signal_conditions in output comes from --conditions-file:
python -c "
import json, os
files = sorted([f for f in os.listdir('local_runner/cache/test_consensus') if f.startswith('refinement_')])
d = json.load(open(f'local_runner/cache/test_consensus/{files[-1]}'))
print(f'signal_conditions: {len(d.get(\"signal_conditions\",[]))}')
print(f'refinement_conditions_only: {len(d.get(\"refinement_conditions_only\",[]))}')
print(f'all_conditions: {len(d.get(\"all_conditions\",[]))}')
"

rm -rf local_runner/cache/test_consensus/
```

### Increment 9 — `consensus_engine.py` refinement mode

Read refinement JSONs. Consensus + binomial test. Full output assembly with correct schema (per-signal field format and cluster-to-signal mapping documented above).

Test:
```
# Requires: raw_signal_clusters_dtss.json in CACHE_DIR,
#   consensus_signal_dtss.json in CACHE_DIR (from inc 8 or manual),
#   signal_exit_dtss.json in data/signal_exit_grind/

mkdir -p local_runner/cache/consensus/test_ref
COND_FILE=local_runner/cache/consensus_signal_dtss.json

python local_runner/pyramid_grinder.py --setup dtss --blackout \
  --skip-gather --subsample-losers --seed 1 \
  --conditions-file "$COND_FILE" \
  --output-dir local_runner/cache/consensus/test_ref/

python scripts/consensus_engine.py --setup dtss --stage refinement \
  --threshold 0.7 --input-dir local_runner/cache/consensus/test_ref/

# Verify output has ALL required fields:
python -c "
import json, os
f = [f for f in os.listdir('local_runner/cache') if f.startswith('refinement_dtss_') and 'consensus' in f]
assert f, 'No consensus refinement output found'
d = json.load(open(f'local_runner/cache/{sorted(f)[-1]}'))
for k in ['all_conditions','refinement_conditions_only','signal_conditions',
           'exit_condition','winner_signals','loser_signals','eliminated_signals',
           'depth_progression','summary','params']:
    assert k in d, f'Missing key: {k}'
    print(f'  {k}: {type(d[k]).__name__} len={len(d[k]) if isinstance(d[k],list) else \"n/a\"}')
# Check per-signal field format:
w = d['winner_signals'][0]
for fld in ['ticker','signal_date','bar_idx','close','classification','move_adr',
            'adr_at_signal','entry_high','is_example','exit_bar','exit_date']:
    assert fld in w, f'winner_signals missing field: {fld}'
print('ALL FIELDS PRESENT')
"

rm -rf local_runner/cache/consensus/test_ref/
```

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


## CRITICAL IMPLEMENTATION RULES — DO NOT VIOLATE

These rules were learned through crashes, RAM overloads, and broken runs. They are non-negotiable.

### RAM Management

1. **`del universe_cache + gc.collect()` between phases is INTENTIONAL.** The 5yr OHLCV cache (~2-3GB) must be freed before allocating the loser matrix, cluster membership arrays, cand_passes, and beam search data structures. Removing this "redundancy" crashes 32GB RAM. The free-reload pattern in `_gather_raw_signal_clusters` is the same: free before scan, reload for classification. Not redundant — deliberate RAM staging.

2. **ProcessPoolExecutor copies data to every worker.** Using it for the beam search inner loop created 11 copies of `cand_surviving_bool` and crashed RAM. ThreadPoolExecutor shares memory but doesn't parallelize CPU work (GIL). The beam search runs single-threaded with numpy vectorization — that's fast enough.

3. **The `.df` field in refinement `win_example_dfs` is dead weight.** Neither `compute_example_ranges` nor `validate_examples` reads it — both use expr cache via `ticker` + `scan_idx` only. The `.df` field IS used in the signal grind path (`_run_single_pass` line 2250) so do not remove it from `load_example_data`. Only the refinement path (`_load_refinement_piles`) omits it.

### Data Type Safety

4. **`np.where()` returns numpy int64, not Python int.** Any value from numpy operations that gets stored on cluster dicts (which are later JSON-serialized) MUST be wrapped in `int()` or `float()`. `json.dump()` cannot serialize numpy types. This applies to `exit_bar`, `breach_bar`, and any future fields derived from numpy indexing.

5. **`round()` on a numpy float returns a Python float** — that's safe for JSON. But bare numpy array indexing (e.g., `highs[idx]`) returns numpy float64 — wrap in `float()` before storing on dicts.

### Beam Search

6. **Beam search is non-deterministic.** Ties in candidate scores are broken by processing order, which depends on dict iteration order and numpy sort stability. This is fine — consensus runs are supposed to produce different results. Do not try to make it deterministic.

7. **`--skip-gather` is safe for consensus runs.** The gather output is deterministic (same signal conditions + same data = same clusters). The beam search is the non-deterministic part. `_load_refinement_piles` reads the cluster file independently by filename — it never uses the return value from `_gather_raw_signal_clusters`. No in-memory state crosses from gather to beam search.

### Testing

8. **Test the full data lifecycle, not just computation.** An optimization that produces correct WR numbers can still crash at JSON save (int64), crash at RAM allocation (ProcessPoolExecutor copies), or crash at cleanup (stale variable references). Always run through the save step before declaring an optimization working.

9. **Sandbox-test every optimization before pushing to the live file.** Reproduce the old behavior, apply the change, verify identical results, verify no new failure modes (serialization, RAM, missing variables).

### Classification

10. **Direction-aware stop logic:** Shorts use highest high as stop (close above = loss). Longs use lowest low as stop (close below = loss). Longs additionally require exit to fire ABOVE entry zone high to count as WIN — exit below entry zone = LOSS. This is implemented in the vectorized classification section of `_gather_raw_signal_clusters`.

11. **NEVER use bar_idx or scan_idx to match examples to clusters.** Always use the hardcoded `entry_date` from the examples table. Bar indices shift when OHLCV or expr caches rebuild. Dates are stable.


## OPTIMIZATION CHANGELOG

### 2026-03-26 — BRKO session (beam search + data pipeline)

All changes in `local_runner/pyramid_grinder.py`, branch `v2`.

**Bug fixes:**
- **Removed stale `tcache` reference** (line 2967). The vectorize commit (`f75f37a8`) replaced the per-ticker incremental expr cache loader (`tcache` dict) with inline per-ticker loading in the classification loop, but left `del universe_cache, tcache` referencing the now-gone variable. Caused `UnboundLocalError` on every refinement run.
- **Cast numpy int64 to Python int** for `exit_bar` and `breach_bar` (lines 2810, 2815). The vectorize commit replaced Python `for`-loop bar scanning with `np.where()`, which returns numpy int64 indices. These got stored on cluster dicts that are later passed to `json.dump()`, which cannot serialize numpy types. Wrapped both in `int()`.

**Beam search optimizations (ClusterAwareRefinementSearch.run):**
- **Numpy bool arrays replace frozensets** for cluster tracking. Each candidate's surviving clusters stored as bool array of length `n_clusters`. Intersection = numpy bitwise AND. Per-node batching: one node ANDed against ALL candidates in a single broadcast operation. 37x faster, 97% less RAM.
- **Sorted tuple dedup replaces frozenset** for the `seen` set in beam expansion. The sorted tuple is already computed for the result — using it as the dedup key eliminates building a separate frozenset. 1.7x faster, 8x less memory per key. Safe because the `ci in used` guard ensures ci is never already in conditions, so frozenset dedup and tuple dedup agree.
- **Partial sort via `np.argpartition`** replaces full `list.sort()` on `next_candidates`. Only needs the top `beam_width` from up to `beam_width * 8` candidates — `argpartition` finds them without sorting the rest. 3.5x faster on the sort step.
- **Stored `used` set alongside each node.** Previously `used = set(conditions)` was rebuilt from the conditions tuple for every node at every level. Now `used` is created once at seed and updated incrementally (`used | {ci}`) when a candidate is added. Saves ~1.4s over 100 levels. Node tuples changed from `(conditions, surviving)` to `(conditions, surviving, used_set)`. `_level_summary_np` and `_print_level_np` updated to handle 3-tuples.
- **Dead node filter at level boundary.** Previously nodes with score=0 (all losers eliminated) were checked and skipped inside the expansion loop every level. Now they're filtered out when building `current_level` at the end of each level: `if score > 0`. The best node is preserved even if score=0 (all losers eliminated — search complete).
- **Matrix-multiply cluster set pre-computation.** `cand_passes @ cluster_membership` (bool→float32 matmul) computes all candidates' surviving cluster sets in one operation. Replaced a Python double loop over candidates × clusters. 17s → 0.6s.

**Data pipeline optimizations:**
- **`--skip-gather` flag** added to CLI. When set, `run_refinement()` checks if `raw_signal_clusters_{setup}.json` already exists on disk. If valid (parseable, has clusters), skips the entire `_gather_raw_signal_clusters()` call (~5 min for BRKO). Falls back to running gather if file is missing, corrupt, or empty. Safe for consensus runs because gather output is deterministic and the beam search (the non-deterministic part) is what consensus needs to vary. `_load_refinement_piles` reads the cluster file independently by filename — no in-memory state crosses from gather to beam search.
- **Dropped `.df` from refinement `win_example_dfs`** in `_load_refinement_piles`. The `df` field (full OHLCV dataframe copy per winner cluster) is never read by `compute_example_ranges` or `validate_examples` — both use expr cache via `ticker` + `scan_idx` only. Also removed the `df.copy()` + date conversion that produced it. Saves 190-570MB RAM depending on number of unique tickers. The `.df` field is still present in `load_example_data` (signal grind path) where it IS used by `_run_single_pass`.
- **ThreadPoolExecutor(4) for .npz I/O** in both winner range computation (`compute_example_ranges`) and loser matrix building. Overlaps disk reads across 4 threads — I/O bound work where the GIL doesn't matter.
- **Candidate-column-only loser matrix.** Only builds columns for expressions with valid winner ranges (have both min and max across all winners). Halves the matrix size (e.g., 7,419 columns instead of 15,805 for BRKO).

**Classification optimizations:**
- **Vectorized classification** via `np.where`. Replaced Python for-loop that scanned bar-by-bar for stop breach and exit fire with two numpy calls: `np.where(closes > stop_level)` and `np.where(exit_series >= thresh)`. Compare indices to determine winner.
- **Batched by ticker.** Group clusters by ticker, load OHLCV + expr cache once per ticker instead of per-cluster. Cuts disk I/O from ~4,685 to ~2,012 loads for BRKO.
- **Direction-aware stop logic.** Shorts: stop = highest high, close above = loss. Longs: stop = lowest low, close below = loss. Longs require exit to fire ABOVE entry zone high to count as WIN.

## OPEN QUESTIONS FOR IMPLEMENTATION

1. ~~The loser subsampling seed — passed as a parameter to `run_refinement()`, or generated internally per run?~~ **RESOLVED:** `--seed` passed as CLI arg, but subsampling only happens when `--subsample-losers` is also set. Manual runs omit `--subsample-losers` and use all losers (today's behavior). The orchestrator always passes both flags.
2. Consensus threshold: exact value within 0.6-0.9 range — start with 0.7 and see?
3. For the binomial test universe baseline: load all expression cache data into memory at once, or stream per-ticker? Memory implications with ~4,167 tickers × ~1,260 bars.
4. If the signal population is much larger (2,500+ clusters instead of 893), the refinement matrix is also larger. Does beam 10,000 still finish in seconds, or does it need adjustment?
5. The `--skip-gather` flag skips Phase 1, but Phase 2 (`_load_refinement_piles()`) still loads the 5yr OHLCV cache for building `win_example_dfs`. Is that necessary, or can it read everything it needs from the cluster JSON + expression cache?
