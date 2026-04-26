# Classifier Spec — Win/Loss Classifier + Exit-Grind Rebuild

Authoritative working doc for the classifier rebuild. Supersedes the old CONTEXT_SALVAGE.md, EXPERIMENTS_CATALOG.md, NEXT_SESSION.md, and CLASSIFIER_CONSTRAINTS.md, all deleted as of 2026-04-17.

---

## 1. Problem

Per active setup (HTF, BF, BASE, DTSS), produce two artifacts that make downstream win/loss metrics accurate and robust:

1. **Signal exit rule** — one close-based TA expression + threshold, chosen by classifier-objective scoring (not MFE capture), applied to the full deduplicated pre-classification signal pop per setup.
2. **Classification rule system** — per-cluster, assigns each cluster in the deduped pop to one of five piles (5-pile framework ratified 2026-04-18):
   - **WIN** — entry fired; exit fires (by exit rule OR forced at earnings-1) at close on favorable side of entry.
   - **LOSS** — entry fired; stop breached intraday at initial entry-bar-extreme stop; loss is flat 1 ADR.
   - **BE** — entry fired; BE ratchet armed, ratcheted stop hit intraday.
   - **AMBIGUOUS** — entry fired; exit fires (by rule OR forced at earnings-1) at close between stop and entry price (losing-but-not-stop-level).
   - **NO_ENTRY** — signal fired but no entry bar matched the entry mechanic within the entry-discovery window. Not a trade.

**Objective:** minimize |AMBIGUOUS| while W/L/BE survive data-science rigor (LOO, holdout, alt-seed). NO_ENTRY is a structural category (signal without trade) and is excluded from p_ambig denominator per §12.4.

Metaphor: battleship. Triangulate outcomes from signal + forward data alone; no entry-bar lookup allowed for non-examples.

Current classifier code: `_gather_raw_signal_clusters()` in `local_runner/pyramid_grinder.py`. Output schema: `raw_signal_clusters_{setup}.json`. Exists for DTSS only; HTF/BF/BASE need Phase 1.

---

## 2. Fixed constraints

These bind any rule-derivation work. Not search variables.

1. **No entry-bar reference for non-examples.** Classification features and logic use signal-bar + forward-bar data only.
2. **Inferred entry candle picked per signal by fixed logic.** Deterministic rule selects a specific forward bar as the inferred entry candle.
3. **Entry fills intraday on the inferred entry candle.** Any time during that day — not locked to close or open.
4. **Default stop placement.** Extreme of inferred entry candle against the trade (low for longs, high for shorts).
5. **Stop placement and entry-price cap.** Stop sits at the entry candle's extreme against the trade (`entry_low` for longs, `entry_high` for shorts). Stop does **not** move up inside the bar. The 1-ADR limit is an **entry-price cap**, not a stop-distance cap: if the entry candle's low-to-close range exceeds 1 ADR(14) for longs, the *effective* entry is pulled down to `entry_low + 1·ADR` so risk from effective entry to stop is bounded at 1 ADR. For longs: `effective_entry = min(entry_high, entry_low + 1·ADR)`. Symmetric for shorts: `effective_entry = max(entry_low, entry_high − 1·ADR)`.
6. **Stop is fixed.** Stop set once at entry and does not move. BE-ratchet, trailing stops, etc. are profit_grinder territory (see halt 14).
7. **Loss = 1 ADR, flat.** By convention. Realized loss on stop hit = −1 ADR regardless of the actual effective_entry-to-stop distance (which is ≤ 1 ADR by construction).
8. **Exit timing.** Exit condition evaluated on close of forward bars; fill is intraday on the trigger bar.
9. **Entry-discovery window.** Bars 1..10 forward from signal bar. Entry bar = first k ∈ [1, 10] where `close[sig+k] > close[sig]` for longs (symmetric for shorts). None in window → NO_ENTRY.
9a. **Trade-lifetime window.** From entry bar forward, capped at `min(earnings_date − 1, 120 bars)`. If neither stop nor exit rule fires within this window, forced exit at the cap bar's close. Realized P&L = `(cap_close − effective_entry) / ADR_at_entry` for longs (sign-flip for shorts).
10. **Rule form.** Single boolean expression `op(value, threshold)` over signal-bar features + forward-bar data + OHLCV. Depth-2 AND/OR compositions are a follow-up if single-expression is inadequate.

**What this rules out:** rules or measurements that reference actual entry bars of non-examples; stop models with variable loss sizes (that's profit_grinder); measurement-only scripts that score a fixed race without proposing rule changes.

---

## 3. Data tools

### 3.1 Labeled winners (ground truth)
- **`data/scanperfect.db.examples`** — 219 rows. Columns: `id, setup_type, ticker, chart_date, entry_date, created_at`. Unique on `(setup_type, ticker, entry_date)`.
- Matched signal bar is strictly `entry_idx − 1` per ticker's expression-cache dates. No proximity fallback. Entry-date lookup uses dates, never bar indices.

### 3.2 Unlabeled pool (what classifier discriminates within)
- **`local_runner/cache/raw_signal_clusters_{setup}.json`** — cluster-deduped signal pool + classification labels from the current race. Per-cluster schema: `cluster_id, ticker, rightmost {bar_idx, date, close}, leftward[], size, is_example (0/1), example_entry_date, example_entry_idx, stop_level, classification (AUTO_WIN | AUTO_LOSS), classification_reason, exit_bar, adr_at_signal, entry_high, exit_date, move_adr`.
- **Exists for DTSS only** (verified 2026-04-17). HTF/BF/BASE need Phase 1 run before audit.
- **DTSS current state:** `n_raw=1374, n_clusters=1122, W=798, L=324, ex_matched=73, FW=3`. WR 71.1%.

### 3.3 Feature space
- **`local_runner/cache/expr_series/{TICKER}.npz`** — **~16,039 expressions** × ~1,200 bars. `EXPR_CACHE_START = 2020-01-02`. Opened via `_open_npz`.
- **Never rely on `.append` for research runs.** Forward-prop is best-effort; silent NaN fills possible. Before any consensus/research measurement: `expr_cache_builder.py --build`.
- Feature vocabulary: daily (4,017), HTF weekly (5,233), HTF monthly (5,233), extension (1,198), LSP (80), algo lines (44). Prefixes: `m_`/`w_`/`d_`. Vocabulary in `local_runner/brute_expressions.py::generate_all()`.

### 3.4 Price + outcome data
- **`local_runner/cache/universe_ohlcv_daily.pkl`** — `{ticker: DataFrame}`, cols `date, open, high, low, close, volume, dvol_20d`. Split+dividend adjusted. ~11,523 tickers. `HISTORY_START = 2016-01-01`.
- `universe_ohlcv_weekly.pkl`, `universe_ohlcv_monthly.pkl` — HTF bars, same format.
- **`data/scanperfect.db.earnings_dates`** — 94,334 rows. Columns: `ticker, earnings_date, updated_at`.
- **ADR14** = cached `adr14` column in expr cache; fallback = 14-bar SMA of `(high − low)`.

### 3.5 Pipeline artifacts
- **Pyramid JSONs** — `local_runner/cache/pyramid_{setup}_mp_sig{N}_pk{K}_{ts}.json`. `tier_results[last_tier].final_signals` is the raw bar list (pre-dedupe). `sig{N}` in filename = that tier's `final_total`. See §5.2 for dedupe.
- **Signal-exit JSONs** — `data/signal_exit_grind/signal_exit_{setup}_{n}ex_{adr}adr_{ts}.json` + `signal_exit_{setup}.json` latest pointer. `top_conditions[0]` is the current classifier exit.
- **Per-setup latest:**

  | setup | pyramid examples/DB | pyramid conds | signal_exit top | floor_adr |
  |---|---|---|---|---|
  | dtss | 73/73 | 112 | `w_ext_slope_xavgc50_off2 >= 0.355` | +0.6 |
  | htf | 32/32 | 86 | `w_rsi_slope_21_off3 <= -8.46` | **−0.4** ⚠ |
  | bf | 45/45 | 59 | `w_nr_h_maxh2_pct >= 7.75` | +1.3 |
  | base | 42/42 | 78 | `w_slope_xavgc9_off2_atr14 <= 0.403` | +0.5 |
  | 3-4db | 26/26 | 43 | `w_ext_slope_xavgc13_off3 >= 0.833` | **−0.4** ⚠ |

  ⚠ HTF + 3-4DB signal_exit top have floor_adr < 0 — at least one example loses money on the WIN-declaring exit. HTF driver is XPEV (Dan's read: needs two-stage exit, single-exit structurally limited); 3-4DB mechanism not yet investigated.

- **Refinement JSONs** — `local_runner/cache/refinement_{setup}_*.json`. Not yet produced for active setups.

### 3.6 Data NOT to use
- `scripts/signal_filter.py` — reference only; `raw_signal_clusters_{setup}.json` supersedes it.
- `data/signal_filter/filtered_*.json`, `data/signal_filter/classified_*.json` — stale outputs of the above.
- `local_runner/cache/consensus/` — intermediate artifacts, not production classifier output.
- `local_runner/cache/_archive/brko_legacy/` — pre-BRKO-rename archive.
- `archive/shelved_docs/`, `archive/shelved_scripts/` — deferred/shelved specs and code.
- `setup_library/*/description.md` — prose, not data.
- Shelved grinders (per `SHELVED.md`): `dartboard_grinder.py`, `hybrid_grinder.py`, `proximity_grinder.py`, `setup_refiner.py`, `market_grinder.py`, `setup_grinder.py`. Plus outputs `data/proximity_grind/`, `data/setup_refiner/`.

---

## 4. Code tools

### 4.1 Data layer
- `local_runner/cache_builder.py` — OHLCV fetch (EODHD + yfinance) → daily/weekly/monthly pickles.
- `local_runner/expr_cache_builder.py` — expression cache `.npz`. Ground-truth path = `--build`.
- `local_runner/matrix_builder.py` — universe_matrix.pkl for D1 tier prefilter.

### 4.2 Grinders (classifier-relevant)
- **`local_runner/pyramid_grinder.py`** — core. Roles: signal grind (beam search), cluster gathering (`_gather_raw_signal_clusters()` — THE CLASSIFIER), refinement grind (`run_refinement()` under `--blackout`).
- **`scripts/signal_exit_grinder.py`** — exit condition search. Inputs: OHLCV, expr cache, pyramid JSON, DB examples.
- `local_runner/spiderweb.py` — beam search impl.
- `scripts/consensus_engine.py` — stability selection + permutation test.
- `scripts/run_consensus_pipeline.py` — orchestrator.

### 4.3 Downstream consumers (read classifier output — don't break)
- `scripts/ev_grinder.py` — reads `raw_signal_clusters_*.json`, `refinement_*.json`. Hard-fails if any example doesn't score.
- `scripts/entry_candle_scorer.py` — reads `refinement_*.json` + `raw_signal_clusters_*.json`.
- `scripts/profit_grinder.py` — reads `raw_signal_clusters`, `ev_*`, `entry_scores`.
- `scripts/entry_grinder.py` — stop management. Deferred.

### 4.4 Compute libraries (pure, no I/O)
`scripts/expression_engine.py`, `scripts/profiling_engine.py`, `scripts/backtest_conditions.py`, `scripts/exit_expressions.py`, `scripts/exit_compute.py`, `scripts/lsp_detector_v2.py`, `scripts/algo_line_detector.py`, `local_runner/brute_expressions.py`.

### 4.5 Auditor
`audit.py` / `audit.sh` at main repo root. Runs on every commit + pull. Reads DEPENDENCY_MAP.md + spec docs. PASS/FAIL. Auto-revert on post-merge FAIL. Don't bypass — fix the root cause.

---

## 5. Reusable patterns

### 5.1 Earnings cap (searchsorted)
Replacement for the O(E×N) string-compare helper in `signal_exit_grinder.py:210–245`. Already used in prior `research/07_variant_sweep.py::earnings_cap()`.

At startup:
```
earnings_map_np[ticker] = np.array(sorted(set(dates_for_ticker)), dtype='<U10')
```

Per call `(ticker, scan_idx, df_dates_str_array)`:
```
ern = earnings_map_np.get(ticker)
if ern is None or len(ern) == 0: return None
signal_date_str = df_dates_str_array[scan_idx]
pos = np.searchsorted(ern, signal_date_str, side='right')
if pos >= len(ern): return None
next_ern = ern[pos]
bp = np.searchsorted(df_dates_str_array, next_ern, side='left')
if bp <= scan_idx: return None
return bp - scan_idx
```

O(log E + log N) per call. One helper instead of two. No Python list materialization per signal. Hygiene fix, not a correctness bugfix — yfinance ISO dates happen to lex-sort correctly.

### 5.2 Deduped pop source
Pyramid JSONs do NOT serialize cluster classifications or the deduped signal list. Top-level keys are `all_conditions, example_signals, examples_failing, examples_passing, multi_pass, n_conditions, params, pass_summaries, peak_target, refinement, setup_type, summary, tier_results, timestamp, total_time_s`.

**The raw signal list IS serialized** — inside `tier_results[last_tier].final_signals`, as `[{ticker, date}, ...]` sorted.

**To reconstruct deduped clusters:** read `final_signals`, apply consecutive-bar dedupe per ticker (rightmost wins), split clusters at example signal bars so each example is its own cluster's rightmost.

Per-setup verified counts:

| setup | raw signals | deduped clusters | example-matched | non-example raceable |
|---|---|---|---|---|
| htf | 545 | 493 | 23 | 466 |
| bf | 530 | 440 | 40 | 398 |
| base | 646 | 538 | 35 | 490 |
| dtss | 1369 | 1108 | 48 | 1048 |

**Don't modify `pyramid_grinder.py` to serialize classifications.** Race logic is under redesign; frozen labels would go stale. Read on demand; each caller picks its own race kit.

If a versioned snapshot is ever needed: write an independent artifact in `data/signal_filter_labels/` with kit parameters in the header.

---

## 6. Active experiments

### 6.1 Classifier worktree — `research/` (5 kept after 2026-04-17 shelving)
- **B1 `01_example_stats.py`** — per-example descriptive stats. Baseline reference.
- **B10 `10_breakeven_ratchet.py` (E3)** — ratchet bar-N sweep; smallest N with 0 LOSSes on examples. Self-inferring. Derives the BE mechanic.
- **B11 `11_entry_anatomy.py` (E4)** — entry-bar anatomy: descriptive feature-separation study comparing features at the entry bar vs later offsets. Does NOT produce an entry-inference rule. The previously-attributed `entry_close = max(close over [leftmost, entry_idx])` claim was FLAWED (retired 2026-04-18) — its range definition references `entry_idx`, violating §2 c1 (no entry-bar reference for non-examples). Empirically tautological on examples (always true by labeling construction), unusable on non-examples. Removed as an entry-mechanic candidate.
- **B16 `18_per_setup_rules.py` (E18)** — decorrelate top features by AUC + derive thresholds from winner p10/p90 + 1 and 2-feature AND rules. Methodology sound; needs clean winner sourcing (B14's issue) before re-run.
- **B17 `19_loo_validation.py` (E19)** — LOO on per-setup rule thresholds. Overfit defense — only script in the B-series that tests generalization.

### 6.2 Dual-exit worktree — `scripts/` + `research/` (4 kept)
- **A2 `scripts/realistic_mfe_analysis.py`** — MFE rescore under earnings-bounded hold windows. Earnings hygiene applies regardless of objective.
- **A3 `scripts/forced_exit_baseline.py`** — hold-to-earnings-minus-buffer floor. Any rule must beat this.
- **A10 `scripts/loo_stability.py`** — LOO pick recurrence across grinder runs.
- **A-E7 `research/14_exit_rule_check.py`** — top-10 alt exits re-raced against the full deduped non-example pop using searchsorted earnings. First classifier-objective attempt — right shape.

---

## 7. Shelved experiments

### 7.1 B-series (15 scripts, now in `research/archive/`)
Each one either (a) operated on a fixed race kit that conflicts with §2 constraints, (b) evaluated rules rather than deriving them, or (c) inherited contaminated inputs downstream of a flagged root script.

- B2 `02_variant_tester.py` — 8 stop variants raced on existing cluster file. Evaluated predefined variants.
- B3 `03_all_setups_v1.py` — V1 across all 4 setups. Superseded.
- B4 `04_v1_example_accuracy.py` — V1 example pass rate. Superseded.
- B5 `05_v1_test.py` — V1 with finalized dedupe. Superseded.
- B6 `06_derive_stop.py` — 7 handpicked stop formulas. Evaluation, not derivation.
- B7 `07_variant_sweep.py` — broader stop sweep. Evaluation.
- B8 `08_race_v1_examples.py` (E1) — worst-case-stop race on examples. Sanity gate, superseded.
- B9 `09_mae_timing.py` (E2) — MAE timing diagnostic. Fed B10; standalone value now low.
- B12 `13_race_population.py` (E6) — full pop race with fixed kit. Entry=signal+1 artifact.
- B13 `15_classifier_feature_sep.py` (E10) — feature separation on E6 labels. Inherits E6.
- B14 `16_winners_vs_losers.py` (E16) — winner-sourcing ambiguity (docstring vs code).
- B15 `17_distribution_overlap.py` (E17) — uses E16 groups.
- B18 `20_rule_apply_envelope.py` (E20) — max_fav overstates (envelope ignores stop).
- B19 `21_expectancy_parallel.py` (E21) — inherits E20.
- B20 `22_proper_measurement.py` (E22) — breakout branch uses close-based tradeable filter; spec says entry-bar extremes.

### 7.2 A-series (moved to profit_grinder "good ideas")
MFE-capture-oriented scripts shelved 2026-04-17. Catalogued in `../swing-screener-dual-exit/archive/shelved_docs/PROFIT_GRINDER.md` → "Good Ideas — MFE-capture exploration (parked 2026-04-17)" section. Scripts live in `../swing-screener-dual-exit/archive/shelved_scripts/`.

A1, A4, A5, A6, A7, A8, A9, A11 — exit-lag diagnostic, fill-assumption rescore, ADR-multiple TP grid, gated dual-exit, parallel dual-exit, per-ticker ext bimodality + rank exit, chart renderer.

---

## 8. Per-setup current state

| setup | DB ex | pyramid ex pass | pyramid n_cond | sig_exit top | floor_adr | raw_clusters | class WR |
|---|---|---|---|---|---|---|---|
| dtss | 73 | 73 ✓ | 112 | `w_ext_slope_xavgc50_off2 >= 0.355` | +0.6 | 2026-04-14 (1122, stale) | 71.1% (stale) |
| 3-4db | 26 | 26 ✓ | 43 | `w_ext_slope_xavgc13_off3 >= 0.833` | **−0.4** ⚠ | no | — |
| htf | 32 | 32 ✓ | 86 | `w_rsi_slope_21_off3 <= -8.46` | **−0.4** ⚠ | no | — |
| bf | 45 | 45 ✓ | 59 | `w_nr_h_maxh2_pct >= 7.75` | +1.3 | no | — |
| base | 42 | 42 ✓ | 78 | `w_slope_xavgc9_off2_atr14 <= 0.403` | +0.5 | no | — |

**Implications:** All 5 setups have fresh pyramid + signal_exit (2026-04-18). Classifier cluster file exists for DTSS only and predates today's grinds — stale under the current race kit. Phase 1 has not been run on any setup against today's pyramids. HTF + 3-4DB signal_exit top conditions have floor_adr < 0 — both carry the "losing example on WIN-declaring exit" bug; accepted for now, future classifier BE rule scratches these.

---

## 9. Open issues + next steps

### 9.1 Spec-flagged (SIGNAL_FILTER.md:164–170)
- DTSS classifier too lenient (71.1% WR vs 40–60% expected). Proposed: narrow ceiling to signal-bar high only, keep race start at FW+1. Not yet tried.
- BRKO/breakout loser stop is universal `0.60 × ADR` driven by one example (PTON). Proposed: per-signal FW-low stop.
- BRKO `move_adr` floor is −4.83 — measurement artifact from `entry_high`-vs-`signal_close` divergence.
- Winner threshold +1 ADR buffer is structural (fill-to-stop distance under 1-ADR normalization).
- Loser threshold stalls for high-ADR stocks (0.60 ADR × $22.67 ADR = ~$14 stop).
- Tradable filter exemption for examples may distort scan statistics.
- `breach_bar` semantics differ by path (fade min = FW+1; breakout min = 1) but same field name.

### 9.2 Session-flagged
- HTF signal_exit top condition has floor_adr = −0.4; WIN-declaring exit loses money on ≥1 example (XPEV; Dan's read: needs two-stage exit, single-exit structurally limited). Accepted for now; future classifier BE rule scratches it.
- 3-4DB signal_exit top condition has floor_adr = −0.4 — new observation (2026-04-18) on a fade setup. Same bug pattern as HTF but mechanism not yet investigated.
- B18's max_fav overstating — envelope needs to honor stop.

### 9.3 Research blocking (from MFE_CAPTURE_PROJECT.md:228–232)
- Where deduped pre-classification signals live → resolved (see §5.2).
- `signal_exit_grinder.py --earnings-cap` hygiene → resolved (see §5.1).
- Which `signal_filter.py` functions (FW derivation, hard stop, race) can be imported vs need refactoring → open.

### 9.4 Next concrete step candidates
- Verify UNPROVEN entry-mechanic hypotheses per §13 (breakout: `high > signal_high`; fade: LSP breach-and-fail) before committing to v1. B11's `entry_close = argmax(close)` finding superseded — see §13.2.
- Run Phase 1 (`pyramid_grinder.py --blackout --scan-only-equivalent`) for HTF/BF/BASE to produce their `raw_signal_clusters_*.json` so they're auditable at all.
- Rebuild `signal_exit_grinder.py` to classifier-objective scoring — **SUPERSEDED** by §12.1a's "no upstream grinders" clause. Signal-exit search is absorbed into joint Phase 1 candidate search, not rebuilt as a separate artifact.

---

## 10. Workflow + hard constraints

### 10.1 From `CLAUDE.md` (main repo + worktree)
- No code without explicit "go/yes/do it."
- FULL STOP after presenting results/plans — no chaining.
- Never modify project `.md` files unprompted. Worktree `research/` scripts and docs are fair game.
- Read the spec before working on a component. `ta_knowledge.md` before TA work; `pcf.md` before PCF work; `DATA_CONTRACT.md` before format changes.
- Never dump large data into context — process via scripts.
- Read actual code before making claims.

### 10.2 Cache / write safety
- Worktree only for writes; main repo read-only.
- No junction links / symlinks to cache dirs.
- No `rmdir/rd/del/git worktree remove` on cache paths.
- Never `--force` a cache builder without presenting what it overwrites.
- Before any multi-ticker job: print cache path, count tickers, confirm ~11,200+; STOP if wrong.
- Before any write, verify resolved target path; if outside worktree → STOP.
- Railway is never a data source for OHLCV / expression cache.
- Never rely on `.append` for research or consensus runs.

### 10.3 Classifier spec (SIGNAL_FILTER.md anchors)
- Two paths: fade (DTSS, 3-4db) = ceiling + exit race; breakout (HTF, BF, BASE) = ADR thresholds.
- Signal-relative only for non-examples; no entry-bar reference.
- Strict `entry_idx − 1` example matching; failures are `GRINDER BUG`, not silent near-match.
- 120-bar max duration; timeout = WIN on breakout, held_to_end = WIN on fade.
- 100% example survival enforced via `is_example` override.
- Binary output (no AMBIGUOUS in current spec). Scratches count as wins. **NOTE:** SUPERSEDED. The 5-pile framework in §1 (WIN/LOSS/BE/AMBIGUOUS/NO_ENTRY, ratified 2026-04-18) overrides this binary-only constraint.
- Tie-break: stop wins on same-bar breach+exit.
- Fade ceiling: `max(cluster_highs ∪ FW_highs)` (shorts); mirror for longs. Intraday touch, not close.
- Breakout thresholds: `winner = max(entry_offset in ADR) + 1.0 + 0.1`; `loser = max(FW MAE in ADR) × 1.10`.
- `move_adr`: `entry_high` = actual entry candle high for examples; FW max/min for non-examples.

### 10.4 Signal + consensus spec (SIGNAL_GRINDER.md, CONSENSUS.md)
- 100% example pass rate enforced on signal grinds.
- 5% margin on bounding boxes (fixed); overridden to 0 for consensus grinding.
- Per-bar tradable filter: `close ≥ $1, dvol_20d ≥ $4M, 20-bar ADRP ≥ 1.8%`.
- Pre-filter 85% pass rate drop.
- Cross-source alignment uses DATES, never bar indices.
- NaN asymmetry: search treats NaN as pass; locked conditions / validation treat NaN as fail.
- Signal consensus requires z > 3.
- Refinement consensus: Meinshausen stability ∈ [0.6, 0.9] AND binomial p < 0.01.

### 10.5 Live data drift
Dan vets new examples continuously. DB example counts move between sessions. Methodology work tolerates this; specific numerical findings must be re-derived on current data.

---

## 11. Pointers

- Main repo spec docs: `../../swing-screener/` (read-only). Key: `SIGNAL_FILTER.md`, `SIGNAL_GRINDER.md`, `REFINEMENT_GRINDER.md`, `DATA_CONTRACT.md`, `DEPENDENCY_MAP.md`, `CONSENSUS.md`, `SWING_SCREENER_PROJECT.md`, `BUGS.md`.
- Dual-exit project log: `../../swing-screener-dual-exit/MFE_CAPTURE_PROJECT.md` — historical record + agreed framework (lines 216–234).
- Dual-exit research notes: `../../swing-screener-dual-exit/research/notes/` — earnings_cap_audit.md and deduped_pop_source.md, both condensed into §5 above.
- Shelved profit-grinder ideas: `../../swing-screener-dual-exit/archive/shelved_docs/PROFIT_GRINDER.md`.

---

## 12. Scoring function

Locks the classifier objective as a single function. Every candidate rule system is ranked by this function; no search step uses a different objective. Without this, "better / worse" is opinion.

### 12.1 Signature

    score(candidate, dataset) -> number in [0, 1]  OR  DISQUALIFIED

- `candidate` — a rule system, atomic: `(exit_rule, entry_inference_rule, BE_rule)`. Every component is part of the candidate. The 4-pile label for a cluster is **mechanically derived** from the forward tape given these three rules: stop breached → `LOSS` (original stop) or `BE` (ratcheted); exit fired with favorable fill → `WIN`; exit fired between stop and entry, or nothing fired in window → `AMBIGUOUS`. No separate classification-rule component is needed or searched — the if/and/or logic lives inside `entry_inference_rule` and `BE_rule`.
- `dataset` — discovery or confirmation half of a setup's deduped cluster pool, examples included. Per Model C decision (2026-04-18), the `is_example` override is retired; example correctness is enforced via the eligibility gate (§12.3) instead.
- Return — scalar where lower is better, OR `DISQUALIFIED` if any Tier 1 gate trips.

### 12.1a No upstream grinders

The candidate is atomic. No component (exit, entry, BE) is produced by a separate search step with its own objective. The scoring function defined here is the sole objective of candidate search.

`signal_exit_grinder.py` in its current form optimizes a different objective and **cannot** be used to supply candidate exit rules. Any reuse of its code is mechanical (enumeration, threshold sweeps) — never its ranking output.

Consequence: §9.4's "Rebuild `signal_exit_grinder.py` to classifier-objective scoring" is misphrased. Signal-exit search gets absorbed into joint candidate search, not rebuilt as a separate artifact. §9.4 to be reconciled in a later pass.

### 12.2 Fixed inputs (not searched)

Structural, from §2. Any candidate that violates one is malformed, not DISQUALIFIED:

- Stop placement = extreme of inferred entry candle, capped at 1 × ADR(14) (§2 c4, c5).
- Loss magnitude = flat 1 ADR (§2 c7).
- Exit timing = close of forward bars; fill intraday on trigger bar (§2 c8).
- Forward window = `min(earnings_date − 1, 120 bars)` (§2 c9).
- Rule form = boolean only, no continuous scoring (§2 c10).

### 12.3 Tier 1 — Eligibility gate (binary)

Any failure → `DISQUALIFIED`. No partial credit.

- **T1.1 — Example WIN survival (no override).** Every example must classify as `WIN` by the candidate rules independently.
- **T1.2 — Example no-scratch.** No example may classify as `BE`. If the candidate's BE ratchet would move an example's stop and that moved stop was hit before exit fired, disqualified. Examples pin the BE ratchet — too early and real winners scratch, too late and the BE pile never forms.
- **T1.3 — LOO example survival.** For each example `e`, remove `e` temporarily and confirm it still classifies as `WIN` under the same rule system. Any flip to non-WIN → disqualified.
- **T1.4 — Internal consistency on WIN pile.** No `WIN`-labeled cluster may have its (original or BE-ratcheted) stop breached intraday before the exit fired on a later bar's close. Same-bar stop+exit resolves to stop (§10.3 tie-break).
- **T1.5 — AMBIGUOUS definition respected.** A cluster gets `AMBIGUOUS` iff (a) neither stop nor exit fires within the forward window, OR (b) the exit fires at a fill price between the stop level and the inferred entry price (losing but not stop-level, per §1). Exact only.
- **T1.6 — §2 conformance.** Candidate's stop placement, loss sizing, evaluation timing, and window capping match §2.

### 12.4 Tier 2 — Primary metric (minimize)

**P.1 — Ambiguous rate.**

    p_ambiguous = |AMBIGUOUS clusters| / |pool|

Lower = more of the pool is confidently labeled → better EV-quotation accuracy on the watchlist (the project's overarching goal — see SWING_SCREENER_PROJECT.md). Computed on the dataset as-supplied, examples included.

### 12.5 Tier 3 — Regularizers (tie-breakers)

Used only when Tier 2 ties within tolerance (§12.8).

- **R.1 — LOO stability.** Hold out each example, recompute `p_ambiguous`, report std across runs. Lower = less sensitive to any single labeled winner.
- **R.2 — Alt-seed stability.** Bootstrap-resample non-example portion (20 resamples at 80% size), compute `p_ambiguous` on each, report std.
- **R.3 — WIN-pile WR plausibility.** Measure realized WR on WIN pile's non-example members. Penalize linearly outside the plausible band per setup class. Bands derived in Phase 0 (§12.8). **Disabled until Phase 0 runs.**

### 12.6 Ordering

Strict lexicographic:

1. Tier 1 — any gate fails → DISQUALIFIED.
2. Tier 2 — rank by `p_ambiguous` ascending.
3. Tier 3 — break ties (within tolerance) by `R.1 + R.2 + R.3` equal-weighted.

Lexicographic prevents regularizers from overruling a worse primary.

### 12.7 What the function does NOT do

- Does not search. Scores one candidate against one dataset.
- Does not choose the holdout split.
- Does not re-derive thresholds inside a candidate.
- Does not log, write, or mutate state.
- Does not silently handle malformed candidates — structural issues raise an error, not `DISQUALIFIED`.

### 12.8 Phase 0 — prerequisite derivations

Two quantities referenced above are not yet data-derived. One-time script, not eyeball:

- **Phase 0a — Plausible WR bands per setup class.** Derived from examples' realized R-multiples under a simple reference (exit, entry, BE) triple. Output: two bands (breakout vs fade) as `[WR_low, WR_high]`. Stored version-controlled JSON in `research/`.
- **Phase 0b — Tolerance for Tier 2 ties.** Proposed `0.005`. Tighten if candidates cluster densely.

Until 0a runs, R.3 is disabled.

### 12.9 Open design parameters (RESOLVED 2026-04-18)

- **O.1 — Holdout cutoff date.** LOCKED per-setup via change-point detection on monthly signal-rate timeseries (binary segmentation + permutation test, α=0.05, `research/o1_changepoint.py`):
  - HTF: `2023-11-01`
  - BF: `2025-06-01`
  - BASE: `2024-02-01`
  - DTSS: LOO-only (no significant shift detected)
  - 3-4DB: LOO-only (no significant shift detected — new setup, most data post-2024)
- **O.2 — Tier 2 tolerance.** LOCKED at `1/pool_size` per setup (native single-cluster-flip resolution of the metric):
  - HTF: 0.00203 (pool 493)
  - BF: 0.00227 (pool 440)
  - BASE: 0.00186 (pool 538)
  - DTSS: 0.00090 (pool 1108)
- **O.3 — R.1/R.2/R.3 weighting.** DEFERRED to post-first-iteration recalibration. V1 uses equal weights (min-assumption default). Post-first-iteration, recalibrate by inverse-variance on σ_R1 and σ_R2 measured on real top candidates.
- **O.4 — Alt-seed resample count.** DEFERRED. V1 uses math-based worst-case bound derived from `σ_max / sqrt(2(N-1)) < O.2 tolerance` with `σ_max = 0.5` (proportion upper bound). Re-derive from measured σ post-first-iteration.

**Why O.3 and O.4 are deferred:** measuring σ requires scoring a fixed candidate. Picking a fixed candidate requires knowing σ for robustness assessment. Circular. Break the loop by using worst-case bounds in v1, then recalibrate on actual top candidates produced by Phase 1 iteration.

### 12.10 Exit rule shape

- `exit_rule` = set of one-or-more `(expression, op, threshold)` atoms combined with **OR** only.
- Exit fires on close of first forward bar where any atom is satisfied.
- No AND on the exit rule — exits fire eagerly when any trigger appears.
- Depth cap: **2 atoms max** per exit rule for v1.

XPEV on HTF is the motivating case: the current single-expression exit scratches this known winner, which T1.2 correctly rejects. A compound blowoff-top + loss-of-momentum exit is the proposed remedy; joint search discovers it (or something equivalent) rather than having it hand-prescribed.

### 12.11 Implementation dependencies (code, not design)

Modules the scoring function will call when implemented. None exist yet in the worktree:

- **Labeler** — per-cluster classifier. `(cluster, forward_data, candidate) → label ∈ {WIN, LOSS, BE, AMBIGUOUS}`. Will live in `research/labeler.py`.
- **Forward-data loader** — OHLCV slice from rightmost-bar+1 through window end. Reads `universe_ohlcv_daily.pkl` read-only from main repo cache.
- **Earnings-window resolver** — searchsorted pattern from §5.1.

### 12.12 Invariants

- Deterministic for a given `(candidate, dataset)`. No RNG in labeler or scorer (alt-seed RNG lives in caller).
- Pure: no side effects.
- Total: every cluster receives a label. NO_ENTRY is the structural catch-all for "signal without trade"; AMBIGUOUS is the outcome catch-all for "trade with unresolved outcome."

---

## 13. Entry mechanic derivation (Phase 0)

The entry-inference rule is a per-setup-class fixed input to Phase 1 (classifier iteration). Derived in Phase 0; not part of the §12 atomic candidate. §12.1a's "no upstream grinders" clause applies to search with its own objective — Phase 0 is a factual mechanic derivation, not a scored search, so not in conflict.

### 13.1 UNPROVEN hypotheses (as of 2026-04-18)

The following entry mechanics are under consideration. **Neither is verified for full-coverage on the current example set.** DO NOT commit as v1 rules until verification runs confirm coverage:

- **Breakout entry (HTF / BF / BASE) — HYPOTHESIS, UNPROVEN.**
  - Proposed rule: first forward bar within the entry-discovery window where `high > signal_bar high`.
  - Preliminary test (`research/entry_mechanic_test.py`, 2026-04-18): 117/119 (98.3%) match at signal+1. Two exceptions first-matching at offset 2: IREN 2025-06-23 (BF), LMND 2024-11-05 (HTF).
  - Full-window coverage (offset > 1) not yet tested.
- **Fade entry (DTSS / 3-4DB) — HYPOTHESIS, UNPROVEN.**
  - Proposed rule: first forward bar within the entry-discovery window where `high > lsp_above1 AND close < lsp_above1` (breach-and-fail of nearest LSP above).
  - Uses existing algorithmic LSP features (80 in the 16k vocabulary, computed via `scripts/lsp_detector_v2.py`).
  - **NOT YET TESTED** against DTSS or 3-4DB examples.

Stop placement under either mechanic: inferred entry bar's against-trade extreme (high for shorts, low for longs), capped at 1 × ADR(14) per §2 c5.

### 13.2 Approaches shelved (2026-04-18)

- **Feature-vector entry signature** computed on entry-bar features from `expr_series`. Shelved because close-based features at the entry bar are contaminated by the bar's outcome — a same-day-loss entry's close features reflect the post-reversal state, not the entry-moment state. Close-exclusion feature filtering (restrict to signal-bar features + entry-bar OHLV-without-close) considered but too complex for v1.
- **`entry_close = max(close over [leftmost, entry_idx])` pattern** (previously attributed to B11). Shelved because the range definition references `entry_idx`, violating §2 c1. Empirically tautological — always true for examples by labeling construction, unusable on non-examples.

### 13.3 Remaining work before Phase 1 can begin

1. Verify breakout hypothesis (§13.1) at full-window coverage. Understand the IREN / LMND edge cases.
2. Test fade hypothesis (§13.1) coverage against DTSS + 3-4DB examples using LSP features.
3. Derive exact entry-discovery window size (bars) per setup class. Not derivable from examples alone — all examples have entry = signal+1 by DB convention, so examples are silent on window size. Candidates: Dan's trading design (setup-level constant), peak-offset distribution on wild signals, or joint search as part of Phase 1.
4. If either hypothesis fails coverage, revisit alternatives: programmatic AVWAP computation for breakouts (Dan described the actual breakout trigger as "break of highest contextual AVWAP" — not in feature library; would need to be built from existing pivot detector), simpler OHLC fallbacks, or iterative derivation via Phase 1 feedback.

---

## 14. Dataset separation — examples vs tradable_entries

### 14.1 `examples` table (existing, authoritative for Phase 1 T1 gates)

- 219 rows as of 2026-04-18. A+ winners only, discretionary labeling by Dan.
- Schema: `(setup_type, ticker, chart_date, entry_date, created_at)`, unique on `(setup_type, ticker, entry_date)`.
- Used for Phase 1 classifier T1 gates (T1.1 example WIN survival, T1.3 LOO). Every example must classify as WIN under a candidate rule system.
- Goal of the classifier: catch all of them as WIN while catching less of everything else.

### 14.2 `tradable_entries` table (future asset, not v1)

- Planned — not yet built.
- Will contain every entry Dan actually took, regardless of outcome. Proposed schema: `(setup_type, ticker, entry_date, outcome ∈ {WIN, LOSS, BE})`.
- Populated over time via continued discretionary labeling.
- Kept distinct from `examples` so losers never flow through T1 gates (would disqualify all candidates since T1.1 requires every row to classify as WIN).

### 14.3 Downstream uses (future)

- **Phase 0 entry-mechanic verification.** Entry mechanic must recognize 100% of `tradable_entries` as valid entry bars. Stronger validation than just the A+ subset, since `tradable_entries` includes takeable entries Dan would have entered even though they lost.
- **Refinement grinder negative class.** Losing `tradable_entries` provide a sharper negative class than "all non-examples" — refinement conditions can discriminate A+ winners from demonstrable losers, rather than winners from noise. Strengthens the consensus-engine signal.
- **Future T1-style gate.** Known-loser entries must classify as LOSS or BE under a candidate, not WIN — extra overfit protection beyond current T1.

### 14.4 Known limitation — same-day loss contamination

Entries that stopped out intraday on the entry bar have close-based features at that bar that reflect the post-reversal state, not the entry-moment state. This affects any close-feature-based entry-identification approach (see §13.2). OHLC-pattern mechanics (§13.1) are intrinsically unaffected — the trigger fires intraday on OHL alone, close direction doesn't matter for entry identification. When `tradable_entries` is built, same-day losers remain useful for refinement-grinder negative class (which compares signal-bar features, not entry-bar close features).

---

## 15. Labeler mechanic (revised 2026-04-21)

Deterministic pure function producing one of `{WIN, LOSS, NO_ENTRY}` per cluster. No BE, no AMBIGUOUS — those collapsed per 2026-04-21 scope decision (BE is profit_grinder scope; AMBIGUOUS removed because with an aggregate-profit-trained exit rule, every trade resolves to a specific close-based P&L that's either above or below effective entry).

### 15.1 Signature

    labeler(cluster_meta, forward_tape, exit_rule) → {final_label, exit_bar_offset, exit_close, realized_pnl_adr}

- `cluster_meta`: `{ticker, signal_bar_idx, is_example, adr14_at_entry, direction, entry_candle OHLC, earnings_cap_offset}`
- `forward_tape`: OHLC rows from entry bar forward through earnings cap.
- `exit_rule`: `{expression, direction (op), threshold}` — the top rule from the signal exit pool grinder (§3.5 / step 3 of the build plan).
- `final_label ∈ {WIN, LOSS, NO_ENTRY}`

Pure. Deterministic. No I/O.

### 15.2 Entry convention

**Examples.** `signal_bar_idx = entry_idx − 1` by DB construction. Examples are double-positive (presignal-forced + pyramid-trained) at E−1.

**Wild.** `signal_bar_idx` = pyramid-fire bar. `entry_bar_idx` derived by the entry-discovery rule (§15.3).

### 15.3 Entry discovery

**Breakouts (HTF, BF, BASE):** use the §17.7 AND-gate entry mechanic. Forward window `W = 11 bars`. Entry bar = first k ∈ [1, 11] where `close[sig+k] > close[sig] AND close[sig+k] > resistance_AVWAP`. Resistance AVWAP = argmax over anchor A ∈ [max(0, sig-200), sig-1] of AVWAP(A..sig), volume-weighted, tp=(h+l+c)/3. Adaptive lookback for recent-IPO cases. Support MA + foothold checks from old §16.2/§16.3 are NOT required. None in window → **NO_ENTRY**.

**Fades (DTSS, 3-4DB):** OUT OF SCOPE for this iteration. Fade entry mechanic is structurally different (entry bar has high > sig_high with rejection close). Separate spec needed.

Supersedes the old bare "close > sig_close" mechanic in this section. Reference implementation: `research/classifier_tag_emit.py`.

### 15.4 Stop + effective entry

Per §2 c5 (corrected interpretation):

**Longs:**
- `stop = entry_low` (the entry bar's low; fixed, never moves up inside bar).
- `effective_entry = min(entry_high, entry_low + 1·ADR14_at_entry)`.
- Rationale: for narrow bars (range ≤ 1 ADR), effective_entry = entry_high (pessimistic intraday fill). For wide bars (range > 1 ADR), effective_entry pulled down to `entry_low + 1·ADR` so entry-to-stop risk is capped at exactly 1 ADR.

**Shorts:** symmetric. `stop = entry_high`; `effective_entry = max(entry_low, entry_high − 1·ADR14)`.

### 15.5 Forward race + label

Walk bars k from entry+1 through `min(earnings_bar − 1, entry_bar + 120, last_bar)`. First event in sequence:

1. **Stop hit** (longs: `low[k] ≤ stop`; shorts: `high[k] ≥ stop`) → **LOSS**. Realized P&L = **−1 ADR** (flat per §2 c7). Return.
2. **Exit rule fires** at bar k's close (apply `direction(close[k], threshold)`) →
   - Compute `realized_pnl_adr = (close[k] − effective_entry) / ADR14_at_entry` for longs; sign-flipped for shorts.
   - `realized_pnl_adr > 0` → **WIN**. `realized_pnl_adr < 0` → **LOSS** (realized loss smaller than 1 ADR — still labeled LOSS). `realized_pnl_adr = 0` → **LOSS** (scratch treated as loss under hard W/L).
   - Return.

If loop ends with no stop hit and no exit fire (forced exit at cap bar):

- Compute `realized_pnl_adr` from cap bar's close using the same formula.
- `realized_pnl_adr > 0` → **WIN**; `≤ 0` → **LOSS**.

### 15.6 Signal exit rule source

The `exit_rule` input is the top condition from `data/signal_exit_grind/signal_exit_pool_{setup}.json` — output of the **aggregate-profit signal exit pool grinder** (step 3 of build plan).

That grinder trains on the 262-cluster combined pool (examples + wild) with the objective of maximizing aggregate realized P&L in ADR across all entered trades. See plan document for full spec.

The older per-example fit rule in `signal_exit_{setup}.json` is not used by this labeler.

### 15.7 Fade treatment

Direction-symmetric per §15.4–15.5 shorts notes. Not yet validated for DTSS / 3-4DB under this revised mechanic. Prior fade attempts have failed 2×; the aggregate-profit objective may or may not change that — validate before shipping fades.

### 15.8 Daily-data limitations (acknowledged)

- **Same-bar intraday stop-outs.** Undetectable from daily OHLC.
- **Intraday fill price unknown.** Handled by pessimistic `effective_entry` construction (§15.4).
- **Earnings cap.** Next earnings date − 1 trading day, fallback to `min(entry + 120, last OHLCV bar)`.

### 15.9 Implementation status

**Built (2026-04-23):**
- `research/classifier_tag_emit.py` — per-signal TAKE/SKIP-family tag emission under §17.7. Produces `research/classifier_tags/{setup}_tags.json` for breakouts. 100% example ENTRY, 9-17% wild carve.

**Built (2026-04-26 — bakeoff resolved 2026-04-25):**
- `scripts/signal_exit_pool_grinder.py` — **L14 labeler.** Per-setup `mfe_during_life >= T_setup` where `T_setup = min(example mfe_during_life)`. Lock by construction. No exit rule selected (separated from labeler scope per the EV-after-Profit reorder; realized P&L is profit grinder's job downstream). HTF: 28/28 lock, T=2.376, 38.6% wild WIN. BF: 45/45 lock, T=2.376, 42.4%. BASE: 38/38 lock, T=2.886, 42.3%. Bakeoff write-up + ranking in `SIGNAL_EXIT_GRINDER.md §Pending research`.

**Modules pending:**
- `research/labeler.py` — pure function per §15.1. Now trivially derived from the L14 mechanic; consumes `signal_exit_pool_{setup}.json` and re-emits per-cluster `final_label` if needed alongside per-signal features.
- `research/classify_pool.py` — driver applying labeler to a pool; writes `research/labeler/{setup}_final_labels.json`.

Label output schema per cluster (from `signal_exit_pool_{setup}.json`):
`{cluster_id, ticker, is_example, tag, signal_bar_idx, status, reason, entry_k, entry_bar, entry_date, cap_bar, cap_date, cap_cause, horizon, eff_horizon, adr14_at_entry, effective_entry, stop, stop_hit_bar, mfe_during_life, mfe_full_window, final_label}`.

Downstream consumers:
- **Refinement grinder** reads `final_label` and the pool cluster list.
- **EV grinder** reads realized P&L per cluster — but NOT from the labeler. Realized P&L comes from profit grinder under the EV-after-Profit reorder. Until the reorder lands, EV operates on the legacy raw_signal_clusters `move_adr` field.

---

## 16. Entry detector — locked 2026-04-22

The full entry detector (resistance + support + foothold + entry trigger) for breakout setups (HTF, BF, BASE). Same rule applied to every chart; setup-specific behavior emerges from auto-detection, not from setup branching.

### 16.1 Inputs per signal bar

For a candidate signal bar at index `sig` (must satisfy pyramid ∩ presignal double-signal):
- OHLCV history through `sig`
- `sig_close = close[sig]`, `sig_low = low[sig]`
- `ADR(14) = mean(high[sig-13..sig] − low[sig-13..sig])`

### 16.2 Support detection (auto)

MA candidate set (Dan-restricted, 2026-04-22):
```
SMA50, SMA100, SMA200, EMA3, EMA8, EMA21
```

For each candidate, compute `signed = (sig_close − MA(sig)) / ADR`. Keep candidates where `|signed| ≤ T_LSO = 2.0 ADR`. Among qualifying, pick the **longest period**; tie-break by smaller `|signed|`. That is the auto-detected support MA, with period `N_lso`.

If no MA qualifies: signal is invalid (no foothold support in range), filter to NO_ENTRY.

### 16.3 Foothold check

```
foothold = (sig_low − MA_lso(sig)) / ADR
```

Pass if `|foothold| ≤ T_FOOTHOLD = 1.856 ADR`. Derived from the worst-case example (LMND 2024-11-05 textbook entry, EMA8 support, foothold 1.856).

If foothold > T_FOOTHOLD: support too far below sig → "needs more sideways" → no entry on this bar.

Sig poking below MA (negative foothold) is allowed up to the same threshold.

### 16.4 Resistance anchor

Lookback window: `N_lso` bars (= the support MA's period).

```
anchor_A = argmax over A in [sig − N_lso, sig − 1] of AVWAP(A..sig)
resistance_AVWAP = AVWAP(anchor_A..sig)
```

where `AVWAP(A..sig) = sum(tp[k] · v[k] for k in [A, sig]) / sum(v[k] for k in [A, sig])`, `tp = (h+l+c)/3`.

This is "the bar in the lookback window that produces the highest overhead AVWAP." Picks the structurally most-overhead reachable resistance at this lookback scale.

### 16.5 Entry candle (AND-gate)

Walk forward `k = 1..10` from sig. Entry candle = the **first** k satisfying BOTH:
1. `close[sig+k] > close[sig]` (basic breakout above signal)
2. `close[sig+k] > resistance_AVWAP` (closes above the overhead AVWAP)

If no k in [1, 10] satisfies both → NO_ENTRY.

Stop and effective-entry follow §2 c5 (entry_low fixed; effective_entry capped to entry_low + 1·ADR).

### 16.6 Test results vs labeled examples (2026-04-22)

After DB cleanup (PTON entry corrected from 2024-10-14 → 2024-10-11; LMND 2024-11-06 chase removed; HTT 2021-01-12 removed as divergence pivot):
- T1.1 = 110/110 examples pass (1 IPO-era excluded due to insufficient ADR history).
- Anchor match vs Dan's hand-picks (n=7 unique cases): 5 exact, 7/7 within 5 bars, mean abs bars off = 0.6, median AVWAP ratio = 1.001.

### 16.7 Wild filter behavior on dedup pool

Applied to 992 wild deduplicated double signals (HTF 219 + BF 308 + BASE 465):
- Mature & ready: 986 (99.4%)
- Needs more sideways (foothold fail): 6 (0.6%)

The foothold filter is loose — most wild signals already have foothold met by the time pyramid + presignal both fire. Foothold rejection adds 0.6% rejection on top of the existing pyramid+presignal filters.

The bigger filter is the entry AND-gate (close[entry] > resistance_AVWAP), which determines whether the breakout actually clears overhead. Not yet measured on wild — pending.

### 16.8 Auto-detected MA distribution by setup (sanity check)

Across the cleaned 110 examples:
- **HTF** (n=27): EMA21 44%, EMA8 44%, SMA200 7%, SMA50 4% — short EMAs dominant ✓
- **BF** (n=45): EMA21 73%, EMA8 16%, SMA50 7%, SMA200 4% — EMA21 dominant ✓
- **BASE** (n=38): SMA50 40%, SMA100 26%, EMA21 24%, SMA200 11% — longer MAs dominant ✓

Distribution emerges from the auto-detection without setup branching. Matches Dan's mental model (HTF=fast, BF=EMA21, BASE=SMA50).

### 16.9 Reference scripts

- `research/h11_argmax_avwap_in_window.py` — locked rule test (T1.1 + Dan-pick comparison).
- `research/h12_foothold_low_based.py` — foothold threshold derivation.
- `research/h13_wild_filter.py` — wild dedup pool filter test.

### 16.10 Implementation status (open work)

- Entry detector locked. Ready to wire into the labeler (§15) as the deterministic entry-candle rule (replacing the current §9 "first close above sig_close" mechanic with the AND-gate).
- DB cleanup complete (PTON, LMND, HTT changes committed to `examples` table). Pyramid + presignal regrind needed downstream because pool counts shifted.
- W/L classification logic (§15) needs robustness work — that's the next session's mandate. Refinement grinder + EV grinder consume the W/L output and need clean labels to do quality work.

---

## 17. In progress (append-here, update-in-place)

Running log of exploratory work between locked spec sections. Delete entries once they land in the spec proper or are definitively abandoned.

### 17.1 OHLCV cache rebuilt 2026-04-23

Distribution-adjustment bug (dividend-adjusted OHLC for ETFs / distribution-paying tickers) fixed. Rebuilt cache: 11,859 tickers. T1.1 still 110/110 on the §16.5 AND-gate rule. Point checks:
- DRN SMA200 distance at sig: 1.36 ADR (biased cache was 1.73 ADR).
- QUBT 2024-11-19 EMA8 distance: 0.205 ADR (sig_low touches EMA8 at foothold 0.004).

### 17.2 Single-bar classifier-stage filter — explored and deprioritized 2026-04-23

Question: can a classifier-stage filter on signal-bar features separate "ready" from "immature" wild signals on top of pyramid + presignal output?

Tried (scripts `research/h14_*.py` through `research/h24_*.py`, keep for reference):
- Corridor width = (resistance_AVWAP − support_MA)/ADR per-setup bounding box (§16-derived).
- Support MA selection sweep: longest-period-within-cap vs tightest-foothold. Hit DRN (needs SMA200 / 200-bar lookback) vs QUBT (needs EMA8 / 8-bar lookback) paradox — coupling support period with AVWAP lookback can't serve both.
- Hard constraint: sig_close < correct_resistance_AVWAP. 31/110 examples violate under 200-bar lookback, all at/near ATH. Unresolved whether indexing is off-by-one or a new setup class ("FTP — flat top breakout") is present.
- Resistance-free approach: per-setup bounding box on `|sig_close − MA|/ADR` for {EMA3, EMA8, EMA21, SMA50, SMA200, SMA330} with IQR-based cushion (k=0.25). Signed variant (retains above/below-MA info) also built.
- Relational features: MA ordering, pairwise gaps, stack compression, 5-bar MA slopes, bar shape, support-touch counts, bars-above streaks. Per-setup patterns visible (HTF has +2.19 ADR EMA3 slope vs BF flat; BASE has −0.33 EMA3-SMA200 correlation unique among setups).
- Joint multivariate: Mahalanobis per setup using pairwise-complete covariance (no examples dropped for missing MAs); PCA; correlation-conditional linear ridges (tightest: BASE EMA8 ≈ 1.49·EMA3 + 0.08, residual std 0.15 ADR).

Wild pool application (h24, 989 wild signals):
- HTF envelope (max Mahalanobis 3.50) rejects 10.1% of HTF wild as outside — discriminating.
- BF envelope (max 6.24, QXO-set) rejects 0% — QXO's extreme-but-legit Mahalanobis admits every wild.
- BASE envelope (max 5.61, QXO-set) same — 0% rejection.
- Nearest-setup centroid classification: 79-86% of wild match their labeled setup. Worth keeping as a setup-label tool but not as a readiness filter.

Conclusion: **single-bar features at signal bar do not discriminate wild from examples once pyramid + presignal have pre-filtered.** Each scalar feature is grindable by pyramid; joint envelopes are dominated by legitimate outliers. Any single-bar classifier-stage filter here is redundant.

### 17.3 Reframing the classifier task (Dan 2026-04-23)

It's *possible* the ~1,100-cluster deduped breakout pool is all real setups — rough pass-through guess ~600 losers / ~200 NO_ENTRY / ~300 winners (with big winners a subset of the 300). Not confirmed; worth exploring. If the pool really is all-real, the classifier task is **outcome prediction, not structural filtering**.

### 17.4 Next-session candidate directions (Dan's "decent ideas")

- **(A) Temporal / multi-bar features pyramid doesn't cache.** MA stack trajectory over N bars (convergence rate, compression over time, slope-of-slope). Captures "coiling as a process," which single-bar features miss. Requires new feature computations, not cache grinds.
- **(D) Forward-outcome labeling.** Skip the pre-filter question. Run labeler.py + aggregate-profit signal exit grinder on the whole pool per `plans/proud-hatching-puddle.md` steps 2-3. Downstream refinement / EV grinder carves on realized P&L, not signal-bar geometry.

Still in consideration (less likely to go first):
- **(B) K-NN to labeled examples** — for each wild find nearest-N examples, inherit label + winner/loser similarity rather than pass/fail.
- **(C) Robust covariance (Minimum Covariance Determinant)** — mathematically down-weights outliers like QXO. Same single-bar features — likely still redundant with pyramid.

### 17.5 Items to clean / resolve

- QXO 2025-06-06 SMA330 = −36.67 ADR confirmed accurate by Dan (not a cache bug) — understand the market context when time permits.
- 31 "sig_close > AVWAP-in-200-bar-lookback" examples — indexing off-by-one vs FTP sub-class hypothesis is unresolved. Revisit only if it becomes blocking.
- ~~§16 T_LSO = 2.0 ADR cap for support-MA selection is hand-picked from DRN's 1.73 ADR case.~~ **Resolved 2026-04-23 (§17.7):** support-MA auto-detection dropped entirely. Fixed 200-bar adaptive AVWAP lookback replaces the N_lso coupling.

### 17.6 Pending open work (folded in from deleted NEXT_SESSION_PROMPT.md)

Still on the plate regardless of which direction (A–D from §17.4) we take for classifier-stage filtering:

1. Wire R3 v2 entry detector (§16) into the labeler (§15). Replaces the current §9 "first close above sig_close" mechanic with the §16 AND-gate. Re-test that all 111 examples produce a valid entry under the locked rule.
2. Run the aggregate-profit signal exit grinder per setup (built in separate session on `signal-exit-pool` worktree). Verify output at `data/signal_exit_grind/signal_exit_pool_{setup}.json`. Read top condition per setup.
3. Apply the labeler to all 111 examples + ~1,100 wild signals. Produce per cluster: entry_k or NO_ENTRY, effective_entry, stop, exit_bar_offset, realized_pnl_adr, final_label (WIN / LOSS / NO_ENTRY).
4. Verify W/L sanity — all 111 labeled examples should classify WIN. Any failures decide rule adjustment vs example reclassification (DB-cleanup precedent: edge cases often mislabels).
5. Wild W/L ratio should be plausible (not 100% WIN — would indicate measurement error).
6. Hand clean labels to refinement_grinder + ev_grinder.

Downstream dependency: pyramid + presignal regrind required after the 2026-04-22 DB cleanup (PTON date correction, LMND 2024-11-06 / HTT 2021-01-12 removals) because pool counts shifted slightly. Don't run without explicit go.

### 17.7 Classifier entry-tag mechanic — locked 2026-04-23 (breakouts only)

Classifier's output unit is the individual double signal (pre-dedup, per-firing), reaffirmed per `feedback_classifier_pool_stays_per_signal.md`. Classifier labels each signal with a TAKE/SKIP-family tag; downstream grinders (signal_exit_pool_grinder, refinement_grinder, ev_grinder) consume the tags plus per-signal features.

**Tags:** ENTRY / REDUNDANT / NOENTRY / MISSING.

**Entry mechanic (locked):**
- **Resistance AVWAP** — argmax over `A ∈ [max(0, sig_idx − 200), sig_idx − 1]` of `AVWAP(A..sig_idx)`, using `tp = (h+l+c)/3`, volume-weighted. Adaptive: if `sig_idx < 200` (recent IPOs), uses `min(200, sig_idx)` bars.
- **Entry trigger** (AND-gate): `close[sig + k] > sig_close AND close[sig + k] > resistance_AVWAP`.
- **Forward window** `W = 11 bars`. Data-derived from max observed same-ticker sibling-to-entry gap across breakout labeled examples (BF's 10-bar max + 1). Fade-tail signals (DTSS PRKS 70-bar gap) trimmed at the natural kneedle break between gap 10 and gap 13.
- **Support MA and foothold (§16.2 + §16.3): DROPPED for this job.** Not required to identify entry candles or tag signals — only the resistance AVWAP anchor is. §16.2's T_LSO derivation was circular and one-example anchored; no clean data-derivation was found.

**Tag definitions:**
- `ENTRY` — AND-gate fires in `[1, W]` and no other double signal on same ticker falls strictly between sig and entry bar.
- `REDUNDANT` — AND-gate fires but another same-ticker double signal sits between sig and entry (the later signal is the "real" one, this fired too early).
- `NOENTRY` — AND-gate never fires within W forward bars (setup never confirmed).
- `MISSING` — ticker absent from current OHLCV cache (3 wild rows across HTF/BF/BASE: EXAS/PSTG/NGD — pool built 2026-04-21 pre-cache-rebuild).

**Results:**
- **Examples: 113/113 ENTRY (100%).** HTF 30/30, BF 45/45, BASE 38/38.
- **Wild carve:** HTF 12% REDUNDANT+NOENTRY, BF 17%, BASE 9%. Remaining 83-91% of wild tagged ENTRY — carried forward to refinement_grinder + signal_exit_pool_grinder.

**Scope:** breakouts only (HTF, BF, BASE). Fade mechanic is structurally different (see `feedback_classifier_breakout_scope.md`) — out of scope for this iteration.

**Implementation:** `research/classifier_tag_emit.py`. Output: `research/classifier_tags/{setup}_tags.json` per setup.

Per-cluster schema (tag fields + auto-emitted per-signal features):
- Tag fields: `cluster_id, ticker, sig_idx, sig_date, sig_close, is_example, tag, entry_k, entry_idx, entry_date, anchor_idx, anchor_date, resistance_avwap, adr_at_sig`.
- Run structure: `run_length, run_position (singleton/first/middle/last), run_index_from_left, run_index_from_right`.
- Lead-up density: `lead_up_density_50, lead_up_density_200` (prior same-ticker pool signal counts in W=50 and W=200 windows).
- Prior-signal gap: `bars_since_prior` (bars to nearest prior same-ticker pool signal, null if none).
- Pre-entry drawdown: `min_low_offset_adr` — signed min-low offset from sig_close in ADR during (sig, entry) bars. Negative = drawdown (MAE); positive = lows stayed above sig_close; 0.0 when entry_k==1.

Features emit automatically on every classification run — no separate step.

**Downstream consumers:**
- `signal_exit_pool_grinder.py` — ingests ENTRY-tagged rows, finds exit expression maximizing aggregate P&L.
- `refinement_grinder` — ingests tag + per-signal features, learns within-cluster picker (prefer signals closest to actual entry, drop REDUNDANT siblings) and across-cluster filter (eliminate losing clusters).
- `ev_grinder` — ingests tag + realized_pnl_adr per signal (after signal_exit_pool_grinder fires), learns feature-conditional expectancy.

---
