# SIGNAL_EXIT_GRINDER.md

Authoritative spec for the signal exit grinder family — the components that produce per-signal WIN/LOSS labels (and, for the legacy path, an exit rule) for a setup's signal population.

**Scripts:**
- `scripts/signal_exit_pool_grinder.py` — **L14 labeler.** Current. Per-signal WIN/LOSS labels for the breakout classifier path (HTF, BF, BASE).
- `scripts/signal_exit_grinder.py` — **legacy per-example exit-rule fit.** Still in use upstream of `pyramid_grinder` cluster gathering, `signal_filter`, `entry_grinder`, and the UI until the pipeline reorder lands.

This file describes intent and behavior; code is the source of truth for implementation details (no line numbers — they go stale).

---

## Purpose

For each ENTRY-tagged signal in the breakout classifier pool (HTF, BF, BASE), attribute a **WIN** or **LOSS** label that reflects whether the entry produced a real-winner profit zone during the trade's lifetime. The label is the labeler's only output — exit-rule selection and realized P&L attribution are downstream concerns (handled by profit grinder under the post-reorder pipeline; see DEPENDENCY_MAP.md).

The legacy per-example exit-rule fit (`signal_exit_grinder.py`) serves a separate role: producing the exit-rule expression that pyramid cluster gathering, signal_filter, entry_grinder, and the UI still consume. It is not a labeler — it picks an exit rule by example fit, then downstream consumers race that rule to classify clusters.

---

## EXACT spec — `scripts/signal_exit_pool_grinder.py` (L14 labeler)

Per setup ∈ {htf, bf, base}, per ENTRY-tagged cluster from `<classifier worktree>/research/classifier_tags/{setup}_tags.json`:

1. **Cluster meta.** Per §2 c5/c7/c9a:
   - ADR14 recomputed at `entry_idx` from OHLCV.
   - `effective_entry = min(entry_high, entry_low + 1·ADR14)` (longs).
   - `stop = entry_low`.
   - `cap_bar = min(entry+120, end_of_tape, earnings_bar−1)`.
   - `stop_hit_bar` = first j where `low[entry+1+j] ≤ stop`, else `horizon`.
   - `eff_horizon = min(stop_hit_bar+1, horizon)` — the trade's lifetime in forward bars.
2. **Forward feature.**
   - `mfe_during_life = (max(high[entry+1 .. entry+eff_horizon]) − effective_entry) / ADR14`.
   - `mfe_full_window = (max(high[entry+1 .. cap_bar]) − effective_entry) / ADR14` — diagnostic; the labeler does not use it. Preserved for downstream consumers that want the full forward-window MFE.
3. **Threshold.** `T = min_{ENTERED examples} mfe_during_life`. Setup-specific. Data-derived. Single nanmin, no margin, no kneedle.
4. **Label.** `final_label = WIN iff mfe_during_life ≥ T else LOSS`. Examples sit at-or-above T by construction; lock holds structurally.
5. **Verify.** `examples_lock_passed = all(ex.final_label == "WIN")`. Should always hold by construction; halt on impossibility.

Non-ENTRY rows (REDUNDANT/NOENTRY/MISSING) keep pool-order slots with `status = "SKIPPED_NOT_ENTRY"`. Clusters with bad data get `status = "SKIPPED_MISSING_DATA"` and a `reason`.

---

## EXACT spec — `scripts/signal_exit_grinder.py` (legacy)

Population: rows from SQLite `examples` for the setup. Each example's signal bar is the cache-relative bar corresponding to `entry_date − 1 trading day`. Direction from SQLite `setups.direction`.

Per candidate `(expression, direction ∈ {>=, <=}, threshold)`:

1. For each example, walk forward up to 120 bars from the signal bar.
2. First bar k where `op(value[k], threshold)` is True is the exit bar.
3. If any example never triggers → drop candidate (100% trigger rule, no exceptions).
4. For surviving candidates, compute per-example realized ADR move = `(exit_close − signal_close) / ADR_at_signal` (sign-flipped for shorts).
5. Compute per-example capture efficiency = `move / MFE` where MFE is max forward favorable excursion in ADR over the forward window.

Ranking: median capture efficiency (primary), median ADR move (secondary). Top-50 saved. Threshold sweep: `np.percentile(pooled_values, np.linspace(5, 95, 20))`, dedup at 6-decimal rounding. Top-1 mirrored to Railway `exit_conditions` table.

Retained until the pipeline reorder retires its consumers.

---

## Details you need to know

- **Cross-source alignment uses dates, never bar indices.** OHLCV cache and expression cache have different start dates; carrying a bar index between them silently produces wrong values.
- **Per-signal pre-dedup.** Both grinders operate per-signal, not per-cluster-rightmost.
- **Breakouts only.** L14 labeler scope is HTF/BF/BASE. Fade setups (DTSS, 3-4DB) have a structurally different entry mechanic (§17.7) and are out of scope for L14 until a fade-specific labeler is designed.
- **NaN-tolerant.** L14 needs only OHLCV (not expression cache). Short-history examples (CRCL, XPEV) handled by `mfe_during_life` being computed within their own eff_horizon.
- **Trade-lifetime cap = 120 bars** (§2 c9a). Earnings cap applied.
- **The L14 labeler does not select an exit rule.** Realized P&L per cluster comes from profit grinder downstream (under the EV-after-Profit reorder).

---

## What L14 consumes and outputs

Consumes:
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `data/scanperfect.db` (`earnings_dates` table)
- `<classifier worktree>/research/classifier_tags/{setup}_tags.json`

Outputs:
- `data/signal_exit_grind/signal_exit_pool_{setup}.json` per setup. Schema in DATA_CONTRACT.md.
- Halts produce `data/signal_exit_grind/signal_exit_pool_{setup}_HALTED_{timestamp}.json` and the latest pointer is not updated.

Per-cluster fields (one entry per cluster in pool order, including SKIPPED rows):
`cluster_id, ticker, is_example, tag, signal_bar_idx, status, reason, entry_k, entry_bar, entry_date, cap_bar, cap_date, cap_cause, horizon, eff_horizon, adr14_at_entry, effective_entry, stop, stop_hit_bar, mfe_during_life, mfe_full_window, final_label`.

Per-setup top-level: `setup_type, grinder_type, timestamp, T_threshold_adr, T_setting_example, n_clusters_pool, n_entered, n_skipped_*, n_examples_entered, n_wild_entered, n_wild_win, n_wild_loss, wild_win_rate, examples_lock_passed, halted, halt_reason, cluster_meta[]`.

---

## What the legacy grinder consumes and outputs

Consumes:
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `local_runner/cache/pyramid_{setup}_*.json` (signal conditions)
- SQLite `examples`, `setups`

Outputs:
- `data/signal_exit_grind/signal_exit_{setup}_{n}ex_{adr}adr_{timestamp}.json`
- `data/signal_exit_grind/signal_exit_{setup}.json` (latest pointer)
- Railway `file_mirror` table + `exit_conditions` table (top-1)

---

## Known bugs

None known on the L14 labeler. The prior `floor_adr < 0` issue on the legacy grinder dissolved with the labeler reframing — that floor was a property of an exit-rule mechanic, not of a labeler.

---

## Pending research

### LOO sensitivity of T_HTF (accepted as ship-state, revisit if it becomes a problem)

`T_HTF = 2.376 ADR` is anchored by OSCR (cluster 177, h=9, full-life-by-earnings-cap, `mfe_during_life=2.376`). LOO-dropping OSCR shifts T_HTF to 4.216 (NHI), a 77% jump that drops wild WIN admission from 38.6% to 26.0%. BF (T=2.376, set by OSCR cid=229) has Δ=+0.51 under LOO. BASE (T=2.886, set by AXTI cid=47) has Δ=+0.10.

Same single-example-band-edge phenomenon flagged in PRESIGNAL_GRINDER.md §3.5. Doesn't violate any constraint — the labeler is correctly calibrated to "as forgiving as the weakest example." Levers if it later proves problematic: (a) re-vet OSCR's example status (DB change), (b) raise T to 2nd quartile of example mfe_life with a structural fallback for short-horizon anchors. Not in scope for ship.

### Fade implementation

L14 is breakouts-only (HTF, BF, BASE). Fade setups (DTSS, 3-4DB) have a structurally different entry mechanic (§17.7 anchors AVWAP for breakouts; fades work on rejection-of-ceiling). The L14 mfe_during_life primitive has not been validated for fades. Open whether the same mechanic transfers symmetrically (lower envelope on negative MFE for short-side) or whether fade labels need a different feature.

---

## Pending build

### Pipeline reorder (separate work item — not part of L14 ship)

EV-after-Profit reordering: `labeler → refinement → entry_candle_scorer → profit_grinder → ev_grinder`. EV consumes profit's per-trade detail as outcome variable. Currently EV-before-Profit per DEPENDENCY_MAP.md.

Implications:
- `raw_signal_clusters_*.json` schema cleanup: drop exit-rule-derived fields (`exit_bar`, `move_adr`, `exit_date`); add per-signal label + `mfe_during_life` + `eff_horizon` + `stop_hit_bar`. Touches DATA_CONTRACT.md and downstream consumers.
- Refinement grinder unit shifts from cluster-aware (rightmost+leftward) to per-signal pre-dedup. Per-signal labels from L14 attached, beam search scores against per-signal rows. Curve-fit defense (consensus + binomial significance) carries over.
- Cluster gathering step in `pyramid_grinder` no longer needs an exit rule (labeler attaches the label directly). Legacy `signal_exit_grinder.py` retires once no consumer reads `signal_exit_{setup}.json`.

The L14 labeler ships independent of this reorder — both grinder paths coexist until the reorder retires the legacy one.

---

## Operational notes

- Per-setup invocation: `python scripts/signal_exit_pool_grinder.py --setup {htf|bf|base}`. Runs in well under 1 second per setup. No expression cache reads (OHLCV-only).
- Output overwrites the per-setup latest pointer on every clean run. Halts produce a timestamped HALTED file and leave the latest pointer untouched.
- The HALTED file from the prior aggregate-PnL pool grinder run (`signal_exit_pool_htf_HALTED_20260425_202509.json`) is preserved as historical reference — do not delete; it documents the wrong-mechanic state that motivated the bakeoff.
