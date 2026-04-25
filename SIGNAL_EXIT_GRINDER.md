# SIGNAL_EXIT_GRINDER.md

Authoritative spec for the signal-exit grinder — the component that derives the exit rule used to produce WIN/LOSS labels on a setup's signal population.

**Scripts:** `scripts/signal_exit_grinder.py`

This file describes intent and behavior; code is the source of truth for implementation details (no line numbers — they go stale). Per-setup current state + latest rule values live in `CLASSIFIER_SPEC.md §3.5`.

---

## Purpose

Search the expression-cache vocabulary for the boolean rule that best resolves a setup's signal population into per-signal trade outcomes (close-based exit on rule fire, stop-out otherwise). Provide the chosen rule and per-signal labels to downstream consumers.

---

## EXACT spec

Population: rows from SQLite `examples` for the setup. Each example's signal bar is the cache-relative bar corresponding to `entry_date − 1 trading day`. Direction from SQLite `setups.direction`.

Per candidate `(expression, direction ∈ {>=, <=}, threshold)`:

1. For each example, walk forward up to 120 bars from the signal bar.
2. First bar k where `op(value[k], threshold)` is True is the exit bar.
3. If any example never triggers → drop candidate (100% trigger rule, no exceptions).
4. For surviving candidates, compute per-example realized ADR move = `(exit_close − signal_close) / ADR_at_signal` (sign-flipped for shorts).
5. Compute per-example capture efficiency = `move / MFE` where MFE is max forward favorable excursion in ADR over the forward window.

Ranking: median capture efficiency (primary), median ADR move (secondary). Top-50 saved.

Threshold sweep: `np.percentile(pooled_values, np.linspace(5, 95, 20))`, dedup at 6-decimal rounding.

Top-1 mirrored to Railway `exit_conditions` table.

---

## Details you need to know

- Cross-source alignment uses dates, never bar indices. OHLCV cache and expression cache have different start dates; carrying a bar index between them silently produces wrong values.
- 100% example trigger requirement is hard — a candidate that fails to fire on any single example is dropped, no exception.
- NaN expression values never satisfy `>=` or `<=`; NaN bars cannot be exit bars.
- Forward window is fixed at 120 bars from the signal bar. Earnings cap not applied.
- The current production exit-rule source is this grinder's `signal_exit_{setup}.json`. Consumers listed in DEPENDENCY_MAP.md.
- BE and any stop-management ratcheting are out of scope for this grinder.

---

## What it consumes and what it outputs

Consumes:
- `local_runner/cache/universe_ohlcv_daily.pkl`
- `local_runner/cache/expr_series/*.npz` via `ExprSeriesCache`
- `local_runner/cache/pyramid_{setup}_*.json` (signal conditions)
- SQLite `examples`, `setups`

Outputs:
- `data/signal_exit_grind/signal_exit_{setup}_{n}ex_{adr}adr_{timestamp}.json`
- `data/signal_exit_grind/signal_exit_{setup}.json` (latest pointer)
- Railway `file_mirror` table + `exit_conditions` table (top-1)

Full output schema in DATA_CONTRACT.md.

---

## Known bugs

- HTF and 3-4DB top rules have `floor_adr < 0` per CLASSIFIER_SPEC §3.5 — at least one example loses money under the WIN-declaring exit. HTF driver = XPEV (single-exit structurally limited). 3-4DB mechanism unexamined.

---

## Pending research

- **Per-example capture-efficiency objective produces weak rules on small example piles.** Tried & live: median capture efficiency rank. Currently exploring: replacing it with an aggregate-P&L objective on the classifier-tagged signal population (see Pending build). Open: whether aggregate-P&L objective on a richer population produces materially better rule quality than per-example-fit on examples alone.
- **HTF + 3-4DB structural floor_adr<0.** Tried & failed: tightening the threshold sweep, varying max_forward. Open question: whether the new aggregate-P&L objective on the classifier-tagged population resolves it or surfaces the same structural limit.
- **Short-side / fade implementation.** Tried twice, failed both times on the per-example objective. Open whether the aggregate-P&L objective changes that.
- **Depth-2 AND/OR compositions.** Considered: a fallback search if a single-expression winner leaves obvious gaps (e.g., forced-exit-heavy patterns where a secondary gate would help). Not yet attempted.

---

## Pending build

**Pool-grinder rewire + new-field build.** Script at `scripts/signal_exit_pool_grinder.py` on main-repo worktree branch `signal-exit-pool` off v2.

Current state (2026-04-24):

- Input: `<classifier worktree>/research/classifier_tags/{setup}_tags.json`. Filtered to `tag == "ENTRY"`; non-ENTRY rows keep pool-order slots with null per-cluster output.
- ADR14 recomputed at `entry_idx` from OHLCV (the tag file's `adr_at_sig` is not consumed).
- Per-row output: `final_label ∈ {WIN, LOSS}` (scratch = LOSS), `exit_cause ∈ {exit_fire, stop_hit, forced_earnings, forced_time_cap}`, rule-independent `mfe_adr`.
- Per top candidate aggregate block: `n_win, n_loss, win_rate, win_pnl_{mean,median,p25,p75}, loss_pnl_mean, aggregate_mfe_adr, aggregate_pnl_capture_fraction`.
- `n_expressions` read from `ExprSeriesCache` at runtime.
- Objective, search shape, threshold sweep, longs-only all unchanged.
- Code + smoke-test complete. Gate-verify run NOT yet done.

**Verify gates** (still pending): examples-lock (every ENTRY-tagged example has `realized_pnl_adr > 0` under the top rule) and `aggregate_pnl_adr > 0`. Both must pass on htf, bf, base.

**Upstream requirements (external):** caught-up expression cache + re-emitted classifier tags against the caught-up cache. See DEPENDENCY_MAP.md for producers.

**Merge `signal-exit-pool` → v2** after gates pass on all three setups. On merge, EXACT spec above gets rewritten to describe the pool grinder; legacy `signal_exit_grinder.py` implementation is removed.
