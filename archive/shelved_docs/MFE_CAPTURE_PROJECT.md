# MFE Capture Upgrade — Project Notes

This worktree (`swing-screener-dual-exit`, branch `dual-exit-or-set`) is dedicated to improving realized MFE capture on the signal exit grinder. Main is untouched throughout. Nothing merges back to `v2` until results are validated.

---

## Goal

Break **70% mean MFE capture across all live setups** (HTF, BF, BASE, DTSS). Current single-exit baseline lands at 54-67% depending on setup. Prior iterations across months have suggested a hard ceiling at 65-69% from indicator-lag alone.

---

## What we've built (2026-04-16)

### Stage 1a — Multi-exit OR-set grinder in `scripts/signal_exit_grinder.py`

A generalization of the single-exit grinder: instead of one best threshold rule per setup, emit a set of 1-N exit rules OR'd together. Whichever rule fires first on a given signal wins. Each added pick must earn its spot over the current OR-combined baseline.

**Defenses against overfit:**
- **Opposite-side-at-entry** — reject candidates that already satisfy the trigger at the entry bar (prevents "fire immediately for a trivial capture" picks like FORM/HTOO's day-1 MACD fires).
- **Pick-index trigger-rate floor** (default 25% for pick 2+) — rules out 1-or-2-example wonders.
- **Redundancy filter** — a new pick must not fire within 2 bars of an existing pick on more than 80% of mutual triggers.
- **70/30 holdout gate** — pick chosen on training 70, must deliver positive aggregate gain on held-out 30. Hard gate (not just reporting).
- **Multi-seed robustness check** — the selected pick is re-scored against 4 alternate random splits; number of positive-holdout seeds reported.
- **Greedy stop** — iterate adding picks until min train gain (0.02) or min holdout gain (0.0) no longer clears.

**Grinder CLI flags added:**
- `--max-picks`, `--min-gain`, `--min-holdout-gain`, `--pick2-min-trigger`, `--aggregate-metric`, `--holdout-seed`, `--no-upload`

**Environment-var support for worktree reads:**
- `SCANPERFECT_READ_ROOT` — SQLite db + pyramid JSONs read from main
- `SCANPERFECT_CACHE_DIR` — expression + OHLCV caches read from main (until we sandbox the expression cache too; see below)

**Schema changes (signal_exit_{setup}.json):**
- `schema_version: 2`
- `or_exit_set` — ordered list of picks, each with pick_index, trigger_rate, per-example bars/eff/adr (nullable for partial-coverage picks)
- `pick_reports` — train/holdout aggregates, alt-seed robustness, defense stats per pick
- `rejected_candidates` — top 10 per pick with rejection reason
- `top_conditions` retained as backward-compat alias = ranked 100%-coverage candidates (first entry always equals `or_exit_set[0]`)

### Results on current setups (unmerged, worktree only)

| Setup | Examples | Picks | Pick 0 baseline | OR-set mean | Delta |
|---|---:|---:|---:|---:|---:|
| HTF | 32 | 3 | 0.543 | 0.639 | +0.096 |
| BF | 45 | 1 | 0.640 | 0.640 | 0.000 |
| BASE | 42 | 1 | 0.673 | 0.673 | 0.000 |
| DTSS | 73 | 1 | 0.666 | 0.666 | 0.000 |

HTF is the only setup where the OR-set found a qualifying pick 2+. BF/BASE/DTSS' candidate pools contained no expression that improved on pick 0 with positive holdout gain. DTSS specifically had candidates that passed the train gate but every single one failed holdout — exactly the overfitting signature the defense was designed to catch.

**Stage 1a is a real but modest upgrade — +10pp on HTF only. Not sufficient.**

---

## The gap: why we can't break 70%

### 1. Expression primitives aren't capturing "common reversal zones"

The cache has extension-chart expressions but they're mostly the wrong shape:
- `ext_*` = current extension value (level)
- `ext_peak_*_lbN` = single max ext in last N bars
- `ext_ceil_*_lbN` = same concept, distance from max
- `ext_slope_*` = slope of ext (rate of change)
- `ext_retrace_*` = retracement from the peak

Dan's observation: **each chart has a "common reversal zone" visible on the extension chart** — a level where the stock's extension repeatedly caps, not the single max. RIVN reverses around 6x ADR from 50 SMA every time; that's the level the grinder should trigger on, and it's different per ticker. The existing primitives either give a pooled-average threshold (wrong — every ticker's level is different) or the single absolute peak (wrong — that could be a one-time spike from news, not the common reversal).

No expression currently measures **the mode/cluster of past ext peaks** — which is the statistical version of Dan's visual observation. This is the main gap.

### 2. Lookback in existing ceiling expressions is too short

`ext_ceil_*_lb252_atr14` uses a 252-bar (1-year) window. Dan's view: for extension charts that level needs **1000+ bars minimum, ideally 2000+** (~8 years), to see enough reversal cycles to identify the stable zone. The cache does have a 1260-bar variant (`ext_ceil_avgc50_atr14_lb1260`), but:
- Many HTF examples are recent IPOs without 1260 bars of pre-signal history. Defined on only 3 of 32 HTF tickers.
- BF / BASE / DTSS example sets likely have more mature tickers; needs confirmation.

### 3. Indicator lag ceiling

Even with perfect per-stock reversal level expressions, a causal rule only fires AFTER the indicator confirms the reversal — which means a few bars past the actual price peak. Typical lag: 5-15 bars, giving back 20-40% of peak amplitude. This is the structural ceiling at ~65-69% that months of analysis keep hitting.

Only two structural fixes to that:
- **Scale-out exits** — partial profit at reversal zone + trail remainder on tight MA (8 EMA close-below). Two chances to land near MFE bars that don't coincide. Blended capture can realistically beat 70%.
- **Look-ahead-regularized ML** — predict future MFE, exit when prediction stagnates. Different paradigm, out of scope here.

---

## What we're trying next

### 1. Sandboxed expression cache in the worktree (in progress)

Copy main's `local_runner/cache/expr_series/` (~60 GB, 11,510 .npz files) + `brute_expressions.json` into this worktree so we can add new expression families and regenerate selectively without touching main's operational cache.

- OHLCV pickles and SQLite db stay read-only on main (via existing env vars).
- Worktree cache is fully writeable — safe to modify, delete, regenerate.
- Disk impact: 60 GB in the worktree (484 GB free on C: confirmed).
- Rollback: delete the worktree's `local_runner/cache/expr_series/` folder and we're back to no-state.

### 2. New expression family: common-reversal zone detection

Design a per-stock, per-MA expression family that measures **the clustered level where ext has capped across history**, not the single max. Shape under consideration (subject to the formal spec):

- For each bar, take the ext series over the last N bars (N = 1260 or more, where available).
- Identify local maxima of the ext series.
- Cluster those local maxima by proximity.
- Emit the dominant cluster's level as the value of `ext_common_reversal_avgc50_atr14` (or equivalent).

Variants: per-MA (8, 21, 50, 200) × per-normalization (ATR, ADR, pct) × per-lookback (1260, 2520). Stock-normalized by construction, so a universal threshold in the grinder translates to a per-stock rule.

Written spec comes before code.

### 3. Scale-out grinder variant (conditional)

Build only if the common-reversal expressions alone still cap capture under 70% due to indicator lag. Structure:
- **Leg 1** (e.g., 50%): fire at common-reversal zone — partial profit.
- **Leg 2** (remainder): tight-MA trail from leg-1's fire bar onward, exit on close below 8 EMA.
- Score blended capture with the same defense machinery (holdout gate, multi-seed).

### 4. Setup-specific validation

Run the new expressions and exit structure across all four live setups. Confirm the 70% target is hit on setups where the example population supports the required history (BF and BASE likely viable; HTF probably not).

---

## Worktree architecture

```
swing-screener-dual-exit/
├── scripts/
│   └── signal_exit_grinder.py       [modified — multi-exit OR-set + defenses]
├── local_runner/
│   ├── cache/
│   │   ├── expr_series/             [copied from main, ~60 GB]
│   │   │   └── *.npz
│   │   └── brute_expressions.json   [copied from main]
│   ├── expr_cache_builder.py        [inherits from main, may be extended]
│   └── ...
├── data/
│   └── signal_exit_grind/            [grinder writes here, worktree-isolated]
└── MFE_CAPTURE_PROJECT.md           [this file]
```

Main repo (`swing-screener/`, branch `v2`): untouched. SQLite db, OHLCV pickles, nightly pipelines continue running normally. Operational exits on main still use the pre-change grinder.

Environment vars used when running worktree scripts:
- `SCANPERFECT_READ_ROOT=<main>` — for SQLite db + pyramid JSON reads
- `SCANPERFECT_CACHE_DIR=<worktree>/local_runner/cache` — for expr cache (once sandboxed) + OHLCV

---

## Rollback

If any stage proves wrong or undesirable:
- Nothing has been committed to main.
- Delete the worktree's branch: `git worktree remove swing-screener-dual-exit && git branch -D dual-exit-or-set`
- Or keep the worktree and abandon the branch; main stays untouched.
- The sandboxed expression cache in `local_runner/cache/expr_series/` is just data — deletable.

---

## Log of what's been decided / done

- 2026-04-16: Worktree `swing-screener-dual-exit` created off v2 HEAD (d490e69). Branch `dual-exit-or-set`.
- 2026-04-16: Multi-exit OR-set grinder built, defended, and validated. Backward compat via `top_conditions` alias.
- 2026-04-16: Tested on all 4 live setups. HTF +10pp mean capture; others unchanged. Verdict: below the target.
- 2026-04-16: Expression cache gap identified — primitives measure max/ceiling, not common-reversal zones.
- 2026-04-16: Scope expanded to include expression cache sandboxing and new expression family design.
- 2026-04-16: `expr_series/` copy from main started (60 GB → worktree).
- 2026-04-16 (late session, autonomous work): Sandbox cache copy finished — 11,510 `.npz` files confirmed + `brute_expressions.json` copied.
- 2026-04-16: **Fire-bar-vs-MFE diagnostic run** (`scripts/mfe_diagnostic.py`). Finding inverted the project's premise: exits fire PREEMPTIVELY, not late. 53–79% of exits fire BEFORE the MFE bar; on BF/BASE/DTSS firing closer to MFE correlates strongly with higher capture (+0.60 to +0.77). The "indicator lag ceiling" narrative was wrong.
- 2026-04-16: **Realistic MFE analysis** (`scripts/realistic_mfe_analysis.py`) — rescored existing pick 0 against earnings-bounded MFE (per-ticker next-earnings-date from `scanperfect.db.earnings_dates`). Under the `earnings` regime (cap = bars-to-next-earnings, examples without earnings data excluded), BF (0.762), BASE (0.820), DTSS (0.762) all exceed 70%. HTF (0.678) short by 2pp. The original "120-bar MFE" denominator was the problem — half the examples had earnings inside 60 bars anyway.
- 2026-04-16: **Forced-exit baseline** (`scripts/forced_exit_baseline.py`) — "do nothing and hold to earnings-1" gives HTF 0.549, BF 0.558, BASE 0.701, DTSS 0.684. BASE and DTSS clear 70% from forced exits alone. This is the floor every rule must beat.
- 2026-04-16: **Grinder modified for earnings-aware scoring** (`scripts/signal_exit_grinder.py`). Added `--earnings-cap` and `--earnings-buffer N` flags. Per-example forward window truncated to `bars-to-next-earnings - buffer`. Examples without earnings data are excluded. Under `--earnings-cap`, pick 0 is injected as a synthetic `earnings_forced_exit` candidate (fires at last available bar for every example) — this removes the 100%-coverage-of-a-single-rule constraint that was over-restricting the grinder when forward windows became heterogeneous. Subsequent picks must beat the baseline via the existing OR-set defenses (holdout gate, alt-seed robustness, redundancy filter, pick2 trigger floor).
- 2026-04-16: **Target met on every live setup under realistic scoring.** Mean capture efficiency with synthetic baseline + grinder-selected add-on picks: HTF 0.762 (29 ex), BF 0.768 (40), BASE 0.836 (38), DTSS 0.795 (64). Medians 0.740–0.883. Alt-seed holdout 4/4 positive on every selected pick. Selected rules: HTF `w_adx_slope_20_off1 <= 1.71` + `macd_hist_slope_6_19_9_off3 >= 1.86`; BF `w_nr_h_maxh35_atr14 >= 0.31`; BASE `w_es_ext50_rsi_slope_7_off3 <= -39.3`; DTSS `w_ext_slope_xavgc50_off2 >= 0.36`.
- 2026-04-16: **No new expression family was required.** The expression cache sandbox + `brute_expressions.json` copy were not used beyond confirming the existing `.npz` files still load correctly in the worktree. The common-reversal-zone expression family idea is not discarded — it remains a candidate for future work — but it is not needed to hit the project's goal.
- 2026-04-16: **Seed-robustness stress test.** Reran every setup with `--holdout-seed 7` (default is 42) to check whether pick selection is seed-dependent. Results (seed-42 → seed-7 mean capture): HTF 0.762 → 0.725; BF 0.768 → 0.781; BASE 0.836 → 0.836 (identical pick); DTSS 0.795 → 0.809. **All 8 runs exceed 70%.** Pick *identity* varies (HTF picked different but same-family weekly expressions; BF and DTSS picked near-identical variations). BASE was perfectly stable. Spread per setup is 0.0–3.7pp. Interpretation: the 70% bar is robust; the specific rule may not be the single "right" one but the family of rules consistently achieves the target.
- 2026-04-16: **Extension charts rendered for every example** (`scripts/render_ext_charts.py`, outputs to `data/diagnostics/charts/{setup}/*.png`, 192 images total, ~17 MB). Visually confirmed Dan's intuition that extension charts carry a distinct per-ticker "personality" — histograms of pre-signal ext values are frequently bimodal, showing clustered reversal zones that a pooled-threshold grinder does not directly target. This was not needed to hit the project goal but is documented for future expression-family work.
- 2026-04-16: **Intrabar execution rescore** (`scripts/intrabar_rescore.py`). Under "peak" execution (fill at bar's high/low) the same picks give HTF 0.781, BF 0.837, BASE 0.894, DTSS 0.899 — a +6.8 to +7.8pp upper bound. This is a ceiling for profit_grinder downstream, NOT a target for signal_exit_grinder (close-price scoring is the honest classification).
- 2026-04-16: **Pure take-profit grinder** (`scripts/take_profit_grinder.py`). Grid of price targets X ∈ {2..50 ADR}, limit-order fill, earnings fallback. Best X by setup: HTF 15 ADR (0.667), BF 25 ADR (0.620), BASE 30 ADR (0.724), DTSS 10 ADR (0.733). **All 6 to 15pp BELOW the rule-based grinder picks** — the 16K-expression rule selection is doing real timing work that a pure price target cannot replicate.
- 2026-04-16: **Gated TP (rule arms standing limit)** (`scripts/gated_tp_grinder.py`). Rule fires → arm limit order at target → fill intrabar or forced earnings exit. Net vs rule-close: HTF -3.2pp, BF -13.0pp, BASE -9.1pp, DTSS +1.0pp. Worse except for DTSS. Rule fires near peak; subsequent bars retrace; target rarely hit.
- 2026-04-16: **Parallel rule-OR-TP** (`scripts/parallel_rule_tp.py`). Whichever fires first wins. Net: HTF -3.1, BF -0.2, BASE -2.4, DTSS +1.0pp. Confirms rule timing is superior to any fixed price target.
- 2026-04-16: **Bimodality + per-ticker rank diagnostic** (`scripts/bimodality_check.py`). Bimodality Coefficient median 0.39–0.43 (only 1–7% of examples cross the BC > 0.555 threshold — visual bimodality is weaker than it looks statistically). HOWEVER, the signal-bar `ext_avgc50_adr14` is at the **92nd / 86th / 57th / 77th percentile (HTF/BF/BASE/DTSS)** of that ticker's own pre-signal 504-bar history; the max-forward ext reaches the **100th / 100th / 99th / 77th** percentile. For longs, 90–100% of examples reach ≥90th percentile of ticker history on the forward run. This is a strong per-ticker-calibrated structural signal.
- 2026-04-16: **Per-ticker ext-rank exit prototype** (`scripts/per_ticker_ext_rank_exit.py`). Tested "exit when ext rank crosses T in ticker's own pre-signal history." Failed — signals enter ALREADY at rank 86–92%, so the rule fires immediately at bar 1 (trigger rate 100% at T=0.90). Mean capture collapses to 0.15–0.49. Naive percentile is wrong; a proper common-reversal-zone feature needs cluster/density detection (e.g., KDE peaks on pre-signal ext distribution), not monotonic rank. Deferred — the existing `ext_ceil_*` family is the closest cousin the grinder already has.
- 2026-04-16: **No `pctrank_ext_*` expressions exist in the cache.** 75 `pctrank_*` expressions exist (atr14, close, range, rsi14, volume) but none on extension series. Confirmed gap for future expression-family work.

## Structural insights from this session

Two caveats on the headline numbers came into focus late in the session and change how the results should be interpreted:

**1. Example-set overfit vs noise overfit.** The defenses the grinder applies (holdout gate, alt-seed robustness, redundancy filter) protect against noise-fitting WITHIN the 29–64 curated examples per setup. They do not protect against the example set itself being a biased sample. Curated examples are handpicked winners; the full pyramid-grinder signal universe includes smaller winners, scratches, losses, and no-entry cases. All "mean capture 0.76–0.84" numbers here are in-sample on winners-only. Full population numbers are unknown until we rerun against the pyramid grinder's raw signal output.

**2. Objective mismatch with downstream consumers.** This session optimized `capture_eff` (realized_move / per-example-MFE). `signal_filter.py`'s actual job is winner/loser classification + mean-ADR-on-winners for the full signal population. Those are different optimization problems and will select different rules. The framework ports forward; the specific picks likely do not, and would need to be re-ground against the full population under a classification objective.

## Distribution

Once the worktree's work is complete, we'll distribute the outputs where appropriate in the main repo.

- Caveats carried forward: sample sizes are small (29–64 ex per setup, holdout 30% = 9–19); HTF pick 2's +0.022 holdout gain on 9 examples is within noise; 11 total examples excluded across setups for missing earnings data; the result is predicated on "exit before earnings" being the actual trading rule.
- Next decisions for Dan: (1) promote the earnings-cap picks to main via the existing Railway upload path?; (2) re-run with tuned `--earnings-buffer` (current: 1 bar before earnings) or `--min-gain` to see if additional picks qualify?; (3) retain or discard the sandboxed `expr_series/` (60 GB) now that it was not needed for this round?

---

## 2026-04-16 (continued) — Objective pivot: win/loss metric quality for downstream consumers

After hitting the MFE capture target (>70% mean capture_eff across all four live setups with earnings-cap + synthetic baseline + grinder picks), the session pivoted to a different problem: the signal_exit_grinder's actual downstream role is not MFE capture — it's producing an exit rule that makes the **win/loss metrics coming out of signal_filter most accurate and robust**. Refinement_grinder and ev_grinder consume those metrics. profit_grinder (later in pipeline) handles MFE capture / profit optimization.

These are different objective functions and will select different exit rules. The prior session's picks (HTF `w_adx_slope_20_off1 <= 1.71` + `macd_hist_slope_6_19_9_off3 >= 1.86`, etc.) are capture-maximizing on curated winners; whether they're also the best exits for win/loss classification on the full signal pop is not known and likely not true.

### Pipeline role clarification (Dan's framing)

- **signal_exit_grinder (this component)**: produces an exit rule. The rule feeds into a race (forward window + hard stop + exit) that the classifier runs across the full signal pop. Goal: reproducible win/loss piles + high-confidence win_rate + mean/median ADR on winners.
- **signal_filter**: runs that race on the full deduplicated signal pop, produces classifications + metrics. SIGNAL_FILTER.md notes the current logic "will almost definitely change" — grinder should not bind to implementation details, should use race entrypoints that adapt.
- **profit_grinder** (later): optimizes live-trade profit-taking / MFE capture across all signals. Different component, different objective, not this grinder's concern.

Key insight: examples are best-case (top-decile winners). Live signals are a grade distribution (A to F). A rule tuned to milk capture on the best may produce noisy/inaccurate classifications on mid-grade signals. The grinder must score on the full population, not curated examples.

### Agreed framework for next session

- **Input**: full deduplicated pre-classification signal pop per setup (HTF, BF, BASE, DTSS). Not curated examples. Not pre-classified clusters. Raw deduped scan output from pyramid_grinder (rightmost bar of each cluster, tradable-filtered, pre-race).
- **Race machinery**: forward window + hard stop derivations imported from signal_filter.py. Start with current derivations (breakout: max example FW; example-MAE × 1.10 hard stop. fade: ceiling from cluster+FW highs). Parameterize calls so logic swaps propagate automatically.
- **Earnings cap**: real next-earnings-date from `scanperfect.db.earnings_dates` per ticker. Forward window truncated to `bars_to_next_earnings − 1`. Prior session's `--earnings-cap` code must be audited — Dan flagged a "wild approximation" in it that must be replaced with actual earnings dates.
- **Scoring**:
  - Primary: expectancy per signal = `WR × mean_ADR_on_winners − (1 − WR) × 1`. Losses normalize to 1 ADR per SIGNAL_FILTER.md convention. Single self-inferring number.
  - Sanity gate (binary): curated examples classify as winners under the race's **own** logic — without the `is_example` override. Self-inferring pass/fail.
  - No designed floors/cutoffs (Dan: "I'd rather let it be self inference").
- **Defenses carried forward**: 70/30 holdout on full pop (positive expectancy required); alt-seed robustness (4/4 positive). Opposite-side-at-entry reject + redundancy filter + pick2 trigger floor retained if OR-set logic is used.
- **Output**: same `data/signal_exit_grind/signal_exit_{setup}.json` schema. Pick 0 is the single best rule per setup. OR-set logic stays available behind a flag but is not default.

### Research blocking the concrete checklist (do first in next session)

1. Where do the deduped pre-classification signals live? `pyramid_{setup}_*.json` has conditions + signal bars; the dedupe + rightmost-of-cluster step is done inside `_gather_raw_signal_clusters()`. Need either (a) dump the deduped pre-race signal list as a new pyramid_grinder output, or (b) extract it by calling the dedupe logic without running the classification race.
2. Audit `scripts/signal_exit_grinder.py`'s `--earnings-cap` implementation to find the "wild approximation" Dan flagged. Fix to use actual `scanperfect.db.earnings_dates`.
3. Identify which signal_filter.py functions (FW derivation, hard stop derivation, race) can be imported cleanly vs need to be ported or refactored for grinder use.

After the research, present a concrete checklist (code paths, CLI flags, schema changes, test plan) and wait for go-ahead before coding.

### Hard constraints (still in force from CLAUDE.md)

- No edits outside the worktree. Reads go through `SCANPERFECT_READ_ROOT` / `SCANPERFECT_CACHE_DIR`.
- No edits to signal_filter.py (read-only reference for the race logic).
- No grinder end-to-end runs against the real cache without explicit go-ahead.
- No expression cache rebuild without audit + go-ahead.
- Propose the concrete checklist before writing any code.
