# CONSENSUS.md

## Status

Sessions 1-5 complete. Inc 1-10 built, 9/9 test steps pass (1216s/20.3 min, down from 35 min after Session 5 optimizations). Pipeline is overnight-feasible: per-grind cost is **~12.8 min** at the corrected defaults (BRKO benchmark, 47 conditions found, all examples validated). Full pipeline estimate: ~8.5 hours.

**Session 5 (2026-04-10/11) findings — major:**
1. **Bar count bug** in tier worker code: workers were using OHLCV bar counts to index the expression cache (which has fewer bars). Result: historical tiers were no-ops in every grind — only 1 ticker survived per tier. Every prior "consensus run" was effectively D1-only. Fixed by date-based lookups in 7 places across `pyramid_grinder.py`, `signal_exit_grinder.py`, and `entry_grinder.py`.
2. **Orchestrator default `--beam 10000` was wrong.** The matmul vectorization that made beam=10000 viable was reverted in Feb (commit `e683eb2`). Without it, beam=10000 takes hours per grind. **beam=500 is the correct default** — it matches the historical 60-min grind baseline and is sufficient because diversity comes from multi-run consensus, not single-run width. Changed orchestrator default to beam=500.
3. **No tradable filter applied**: grinder was scanning all 11,525 raw tickers including penny stocks and illiquid names. Added `compute_tradable_masks()` per-bar liquidity filter (price≥$1, dvol≥$4M, ADRP≥1.8%, applied per-bar so historical bars from currently-illiquid tickers are still kept). Reduces effective universe to ~6,468 tickers with ~32% of bars qualifying.
4. **EODHD exchange filter gap**: 297 tickers missing because `EODHD_EXCHANGES` didn't include `"NYSE MKT"` or `"AMEX"` labels. Added both. Recovers EQX, UAMY, REPX, LEU, BTG and other AMEX names.
5. **build_tradable.py was reading empty SQLite table** instead of the OHLCV pickle. Rewrote to use the pickle. Now produces 3,499 qualified tickers, matching the historical baseline Dan remembered.

Next: Session 6 = MORE OPTIMIZATION before the first real run. Dan explicitly stated we are not running the real pipeline yet — wants more margin first. The current 12.8 min/grind = ~8.5 hr pipeline is overnight-feasible but Dan wants additional headroom. See "Discovered Optimizations" section below for the menu of options ordered by impact and risk.

After Session 6 optimization, before first real run:
- Run nightly refresh (`python local_runner/nightly.py`)
- Append expression cache (`python local_runner/expr_cache_builder.py --append`)
- Run consensus pipeline (`python scripts/run_consensus_pipeline.py --setup dtss`)

**Entry grinder still deferred** per Dan's earlier instruction — needs planning.

## What This Is

Multi-run consensus for the signal grinder. The beam search is non-deterministic — different runs find different condition sets. Running N times with 50% universe subsampling and keeping only conditions that appear consistently (Meinshausen & Buhlmann 2010 stability selection) produces a stable, overfitting-resistant signal condition set.

A permutation test (15 real + 15 permuted runs) provides the noise floor. z > 3 required to proceed.

## Reference

- Full spec, architecture, and build increments: `SIGNAL_GRINDER.md`
- Refinement consensus (runs in the same pipeline): `REFINEMENT_GRINDER.md`
- Orchestrator: `scripts/run_consensus_pipeline.py` (Inc 10 complete, tested in --test-mode)
- Consensus engine: `scripts/consensus_engine.py` (signal + refinement modes complete)

## Build Plan (6 Sessions)

### Context
Purpose: Produce curve-fit-proof conditions autoregulated to setup example size. More examples should allow more conditions without risking curve fit. The permutation test IS the autoregulation — it measures the noise floor specific to the beam search algorithm, feature space, and sample size. With 68 examples, noise produces X conditions. Real produces Y. The z-score tells you if Y is real. With 120 examples, the noise floor drops (tighter bounding boxes = fewer coincidences), more real conditions survive, and the system gets better — continuous improvement, not a binary gate.
How: Meinshausen & Buhlmann (2010) stability selection. Run 15 real + 15 permuted grinds with 50% universe subsampling. Keep only conditions stable across runs. z > 3 required to proceed (99.7% confidence the pattern is real, not noise).
Current state: Pipeline is unbuilt. Only --skip-gather exists in pyramid_grinder CLI. Grinders still use old .npz ExprSeriesCache. Entry grinder v1 exists but unvalidated. Exit grinder is superseded by signal_exit_grinder + profit_grinder.
Spec: SIGNAL_GRINDER.md (authoritative, 930 lines, 11 build increments defined)
Critical constraint — single-run bootstrapping path:
The consensus pipeline needs ~100+ examples for a meaningful z-score. But new setups start with ~20. The single-run pipeline (pyramid_grinder.py --setup dtss with NO consensus flags) is the bootstrapping mechanism: run once -> produce winner pile -> vet charts in UI -> add examples -> repeat until ~100 -> THEN run consensus. The single-run path must never break. All consensus flags are additive; the existing no-flags code path stays untouched. Every increment's test includes a backward-compat check: run without consensus flags, verify same output as before.

### Phase 0: Expression Cache Assessment (do first)
Goal: Determine if .npz cache is valid and what fixes are needed.
The pyramid_grinder and entry_grinder use ExprSeriesCache which loads .npz files (16K columns per ticker). The scanning system (signal_filter, scan_engine) already migrated to .im files (196 intermediates). The grinders CANNOT migrate to .im yet — the beam search needs all 15,805 expression values at arbitrary bars, but only ~42 expression types are dispatch-able from .im intermediates. The remaining ~15,763 (bool aggregates, SLOW_OPS, LSP, algo, HTF) would need ExpressionEngine fallback, which is too slow for beam search.
Recommendation: Keep .npz for grinders. The .im migration is a separate future project requiring expanding the .im format to cover all expression types.
Tasks:
- Check .npz cache status: do the files exist? Is _manifest.json valid? Does ExprSeriesCache().is_valid() pass against current generate_all()?
- Check bar alignment: do .npz dates align with 5yr OHLCV after EXPR_CACHE_START changes?
- If cache is stale: assess what changed and whether a rebuild is needed (80-120 min)
- Verify entry_grinder's ExprSeriesCache usage still works
Key files:
- local_runner/expr_cache_builder.py — ExprSeriesCache class (line 2179)
- local_runner/pyramid_grinder.py — cache loading (line 1977)
- scripts/entry_grinder.py — cache loading (line 137)

### Phase 1: Consensus Pipeline Build (11 Increments)
Following SIGNAL_GRINDER.md spec exactly. Each increment independently testable.

**Increment 1 — CLI Skeleton**
Add all new argparse arguments to pyramid_grinder.py main() and signal_exit_grinder.py main(). No logic changes, just parsing + validation.
New args: --permute, --subsample, --seed, --pass-order, --zero-margin, --no-peak-target, --scan-only, --conditions-file, --output-dir, --subsample-losers
Validation rules: --scan-only requires --conditions-file, mutually exclusive with --blackout/--permute/etc. --pass-order must be permutation of 1,2,3.
Files: local_runner/pyramid_grinder.py (main, ~line 3600), scripts/signal_exit_grinder.py

**Increment 2 — --output-dir + Railway suppression**
When --output-dir set: grind JSONs write there, skip mirror_file() and grind_uploader.upload(). os.makedirs(output_dir, exist_ok=True).
Files that always use CACHE_DIR regardless: cluster files, final consensus output.
Files: local_runner/pyramid_grinder.py (save logic, ~line 2150)

**Increment 3 — Core consensus mechanics**
The big one. Inside run_pyramid():
- --seed: rng = random.Random(seed) for all randomization
- --subsample 0.5: after cache load, filter universe_cache to 50% of tradable tickers using rng
- --zero-margin: after compute_example_ranges(), overwrite each range with exact min/max (same pattern as refinement lines 3106-3116)
- --no-peak-target: pass peak_target=0 to beam search
- --pass-order 2,1,3: reorder MULTI_PASS_DEFS (line 1604) per specified ordering
- D1 filtering: after loading universe matrix, filter rows to only tickers in subsampled universe_cache
Files: local_runner/pyramid_grinder.py (run_pyramid, ~line 1786)

**Increment 4 — --permute + filename prefix**
Fake example generation inside run_pyramid() after cache loading:
- Generate 68 fake examples: random ticker from tradable universe, random bar (50..n-1), verify non-NaN
- Use as override_example_dfs
- Change output prefix to permuted_{setup}_* instead of pyramid_{setup}_*
Files: local_runner/pyramid_grinder.py

**Increment 5 — --scan-only + --conditions-file (scan path)**
New code path in main(): load conditions JSON, pass to _gather_raw_signal_clusters() via conditions_override param, save cluster file, exit. No beam search.
Files: local_runner/pyramid_grinder.py (main + _gather_raw_signal_clusters)

**Increment 6 — --skip-gather + --subsample-losers + --conditions-file (refinement path)**
Extends existing --skip-gather. Adds loser subsampling with --seed. --conditions-file populates signal_conditions field in output instead of _load_signal_conditions().
Files: local_runner/pyramid_grinder.py (run_refinement, ~line 3200)

**Increment 7 — signal_exit_grinder.py --conditions-file**
Bypass internal load_pyramid_conditions() when --conditions-file provided.
Files: scripts/signal_exit_grinder.py

**Increment 8 — consensus_engine.py signal mode (full rewrite)**
Phases A-E from spec:
- A: Read real run JSONs, count condition frequencies
- B: Read permuted run JSONs, count condition frequencies
- C: Bootstrap z-score (1000 draws)
- D: Gate decision (z > 3 proceed, z < 2 stop)
- E: Lock conditions with 5% margin
Output: consensus_signal_{setup}.json
Files: scripts/consensus_engine.py (full rewrite)

**Increment 9 — consensus_engine.py refinement mode**
Read refinement JSONs. Consensus + binomial test. Output with full schema per REFINEMENT_GRINDER.md.
Files: scripts/consensus_engine.py

**Increment 10 — run_consensus_pipeline.py orchestrator**
New script. Subprocess execution of each step. Interleaved real/permuted runs. Early abort after 3+3. z-gate. Chaining Steps 1 -> 2 -> 3 -> 3.5 -> 4 -> 5. Nightly refresh guard. Stops after Step 5 (refinement consensus). EV grinder and profit grinder are a separate future build ("live EV ranked watchlist").
Files: scripts/run_consensus_pipeline.py (new)

**Increment 11 — test_consensus_pipeline.py automated test runner**
Mini run (1+1 instead of 15+15). Self-verifying format checks per step. Clean consensus/test/ directory. Pass/fail report. 7 steps (signal grind, permuted grind, consensus engine, scan-only, exit re-grind, refinement, refinement consensus). No EV/profit grinder steps.
Files: scripts/test_consensus_pipeline.py (new)

### Phase 2: Entry/Exit Grinder Fixes
Can run after Phase 1 produces signal populations. Depends on Phase 1 Increment 5 (scan-only mode produces cluster file) and Phase 1 Increment 7 (signal_exit_grinder with --conditions-file).

**Signal Exit Grinder Validation**
- Run signal_exit_grinder to produce signal_exit_{setup}.json
- Verify output format matches what entry_grinder expects
- Ensure --conditions-file path works for consensus integration

**Entry Grinder Fixes**
- Ratchet path investigation: Diagnose why ratchet search returns 0 survivors. Use near-miss diagnostics from v1 to determine if it's a logic bug or legitimate.
- Signal condition validation: Verify signal conditions actually fire on scan bar for every example (not just cluster proximity)
- Expression cache compatibility: Ensure ExprSeriesCache works with current .npz state
- Forward window source: Resolve whether to read from cluster file or setups table

**NOT in scope (separate build later)**
- EV grinder — will be part of "live EV ranked watchlist" build
- Profit grinder — same, deferred to post-consensus
Key files:
- scripts/entry_grinder.py
- scripts/signal_exit_grinder.py
- ENTRY_GRINDER.md

### Phase 3: Optimization
Priority order:
1. Nightly refresh + forward scanning speed (daily operation, live watchlist). This is the production-critical path. Consensus changes must not slow it down.
2. Consensus pipeline speed (infrequent, quarterly). Acceptable to be slow as long as it completes overnight. Optimize only if it exceeds the overnight window.
Guard rail: Before any optimization, verify nightly refresh + forward scan timing is unchanged. If consensus work regressed daily speed, fix that first.
Consensus-specific hot spots (optimize only if overnight window is exceeded):
- Tier matrix building: parallel workers load .npz per ticker
- Beam search expansion: beam_width * 8 cap
- Expression cache I/O: each of 30 runs reloads same .npz files
- Subprocess startup: ~10-15s per run x 30 = ~7-8 min overhead
Optimization is data-driven — profile first, fix actual bottlenecks.

### Verification

**Per-Increment Testing**
Each increment has test commands in SIGNAL_GRINDER.md. Run those exactly.

**End-to-End Test**
python scripts/test_consensus_pipeline.py --setup dtss
Mini run (1 real + 1 permuted). 7-step verification through Step 5 (refinement consensus). ~45 min.

**Full Overnight Run**
python scripts/run_consensus_pipeline.py --setup dtss
15+15 signal grinds -> consensus -> scan -> exit re-grind -> refinement x 10 -> refinement consensus. Early abort at ~2 hours. Stops after Step 5. ~8-10 hours.

### Cross-Session Continuity
No new documents. SIGNAL_GRINDER.md is the source of truth. No build tracker file that could go stale.
- Memory entries — 1-2 compact entries saved at end of each session: what's done, what broke, key decisions. Auto-loaded next session.
- Git commits per increment — git log IS the build tracker. One commit per increment, message references spec section.
- Test runner grows from Session 1 — skeleton in Session 1, steps added each session. Catches regressions before they compound.
- CONSENSUS.md — one-line status update when pipeline works. No other doc changes.
- Each session starts by re-reading the relevant SIGNAL_GRINDER.md section + memory + running the test runner against all completed increments.

### Session 1 — Foundation + Core Mechanics
- Phase 0: Cache assessment (verify .npz, fix if needed)
- Inc 1: CLI skeleton (argparse only)
- Inc 2: --output-dir + Railway suppression
- Inc 3: Core consensus mechanics (seed, subsample, zero-margin, no-peak-target, pass-order)
- Test runner skeleton: Step 0 (single-run backward-compat check — no consensus flags, verify clusters + winner pile) + Steps 1-2 (real + permuted grind with tiny beam, verify format)
- Tests: Single-run regression first. Then spec's Inc 1-3 test commands. Run same seed twice for determinism. Test runner green.
- Debug targets: D1 matrix row filtering with subsampled universe, pass ordering, zero-margin overwrite

### Session 2 — Permute + All Condition Injection
- Inc 4: --permute + filename prefix
- Inc 5: --scan-only + --conditions-file
- Inc 6: --skip-gather + --subsample-losers + --conditions-file (refinement)
- Inc 7: signal_exit_grinder.py --conditions-file
- Test runner grows: Add steps 3-6 (permuted grind, scan-only, refinement, exit re-grind)
- Tests: Spec's Inc 4-7 test commands. Test runner covers Sessions 1+2 increments.
- Debug targets: Fake example generation edge cases, conditions_override plumbing, scan-only must skip beam search

### Session 3 — Consensus Engine
- Inc 8: consensus_engine.py signal mode (full rewrite, Phases A-E, bootstrap z-score 1000 draws)
- Inc 9: consensus_engine.py refinement mode
- Test runner grows: Add step 7 (consensus engine on real+permuted outputs from steps 1-2)
- Tests: Generate test data from Session 1-2, run consensus engine, verify z-score + condition locking + 5% margin. Test runner covers all increments so far.
- Debug targets: Bootstrap edge cases (std_P = 0), condition name matching, margin arithmetic

### Session 4 — Orchestrator + Full E2E
- Inc 10: run_consensus_pipeline.py orchestrator (Steps 1-5 only, no EV/profit)
- Complete test runner to full Inc 11 spec (7 steps: real grind, permuted, consensus, scan-only, exit re-grind, refinement, refinement consensus)
- THE INTEGRATION SESSION: Run test_consensus_pipeline.py --setup dtss (~45 min). First full E2E. Fix whatever breaks.
- Debug targets: Subprocess arg passing, file paths between components, output schema mismatches. This session is mostly debugging.

### Session 5 — Entry Grinder + Optimization + Overnight Prep
- ✅ Signal exit grinder validation (format compatible, --conditions-file works)
- ⏸️ Entry grinder: deferred per Dan's instruction (needs planning input)
- ✅ Verified nightly refresh + forward scan code untouched (zero overlap with grinder)
- ✅ Profiled and discovered the bar count bug + matmul revert + tradable filter gap
- ✅ Final test_consensus_pipeline.py run: 9/9 PASS in 1216s (down from 35 min)
- ✅ Pipeline now overnight-feasible at 12.8 min/grind (BRKO benchmark)
NOT in this build: EV grinder, profit grinder — deferred to "live EV ranked watchlist" build.

### Session 6 — First real overnight run + entry grinder
1. Pre-flight: nightly refresh, expr cache append, verify alignment
2. Optional: review/apply discovered optimizations (see "Discovered Optimizations" below)
3. Run consensus pipeline against DTSS or BRKO
4. Entry grinder work (separate planning conversation with Dan first)

## Discovered Optimizations (from Session 5 deep dive)

These were identified during Session 5 timing investigation. None are blocking — the current pipeline is overnight-feasible at 12.8 min/grind. Listed in priority order:

**Higher impact, higher risk:**

1. **Matmul vectorization revival in `PeakSpiderweb`** — commit `ecdd793` added matmul pre-screening that batches `(beam, candidate)` pair evaluation into a single SGEMM call. Reverted in `e683eb2` for tuning reasons (produced different signal counts than the goal "sub-200" tuning at the time). At beam=500 it gives ~5% pipeline savings AND broader beam node coverage (current capped iteration only expands the first ~9 of 500 beam nodes). At beam=10000 it makes that beam viable. See `reference_matmul_revival.md` in memory for the algorithm.

2. **Slim expression cache** — pre-compute per-ticker .npz files containing only the ~2,000 useful expressions instead of all 16,039. Cache shrinks from 98 GB to ~17 GB (fits in OS file cache on 32 GB). Per-grind I/O drops 5-7×. ~1 hour one-time build cost.

3. **Persistent worker pool within pass** — workers stay alive across tiers and cache loaded `.npz` data. 6× I/O reduction in Pass 1, 4× in Pass 2, 3× in Pass 3. Memory pressure on 32 GB system, requires column slicing. Architectural change.

**Lower impact, lower risk:**

4. **Cache `example_matrix` across passes within a grind** — currently computed 3× per grind (once per pass with different expression filter). Compute once for all expressions, slice per pass. Saves ~7-10 min total across 30 grinds.

5. **Float16 throughout** — drop the `float32` cast on .npz load. 50% memory, 30-40% load time. Precision risk in `bincount` and accumulation operations.

6. **Shared memory for tier matrices** — workers currently serialize tier matrices via pickle to send to parent. `multiprocessing.shared_memory` eliminates the round-trip. ~10-20% per tier.

7. **Memory-map universe matrix** — convert from pickle to `.npy` + mmap. Lazy loading. ~2-4 min savings.

8. **Batch grinds in single subprocess** — orchestrator currently launches 40 separate Python processes. Run multiple grinds in same interpreter to save startup cost. ~10 min savings. Watch for memory accumulation.

**What was investigated and dismissed:**
- **Parallelizing grinds** (running 2+ in parallel via different processes) — bad idea. Each grind already uses all 15 CPU cores. Parallel grinds would oversubscribe and thrash.
- **Heuristic skip 5yr tier** — too risky. 5yr does add conditions for some setups (e.g. BRKO benchmark Pass 1 added 10 from 5yr).
- **Refinement blackout/whitelist date translation** — investigated, not needed. blackout_map is never assigned; whitelist_map is consumed by `run_refinement`'s own loser matrix builder which already uses cache coordinates correctly.
