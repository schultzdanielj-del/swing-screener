# Data Contract — V2 Railway SQLite Schema

**Last updated:** 2026-03-06  
**Status:** Authoritative. Build from this. Do not assume anything not written here.

---

## Design Principles

- Railway SQLite is the single authoritative store. All compute runs locally, results upload to Railway on completion. The UI reads only from Railway.
- Every grind run produces a versioned result (a "cycle"). The UI shows a dropdown of all saved cycles per setup type. You pick any one and hit "Restore" — it becomes the active cycle the watchlist reads from. Nothing is ever hard-deleted.
- Existing tables (`examples`, `pending_examples`, `rejected_signals`, `earnings_dates`, `ohlcv`, `extension`, `tradable_universe`, `universe_ohlcv`, `universe_exclusions`) are kept as-is. V2 adds new tables alongside them.
- All timestamps are UTC ISO-8601 strings: `"2026-03-06T14:30:22Z"`.
- All dates (signal dates, entry dates, chart dates) are `"YYYY-MM-DD"` strings.

---

## Existing Tables — Kept As-Is

### `examples`
Ground truth. One row per validated setup example.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| setup_type | TEXT | e.g. `"dtss"` |
| ticker | TEXT | |
| chart_date | TEXT | date the chart pattern is visible |
| entry_date | TEXT | date of the actual entry bar |
| created_at | TEXT | UTC timestamp |

Unique constraint: `(setup_type, ticker, entry_date)`

### `pending_examples`
AI vet queue. Signals that a human marked YES and are awaiting AI review + final approval.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| setup_type | TEXT | |
| ticker | TEXT | |
| signal_date | TEXT | date signal fired |
| entry_date | TEXT | date of the entry bar |
| status | TEXT | `pending` / `ai_reviewed` / `approved` / `rejected` |
| ai_verdict | TEXT | `GREEN_LIGHT` or `FLAG` |
| ai_reasoning | TEXT | one-line or multi-line AI explanation |
| review_notes | TEXT | human notes |
| created_at | TEXT | |
| reviewed_at | TEXT | timestamp of final human decision |

Unique constraint: `(setup_type, ticker, entry_date)`

### `rejected_signals`
Signals manually marked NO. Prevents them from re-surfacing in the vetting queue.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | |
| setup_type | TEXT | |
| ticker | TEXT | |
| signal_date | TEXT | |
| created_at | TEXT | |

### `earnings_dates`
| Column | Type |
|--------|------|
| ticker | TEXT |
| earnings_date | TEXT |
| updated_at | TEXT |

### `tradable_universe`, `universe_ohlcv`, `universe_exclusions`
Unchanged. `tradable_universe` is the 4,167-ticker universe used for all profiling and backtesting.

---

## New Tables — V2

---

### `grind_cycles`
One row per grind run, per setup type. This is the versioned snapshot record.

| Column | Type | Notes |
|--------|------|-------|
| cycle_id | TEXT PK | `"{setup_type}_{YYYYMMDD}_{HHMMSS}"` e.g. `"dtss_20260306_143022"` |
| setup_type | TEXT | e.g. `"dtss"` |
| status | TEXT | `running` / `complete` / `error` / `reverted` |
| error_msg | TEXT | populated if status = `error`; null otherwise |
| is_current | INTEGER | 1 if this is the active cycle for this setup type, 0 otherwise. Exactly one row per setup_type has is_current=1. |
| n_examples_at_grind | INTEGER | snapshot of how many examples were in the library when this grind ran |
| created_at | TEXT | UTC timestamp when grind started |
| completed_at | TEXT | UTC timestamp when grind finished; null if still running or errored |
| reverted_at | TEXT | UTC timestamp if this cycle was un-set as current; null otherwise |

**is_current mechanics:** When you restore a cycle, that cycle's is_current flips to 1. All other cycles for that setup_type flip to 0. One UPDATE + one UPDATE — atomic in a transaction.

**cycle_id** is human-readable and doubles as a filename label for the UI dropdown: `"dtss — 2026-03-06 14:30"`.

---

### `cycle_conditions`
One row per condition per grind cycle. The full condition set for a cycle.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| cycle_id | TEXT | FK → grind_cycles.cycle_id |
| tier | TEXT | `"D1"` / `"1wk"` / `"1mo"` / `"6mo"` / `"1yr"` / `"5yr"` |
| expression_name | TEXT | name of the expression from the library |
| low | REAL | lower bound of the passing range |
| high | REAL | upper bound of the passing range |
| filter_power | REAL | fraction of universe REMAINING after this condition (0.0–1.0). Lower = more restrictive. |
| sort_order | INTEGER | order conditions were locked in the grind |

Index: `(cycle_id)`

---

### `cycle_signals`
One row per deduped signal per grind cycle. The materialized signal set.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| cycle_id | TEXT | FK → grind_cycles.cycle_id |
| setup_type | TEXT | denormalized for query convenience |
| ticker | TEXT | |
| signal_date | TEXT | date conditions fired |
| bar_idx | INTEGER | bar index in the 5yr OHLCV series |
| close | REAL | close price on signal date |
| adr | REAL | ADR value on signal date |
| is_example | INTEGER | 1 if (ticker, signal_date) matches an entry in the `examples` table; 0 otherwise |
| classification | TEXT | `AUTO_WIN` / `AUTO_LOSS` / `MANUAL_WIN` / `MANUAL_LOSS` / `AI_WIN` / `UNCLASSIFIED` |
| classification_source | TEXT | `example` / `exit_filter` / `manual` / `ai_approved` |
| exit_triggered | INTEGER | 1 if exit condition fired within max_forward bars; 0 otherwise |
| exit_date | TEXT | date exit condition fired; null if not triggered |
| move_adr | REAL | (signal close − exit close) / ADR at signal date; null if no exit |
| mfe_adr | REAL | max favorable excursion in ADR from signal bar forward |
| capture_eff | REAL | move_adr / mfe_adr; null if no exit |
| regime_score | REAL | regime model score at this signal's date; null until regime model has run |
| vetted_at | TEXT | timestamp of manual vetting decision; null if auto-classified |

Index: `(cycle_id)`, `(cycle_id, classification)`, `(ticker, signal_date)`

**Classification priority (applied in order, first match wins):**
1. `(ticker, signal_date)` matches `examples` table → `AUTO_WIN` / source = `example`
2. `pending_examples` row with status = `approved` → `AI_WIN` / source = `ai_approved`
3. `rejected_signals` row exists → `MANUAL_LOSS` / source = `manual`
4. exit_triggered = 1 AND move_adr >= sample median ADR → `AUTO_WIN` / source = `exit_filter`
5. exit_triggered = 0 OR move_adr < sample median ADR → `AUTO_LOSS` / source = `exit_filter`

---

### `exit_conditions`
One row per setup type. The exit expression used by the exit filter. Updated only when the exit grinder is re-run.

| Column | Type | Notes |
|--------|------|-------|
| setup_type | TEXT PK | |
| expression_name | TEXT | expression from the library that defines "move is over" |
| direction | TEXT | `"above"` / `"below"` / `"crosses_above"` / `"crosses_below"` |
| threshold | REAL | value the expression must breach |
| max_forward_bars | INTEGER | how many bars forward to scan for exit |
| adr_threshold_multiplier | REAL | move must be >= (sample_median_adr × this value) to count as meaningful. Default 1.0. |
| updated_at | TEXT | timestamp of last exit grinder run |

---

### `cycle_health`
One row per completed grind cycle. Health check output. Written by `health_check.py` after every cycle completes.

| Column | Type | Notes |
|--------|------|-------|
| cycle_id | TEXT PK | FK → grind_cycles.cycle_id |
| setup_type | TEXT | |
| — **Signal quality** — | | |
| n_signals | INTEGER | total deduped signals in this cycle |
| peak_per_day | REAL | max signals on any single calendar day |
| avg_per_day | REAL | mean signals on active trading days |
| signal_stability_pct | REAL | % of this cycle's signals also present in the previous cycle (null for first cycle) |
| — **Example coverage** — | | |
| examples_passing | INTEGER | must equal n_examples_at_grind; hard fail if not |
| examples_added_this_cycle | INTEGER | net new examples added to library since previous grind |
| examples_since_last_grind | INTEGER | cumulative examples added since the grind that produced this cycle |
| — **Classification quality** — | | |
| win_rate_auto | REAL | winners / total, auto-classified signals only |
| win_rate_vetted | REAL | winners / total, manually vetted signals only; null if none vetted |
| pct_manually_vetted | REAL | fraction of signals with a manual label |
| — **EV estimate** — | | |
| median_winner_adr | REAL | median move_adr on AUTO_WIN signals |
| median_loser_adr | REAL | median move_adr on AUTO_LOSS signals |
| ev_estimate | REAL | win_rate_auto × median_winner_adr − (1 − win_rate_auto) × median_loser_adr |
| — **Cycle delta** — | | |
| prev_cycle_id | TEXT | cycle_id of the previous complete cycle for this setup type; null for first |
| signal_count_delta | INTEGER | n_signals − prev cycle n_signals; null for first |
| condition_count_delta | INTEGER | conditions in this cycle − prev cycle; null for first |
| win_rate_delta | REAL | win_rate_auto change vs prev cycle; null for first |
| — **Recommendations** — | | |
| promote_recommendation | TEXT | `promote` / `flag` / `hard_reject` |
| flag_reason | TEXT | human-readable reason if flagged or hard_rejected; null otherwise |
| live_ready | INTEGER | 1 if all live-readiness thresholds are met; 0 otherwise |
| live_ready_blockers | TEXT | JSON array of threshold names not yet met; null if live_ready=1 |
| computed_at | TEXT | timestamp health check ran |

**Hard reject conditions (promote_recommendation = 'hard_reject'):**
- examples_passing < n_examples_at_grind

**Flag conditions (promote_recommendation = 'flag', requires explicit confirmation to restore):**
- signal_count increased AND win_rate_auto decreased
- signal_stability_pct < 50

**Otherwise:** promote_recommendation = 'promote' (auto-restores as current)

**Live-readiness thresholds (all must be true for live_ready = 1):**
- signal_stability_pct >= 80 (across two consecutive cycles)
- avg_per_day between 2.0 and 7.0
- win_rate_auto >= 0.40
- ev_estimate > 0
- median_loser_adr < 1.0
- examples_added_this_cycle + prev cycle examples_added_this_cycle < 5

---

### `regime_model`
One row per setup type. The current regime model state — correlation weights derived from classified signals vs market conditions. Updated every cycle.

| Column | Type | Notes |
|--------|------|-------|
| setup_type | TEXT PK | |
| cycle_id | TEXT | cycle this model was computed from |
| n_signals_used | INTEGER | total classified signals the model was fit on |
| feature_weights | TEXT | JSON object: `{"spy_rsi14": 0.31, "spy_ext_sma50_atr": -0.24, ...}` — correlation of each indicator with win outcome |
| top_features | TEXT | JSON array of the 5 most predictive feature names, ranked |
| baseline_win_rate | REAL | overall win rate across all signals regardless of regime |
| updated_at | TEXT | |

---

### `signal_regime_scores`
Per-signal regime scores for the historical record. Written when regime model runs.

| Column | Type | Notes |
|--------|------|-------|
| cycle_signal_id | INTEGER PK | FK → cycle_signals.id |
| cycle_id | TEXT | |
| regime_score | REAL | 0.0–1.0; similarity of that signal's market fingerprint to historically winning environments |
| expected_win_rate | REAL | model's predicted win rate at this regime score level |

(This table is the backing store. `cycle_signals.regime_score` is the denormalized copy for query convenience — written from here.)

---

### `nightly_watchlist`
Append-only. One row per signal per nightly run. Historical record preserved.

| Column | Type | Notes |
|--------|------|-------|
| id | INTEGER PK | autoincrement |
| run_date | TEXT | `"YYYY-MM-DD"` date of the nightly run |
| setup_type | TEXT | |
| ticker | TEXT | |
| signal_date | TEXT | date signal fired (will often equal run_date for fresh signals) |
| cycle_id | TEXT | which cycle's conditions produced this signal |
| regime_score | REAL | rolling average regime score across the signal-to-entry window |
| expected_win_rate | REAL | model's predicted win rate at this regime score |
| rank | INTEGER | rank within tonight's list, 1 = highest |
| expected_move_adr | REAL | sample median ADR for this setup type (informational) |
| ai_vet_status | TEXT | `LOOKS_GOOD` / `FLAGGED` |
| ai_vet_reason | TEXT | one-line reason if FLAGGED; null if LOOKS_GOOD |
| created_at | TEXT | UTC timestamp |

Index: `(run_date)`, `(run_date, setup_type)`

---

## Summary: New Tables

| Table | Purpose |
|-------|---------|
| `grind_cycles` | Versioned cycle registry; tracks status and which is active |
| `cycle_conditions` | One row per condition per cycle |
| `cycle_signals` | One row per deduped signal per cycle with all classification + exit data |
| `exit_conditions` | Exit expression per setup type; stable across cycles |
| `cycle_health` | Health check metrics per cycle; drives promote/flag/reject and live-ready |
| `regime_model` | Current regime correlation weights per setup type |
| `signal_regime_scores` | Per-signal regime scores (historical record) |
| `nightly_watchlist` | Append-only watchlist; one row per signal per nightly run |

---

## Key Invariants

1. Exactly one `grind_cycles` row per setup_type has `is_current = 1` at all times (after the first grind completes).
2. `cycle_signals.is_example` is computed at write time by checking `(ticker, signal_date)` against the `examples` table. It is never updated after write.
3. Classification is re-derived from source tables (examples, pending_examples, rejected_signals) every time classify_signals.py runs — it is not immutable. A signal can move from AUTO_WIN to MANUAL_LOSS if you reject it.
4. `exit_conditions` has one row per setup_type. It is written only by exit_grinder.py. The exit grinder does not run automatically — it is triggered manually.
5. The watchlist reads only from the `is_current = 1` cycle for each setup type.
6. All grind computation runs locally. All results upload to Railway on completion. The UI never reads from local files.
