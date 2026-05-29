# Presignal Grinder — Component Spec

Authoritative doc for the pre-classifier pool filter. Emerged 2026-04-18/19 during classifier work when it became clear the classifier can't reliably label a signal pool that hasn't been checked for structural setup-coherence against labelled examples. Companion to `CLASSIFIER_SPEC.md`; upstream of the 5-pile labeler.

---

## 1. Purpose

A signal pool from the pyramid/consensus grinder contains clusters that fire the TA signal conditions but may or may not be real setups. The classifier's job is to label each cluster's outcome (WIN/LOSS/BE/AMBIGUOUS/NO_ENTRY). If the pool has material noise — clusters that pass signal conditions but have no structural resemblance to actual setups — classifier output is polluted:

- `p_ambiguous` inflates from noise clusters that never develop a tradeable entry or resolve cleanly.
- `WIN`-pile realized WR is biased low by noise wins that aren't real setup wins.
- Downstream EV quotation reflects the polluted pool, not the tradeable universe.

The presignal grinder is a per-setup filter that narrows the pool BEFORE the classifier runs. It keeps clusters whose pre-signal + signal-bar feature profile lies within the range defined 100% by Dan's labelled examples on that setup. Clusters whose profile falls outside the example range on any of a selected feature set are dropped as NO_ENTRY-at-source — they never get passed to the classifier.

The principle: if a cluster doesn't look like an example on the broadest reasonable feature basis, it's not the setup, no matter what the signal conditions say.

---

## 2. Invariants and tools

Constraints every architecture proposal must honour, plus the data-derived tools available to the next iteration.

### 2.1 Invariants

- **100% EX pass is definitional.** Every labelled example must pass the final filter by construction. No architecture that trims examples is permitted.
- **NaN-lenient symmetric.** Examples and candidates both contribute whatever bars they have. Offsets/bars/features an example doesn't reach do not constrain the filter at those offsets, and don't disqualify the example. Candidates with NaN at a feature auto-pass that feature's check. A CRCL-like recent-IPO example with 5 bars of history is in the example pool and contributes to its reachable offsets; a CRCL-like wild candidate with 5 bars is scanned and auto-passes unreachable offsets.
- **Post-2020-01-02 date cutoff** on wild-universe scans.
- **§5.2 cluster dedup** applied before any downstream consumer: per-ticker consecutive-bar rightmost-wins, split at labelled example bars so each example is its own cluster's rightmost.
- **Weekly anchor rule: close[E-1]** — the weekly anchor bar is the ISO week containing date E-1, truncated to close at close[E-1]. Partial-week close at the day before entry. Applies everywhere weekly aggregation is used.
- **Entry bar E is never touched.** Anchor is always E-1 (daily) / W-0 (weekly). Filter operates purely on pre-signal context; whether the setup fires an entry, wins, or loses is downstream.
- **Data-derived everything.** Dan suggests strategies and methods; quantities, thresholds, datasets, and per-setup parameters derive from the data. No hand-picked numbers, no opinion-derived thresholds.
- **Calibration target: ≤ ~10× spread across setups.** Per-setup volumes can differ naturally; a spread substantially larger than ~10× across the 5 setups is evidence of miscalibration, not real heterogeneity.
- **Setup-agnostic code.** Per-setup parameters differ because data differs; algorithm is uniform. No class-keyed or setup-keyed branches in control flow.
- **OHLC basis** by default. Any proposal that re-introduces expression-cache features must document how its composition avoids compound-probability-collapse (see §3.1 / §3.2).

### 2.2 Rejected a priori (do not propose)

- **K-of-N softening** — allow a candidate to fail K of N feature checks. The filter is a bounding object, not a voting system.
- **Tiers** — multiple outputs per setup (tight/medium/loose). One output per setup.
- **Regime-based filtering** — per-regime band sets or regime features inside the filter. Regime lives in the EV grinder, not here.
- **Statistical alternatives that trim examples** — p5-p95 bands, KDE/Gaussian density thresholds, supervised score thresholded above weakest example. All break 100% EX pass.

### 2.3 Tools available

- **Leave-one-out (LOO) stability.** Drop each example in turn, rebuild the filter, measure how far the held-out example falls outside. Equivalent to "frontier-gap" slack (`e_1 − e_0` on lower frontier, `e_{N-1} − e_{N-2}` on upper) per cell. Self-scales with example consensus — tight consensus → small slack, dispersed → larger slack. In §4 the LOO signal is reused as a generalization measure on a cumulative filter (LOO-example pass rate as cells are added).
- **Permutation null.** For each cell or feature, build the filter from N random pseudo-examples drawn from the wild universe; repeat many times; compare the real filter's tightness or carve-rate to the null distribution. Cells where the real filter is statistically indistinguishable from random are not carrying signal.
- **Kneedle elbow.** Point of maximum perpendicular distance from the line connecting first and last values of a sorted curve. Used throughout the pipeline for data-derived cutoffs — N derivation, horizon M derivation, signal-vs-noise boundary on cumulative-admission curves, etc. Any "where does the curve flatten" cutoff in a future proposal should reuse it.

---

## 3. History of rejected architectures

Four architectures have been tried and documented. This section preserves each one's failure mode so future iterations don't repeat the mistake.

### 3.1 §13 — Cache-basis strict-AND (2026-04-19 early)

**Architecture.** At every bar in the N-window lead-up, test every feature in the ~16,039-feature expression cache against `[min_ex, max_ex]` bands. Strict-AND across 16k features × (N+1) bars. 100% EX by construction.

**Failure mode.** Relevance-filtering (Spearman ρ > 0 between offset and range) only dropped 10–20% of bands at each offset. 80–90% of bands survived at each bar. Strict-AND across ~300k–600k surviving bands produced **compound-probability-collapse to zero wild hits** — not a single non-example candidate passed across the full universe.

**Takeaway.** Strict-AND over thousands of per-feature bounds never works regardless of feature selection. The compound-probability of passing N independent-ish bounds approaches zero as N grows. Any new architecture that would AND more than a few dozen features must justify why compound-prob-collapse won't occur (e.g., because features are highly correlated and effective dimensionality is much smaller than nominal).

### 3.2 §14 — Two-stage composition (2026-04-19 mid)

**Architecture.** Stage 1 = 4-axis OHLC per-offset bands (log_close, log_high, log_low, atr_close ratios) with a max-LOO-bars-outside budget as softening. Stage 2 = 8,000-feature expression-cache subset with band filtering. Composed as strict-AND.

**Failure mode.** Same compound-probability-collapse as §13. Softening via max-LOO budget turned out to be dominated by low-volatility tickers (bond ETFs) whose ADR-normalized OHLC fit in tiny boxes — these candidates passed Stage 1 trivially without matching setup shape. Stage 2 did 100% of the carving; Stage 1 was effectively pass-through. Full-universe run: **zero wild hits**.

**Post-mortem — four specific dead ends catalogued:**

1. Plain `ex_range[k,c] < rand_range[c]` band filter — carves only 10–20% of bands; survivors → strict-AND → collapse.
2. Max-LOO-bars-outside OHLC budget — too permissive with only 3 axes; one loose held-out example sets tolerance for all candidates; misbehaves on low-vol tickers.
3. Two-stage composition as spec'd — Stage 1 effectively pass-through, Stage 2 did all carving, full composition still collapsed.
4. **Any strict-AND over >50% surviving bands** — compound probability collapses regardless of upstream staging.

**Takeaway.** K-of-N style softening doesn't save strict-AND from compound-prob-collapse; it just re-locates the failure. Two-stage pipelines inherit the compound-prob problem from their strict stage.

### 3.3 §15 — F1 hull + 5-descriptor Location on OHLC (2026-04-20)

**Architecture.** Two axes composed as AND:

- **F1 (Visual).** For each adjacent offset pair (k, k+1) in the N-bar lead-up, examples' `(log(close[E-k]/close_E), log(close[E-k-1]/close_E))` points form a 2D convex hull. Candidate passes iff its path's matching pair is inside every hull. Joint-pair hulls preserve inter-offset shape coherence that per-offset 1D bands drop.
- **Location.** Five scale-invariant context descriptors ANDed: D1 price position in M-bar range, D2 pre-lead-in log-return, D3 bars-since-higher-close (capped), D4a/D4b log distance from ATH/ATL, D5 recent vs long vol ratio. Horizons M data-derived per setup by kneedle.

Daily and weekly scales composed via cluster-key intersection (daily∩weekly). NaN-lenient symmetric applied at both scales. Weekly anchor = close[E-1].

**Results (full 11,842-ticker universe, post-2020-01-02, 100% EX end-to-end):**

| setup | n_ex | daily F1∩Loc | weekly F1∩Loc | daily∩weekly |
|---|---|---|---|---|
| HTF | 32 | 1,206 | 41,748 | 83 |
| BF | 45 | 3,335 | 17,873 | 449 |
| BASE | 42 | 46,482 | 281,893 | 8,370 |
| DTSS | 73 | 9,219 | 304,079 | 1,255 |
| 3-4DB | 26 | 551 | 91,404 | 50 |

Calibration spread daily∩weekly: 8,370 / 50 = **167×**. Target is ≤10×.

**Root-cause diagnosis (2026-04-20 late).** Two diagnostics run after the calibration failure:

- **Hull contributor counts per offset pair.** Every F1 pair on every setup, both scales, has 26–73 contributors (full or near-full example count). No `hull=None` cells anywhere. Hulls are well-populated; just geometrically large for heterogeneous example sets.
- **Per-descriptor keep rates on the Location axis.** 4 of the 6 descriptors are near pass-through (admitting 85–100% of the market) across every setup:
  - D3 (weeks since higher close) — 99.9% on weekly; bound is [1, cap] on every setup (one example near the bottom, one near the top).
  - D2 (pre-lead-in trend) — 92–100% on both scales.
  - D4a (log_ath) — 87–100% on weekly, except 3-4DB.
  - D5 (vol ratio) — 84–95% on most setups.
  - Only D1 (pos in range) and D4b (log_atl) do consistent carve.

**Mechanism.** With the effective filter reduced to F1 + D1 + D4b (only 3 independently-carving axes), volume scales with F1 hull area, and F1 hull area scales with example heterogeneity per setup. Tightly-clustered example sets (3-4DB, 26 examples in Oct/Nov 2025 only) produce tiny hulls → 50 DW clusters. Heterogeneous example sets (BASE, 42 examples spanning 2020–2026 across regimes) produce large hulls → 8,370 DW clusters. Nothing backstops the hull-area-scales-with-example-variance mechanism.

**Takeaway.** A bounding-region filter with too few independently-carving axes inherits calibration instability directly from example variance. More axes needed. MA-family basis is one route (see §3.4). Expression-cache is another, provided the composition avoids §13/§14 compound-prob-collapse.

**Survives from §15 and remains usable:**

- NaN-lenient-symmetric semantic.
- Weekly anchor rule (close[E-1]).
- §5.2 cluster dedup logic.
- Weekly aggregation code (`presignal_weekly_aggregate.py`).
- Per-setup W_N and W_M* values derived via kneedle on weekly spread — usable by any later architecture that needs a lead-up window size.

Full §15 data (results tables, bounds, hull stats) archived in `project_presignal_grinder_f1_location.md` memory.

### 3.4 §16 — MA-corridor cells with reject-rate kneedle (2026-04-20 mid)

**Architecture.** 24 MA types (SMA + EMA at 12 periods {5, 8, 10, 13, 20, 21, 30, 50, 65, 100, 150, 200}) × 40 daily offsets × per-setup weekly offsets. Per-cell corridor = `[min_ex, max_ex]` with frontier-gap slack (`e_1 − e_0`, `e_{N-1} − e_{N-2}`). Cell inclusion via kneedle on sorted market-reject-rate curve, intersected across LOO folds. Scan = strict-AND of all included cells on a fixed random-sample market baseline. Daily and weekly composed as AND.

**Selection phase completed for all 5 setups, 100% EX self-check passed:**

| setup | W_N | n_ex | daily cells | weekly cells |
|---|---|---|---|---|
| HTF | 48 | 32 | 831 | 384 |
| BF | 44 | 45 | 660 | 239 |
| BASE | 41 | 42 | 656 | 66 |
| DTSS | 16 | 73 | 703 | 174 |
| 3-4DB | 14 | 26 | 287 | 173 |

**Failure mode.** Partial scan ran HTF to ticker 6,000/11,842 with 170k pre-dedup passes (~28 passes per ticker, ~1.9% admission of bar-opportunities). Projected full-scan: ~500× too loose.

**Root-cause diagnosis — pathological cell redundancy.** SVD of the `(n_ex × n_cells)` feature matrix per setup per scale:

| setup | daily cells | daily eff-rank @ 99% energy | weekly cells | weekly eff-rank @ 99% |
|---|---|---|---|---|
| HTF | 831 | 4 | 384 | 5 |
| BF | 660 | 3 | 239 | 5 |
| BASE | 656 | 7 | 66 | 3 |
| DTSS | 703 | 5 | 174 | 4 |
| 3-4DB | 287 | 2 | 173 | 3 |

At 50% energy every setup collapses to rank 1 — a single dominant factor (broad price-vs-close trend) plus tail. 831 "constraints" are ~4 truly-independent constraints in a clever disguise. SMA5 at offsets E-6..E-39 is 34 cells measuring near-identical quantities (SMA5 shifts by 1/5 per bar). Adjacent-offset cells and cross-period SMAs all track the same underlying trend factor.

**Admission math:** 4 truly-independent corridors × ~40% admission each (wide ex_min–ex_max + frontier-gap slack) → 0.4⁴ ≈ 2.5% admission. Matches observed 1.9%. Stacking more correlated cells can't tighten past this — adding a 5th redundant cell that says the same thing as the existing 4 gives no carve.

**Two simultaneous design errors:**

1. **The filter's corridor mechanism is too wide.** `[ex_min, ex_max]` on 32 HTF examples spanning 2020–2026 regimes is already wide in absolute terms. Frontier-gap slack adds 8–40% more. Each single cell admits ~40% of market.
2. **The selection mechanism picked correlated duplicates.** Kneedle on sorted-reject-rate cut permissively because the reject-rate curve plateaus high across most cells (most cells have 50%+ reject relative to market because examples cluster in market tails). 86% of possible cells for HTF were selected.

**Takeaway.** Picking more cells from the MA-value basis cannot tighten past the effective-rank ceiling. Either (a) the feature basis needs genuinely more independent dimensions, or (b) the corridor mechanism itself needs to be tighter per cell, or (c) both. §4 takes route (b): per-example σ-cloud corridor widens/narrows naturally with how locally stable each SMA is, combined with an LOO-based overfit stop that catches chance-tight cells directly rather than relying on kneedle on a correlated curve.

**Artifacts that survive as diagnostic inputs:**

- `research/presignal_ma_corridor/{setup}_selection.json` — per-setup cell lists and corridor bounds. Usable as reference for which (MA, bar) cells the kneedle-on-reject picked.
- `research/presignal_ma_corridor_diag.py` — SVD-based effective-rank diagnostic. Reusable on any future cell set.
- Per-example / per-cell MA value extraction machinery in `research/presignal_ma_corridor.py` — the feature-computation helpers (`compute_mas`, `daily_feat_at`, `weekly_feat_at`, `build_ticker_cache`) are reusable.

---

### 3.5 σ-cloud union bands with LOO overfit walk-up (2026-04-20 late — rejected 2026-04-21)

**Architecture.** For each (MA period p, bar offset k) cell, per-example ±1·σ clouds where σ = rolling SD of the SMA line over p bars. Union band: `Upper = max_i(lr_i + σ_i)`, `Lower = min_i(lr_i − σ_i)`. Global cell ranking by cross-ex SD of log-ratios (tightness). Walk-up driver added cells rank-ascending, tracked two curves: (a) market admission rate, (b) LOO example pass rate. Kept range = between market-admission kneedle elbow and first LOO-drop. Strict-AND across kept cells.

**Results and failure modes.**

1. **σ-cloud inflation on volatile examples.** For BASE × SMA5 weekly k=1: central log-ratios clustered with cross-ex SD = 0.017 (examples tightly agreed). But the union band was 0.219 wide — ~12× wider than the cluster — because AXTI (volatile microcap, σ_i = 0.109 in log-space) single-handedly defined BOTH band edges. One volatile example dominates both `max(lr+σ)` and `min(lr−σ)`. Tight setups that happened to contain any volatile-MA example got blown-out bands. Dropping σ was found to shrink widths 2–2.5× on BASE short-term weekly cells, recovering the genuine cluster tightness.

2. **LOO drop ordering inverted vs spec assumption.** The spec predicted market-admission elbow comes first, LOO drop later — kept range between. Empirically across all 5 setups, LOO-drop K* (strict 100% failure) landed at K=117–171 out of ~1,500 cells, while market-admission elbow landed at K=481–1,079. LOO drop was in the pre-elbow no-op zone. Strict-100%-LOO walk-up terminated at K*−1, which sat at admission rates of 84–94% — essentially no carve. Mechanism: any "tight" cell has some example at the band edge; removing that example contracts the N−1 band past it; LOO fails. Spec's "redundancy at band edges" assumption doesn't hold in practice — edges are defined by individual examples, not clusters.

3. **σ + LOO added machinery before simplest baseline was tested.** Plain strict-AND over all no-σ `[min_i lr_i, max_i lr_i]` bands (no cell ranking, no overfit protection) produced usable admission (0.76–20%) on the 5,000-bar market sample with 100% EX preserved by construction. The ranking/walk-up layer was premature optimization.

**Takeaway.** (a) σ clouds as union-band slack amplify whichever example has the largest σ — not suitable as a data-derived robustness knob. (b) LOO on strict-100% threshold catches example-at-band-edge cases that aren't overfit, just outliers — can't be used as-is. (c) Test the simplest filter first before layering ranking/walk-up/protection.

**Survives from §3.5 and remains usable.**
- Per-example trajectory extraction (anchored log-ratio, multi-MA × daily+weekly) — `research/presignal_sma_band_extract.py`.
- Scan-path lr computation pattern (reused in §4 for bit-for-bit band consistency).
- Weekly partial-anchor patch logic.

Full §3.5 data (walk-up curves, LOO first-drop examples per setup, tightness distributions) archived in `project_presignal_grinder_walkup_failure.md` memory.

---

### 3.6 Carve-greedy weekly chain (2026-04-27)

**Architecture.** Chain of binary OHLCV/MA-derived events fired across the W_N=44 weekly lookback. Each chain element advances state when its event fires at a bar strictly later than the previous step's bar. Universal-event filter retains events firing in 100% of BF examples in their lookback. Chain build = carve-greedy: at each step, score candidates by conditional universe-rejection on a random sample; add the lowest-pass under 100% example pass; stop when no candidate keeps all examples advancing.

**Failure mode — structural anchor-pinning.** The chain capped at length 4 on BF. The first three steps were all "new k-week high" events (20w, 10w, 5w) — the right structural picks. But because BF setups make their fresh high AT THE ANCHOR (entry bar), several examples had their last chain step land on the anchor itself, leaving zero room for a 5th step. With strict 100% example pass + monotone advance + breakouts at the anchor, no candidate event can extend the chain regardless of pool depth.

**Triangulation.** Pyramid-signal pass-rate on the chain was very high; random universe pass-rate was also high; both groups had chain endings spread across the lookback rather than clustered at the anchor. The chain doesn't anchor-pin: it admits any chart that's had a new-high cascade somewhere in the past 44 weeks, regardless of when. This is the symmetric reason it can't extend AND can't standalone-deploy.

**Survives.** Chain forward-stable on its own (low walk-forward FR in the late-prior regime). Carve-greedy build mechanism is reusable. Universal-event filter logic reusable. Structurally-needed companion is a complementary anchor-pinned filter — see §6.

**Takeaway.** Event-chain primitive with strict 100% pass + monotone-advance is fundamentally incompatible with breakout setups whose terminal events fire at the anchor. The chain captures real BF cascade structure but can't compound carve alone; pair with a per-offset anchored filter (§6).

### 3.7 Expression-trajectory sign-coherence (2026-04-27)

**Architecture.** Sign-only extension of §4 cells to the full ~16k-expression cache. For each (expression, daily offset k), cell asks: sign of `expr[E-1-k]` minus `expr[E-1]`. Threshold = zero (structural). Cells kept iff 100% of examples agree on the sign.

**Failure mode — high-dim overfit despite 100% agreement gate.** Pool of ~778k candidate cells against 45 examples. Many cells survive 100% example agreement by chance, not by structural signal. Walk-forward FR in the late-prior regime is far above target — cells passing 45 examples don't extend to held-out examples. Per-cell admission rate against null is very high (cells are statistically real), but "above null" is not the same as "generalizes forward" when example budget is small relative to candidate pool.

**Random-subsample consensus does not fix this.** Drawing N independent random 80% subsets of examples and intersecting kept-cells reduces tautologically to the same set: a cell where 100% of all 45 agree also has 100% agreement in any subset of those 45; a cell where one example disagrees fails enough random subsets to drop out of the intersection. Either way, random-consensus equals the full-set 100%-agreement set.

**Takeaway.** Sign-coherence on a high-dimensional cell pool overfits regardless of ensemble approach when 100%-agreement is the selection rule. To use the expression cache for a forward-stable filter, dimensionality must be reduced first (structural pre-selection of expressions) or a different selection rule must be used.

### 3.8 Pyramid walk-forward — disqualified as operational closer (2026-04-27)

**Test.** Single chronological 30/15 split. Train pyramid grinder on first 30 BF examples (entry dates 2020-05 through 2025-08), test whether the 15 held-out examples (2025-08 through 2025-12) appear in pyramid's wild-signal output. Same params as the existing pyramid_bf_mp_sig582 run. Output redirected to a worktree directory to skip Railway mirror.

**Result.** Zero of 15 held-outs appear in pyramid signals. Forward false-rejection = 100%. Pyramid does not generalize across an ~8-month gap in training data.

**Implication.** Pyramid intersection numbers in the spec (e.g., §4.6's BF = 87 over 5.5y) reflect pyramid's agreement with training-set examples, not forward-validation. Treating pyramid as an operational closer is unsafe — its conditions are too narrow. The existing universe-subsample consensus pipeline addresses one kind of overfit (universe-condition fit) but not example-level overfit. **Pyramid example-subsample consensus is the open hypothesis** — see §7.

---

## 4. Bbox direction (2026-04-21, superseded for forward deployment 2026-04-27)

> **Note (2026-04-27):** §4 walk-forward FR is 73–90% across setups (per §6.1 of the prior session's spec, now folded into §3.6 history above). The §4 bbox does not generalize forward; it relies on `[nanmin, nanmax]` numerical bounds derived from individual examples (point memorization). The current deployable filter is the sign-coherence MA cells + carve-greedy chain stack — see new §6 below. §4 retained here for historical context and because some of its components (weekly anchor rule, cluster dedup, tradable filter, partial-week patch) survive into §6.



Plain strict-AND across every (MA type, scale, bar offset) cell, with each cell's band = `[nanmin_i lr_i, nanmax_i lr_i]` over the example set. No σ. No cell ranking. No LOO. No walk-up driver. Output keyed by signal bar (E−1). Tradable filter applied at the signal bar, examples bypass.

### 4.1 Core construction

For each cell on both scales:

- **Feature per example.** Anchored log-ratio: `log(MA_p[E−k] / MA_p[E−1])` daily; `log(MA_p[W−k] / MA_p[W−0])` weekly. Anchor never E. Weekly W−0 uses partial-week anchor patched at close[E−1] (§3.3 weekly anchor rule).
- **Band per cell.** `Lower(c) = nanmin_i lr_i(c)`, `Upper(c) = nanmax_i lr_i(c)`. No σ. Example trajectories alone define the envelope.
- **Bit-consistent band construction.** Bands are built via the *same* lr-compute code path the scan uses (`scan_lr_single`), not via Phase 1's stored tensors. Rationale: Phase 1 used `np.sum(prior) + anchor_close` (pairwise summation); the scan uses `wc_cumsum[hi] − wc_cumsum[lo] + anchor_close` (sequential summation). These are mathematically equivalent but differ by ~1 ULP at the 15th decimal. Examples at band edges would fail their own bands by 1e-15 under the Phase-1-built bands. Consistent compute path eliminates this.
- **Candidate check at cell.** Candidate's own anchored log-ratio at the cell sits in `[Lower, Upper]`. NaN-lenient: NaN candidate lr OR NaN band → auto-pass.
- **Filter = strict-AND** across every cell (~1,500 per setup). No ranking, no selection.

### 4.2 Cell space

- **Daily MA types (21 total):** SMA at `{5, 8, 10, 13, 20, 30, 50, 100, 150, 200}` (10 periods) + EMA at `{3, 5, 8, 10, 13, 20, 30, 50, 100, 150, 200}` (11 periods; 3-period EMA included as a short-trend axis).
- **Weekly MA types (15 total):** SMA at `{5, 8, 10, 13, 20, 30, 50}` + EMA at `{3, 5, 8, 10, 13, 20, 30, 50}`.
- **Daily offsets:** `k = 1..N_daily` (tensor index i = k−1, i=0 is anchor). N_daily per setup from `research/n_derivation_cache/{setup}_summary.json` field `N_bars`.
- **Weekly offsets:** `k = 0..W_N−1` (i=0 is anchor W−0). W_N per setup from `research/presignal_weekly_stage_a/{setup}_stage_a.json` field `W_N`.
- **Trivially-passing cells.** Daily k=1 (anchor) and weekly k=0 (anchor) produce log-ratio 0 by construction for both examples and candidates; band = `[0, 0]`; always passes. The scan skips weekly k=0 explicitly for efficiency.

### 4.3 Tradable filter at signal bar

Per SIGNAL_GRINDER.md §Per-bar tradable filter — a bar is tradable iff:
- `close ≥ $1.00`
- `20-day average dollar volume ≥ $4,000,000` (read from df `dvol_20d` column; falls back to computing rolling `mean(close * volume)` over 20 bars)
- `20-bar ADRP ≥ 1.8%` where ADRP = `(mean(high/low over 20 bars) − 1) * 100`

Applied to the **signal bar E−1** (the bar being output after signal-bar rekey). For candidate entry bar E to pass, `tradable[E−1]` must be True. Examples bypass this check (100% EX invariant per §2.1).

### 4.4 Output keying

Output is keyed by **signal bar = E−1**, not entry bar E. Matches pyramid grinder's `signal_date` field for direct intersection. The internal scan iterates candidate entry bars E; the final cluster reps and pre-dedup pass lists are shifted by −1 before writing.

### 4.5 Example eligibility

Every labelled example's E is forced into the scan's eligibility set regardless of DATE_CUTOFF and regardless of tradable status. This makes 100% EX pass structural, not contingent on market filters. Implementation in `scan_ticker_fast(example_E_list=…)`.

### 4.6 Composition and operational output

Presignal alone carves the 11,842-ticker post-2020 universe to O(10k–300k) signal-bar clusters per setup. That is **diagnostic-only**. The operational output is presignal ∩ pyramid at the same signal bar:

- Pyramid independently produces 500–1,500 signals per setup (already tradable-filtered).
- Historical intersection counts (pyramid signal_bar ∈ presignal cluster-rightmost set) across 5.5 years: HTF 72 (40 non-example), BF 87 (42), BASE 89 (47), DTSS 153 (80), 3-4DB 69 (43). Total **~46 non-example matches/year across all 5 setups**, ~1/week cadence.

Forward-scan pattern: at day close, compute presignal-positive tickers for tomorrow-entry AND pyramid-fires-today. Tickers appearing in both intersected sets are the next-day actionable candidates. Forward runner not yet built — deferred until consensus pipeline is ready.

### 4.7 Why bounding-box alone is the right primitive

- **100% EX pass by construction.** Each example's lr at cell c sits in `[nanmin_j lr_j(c), nanmax_j lr_j(c)]` trivially. No trimming, no softening, no precision edge cases (after the scan-path band build).
- **Axis-aligned per cell.** Each dimension is independent. Candidate must stay inside every cell's 1D band simultaneously — strict-AND. Admission compounds across ~5–7 effective independent axes (§3.4 SVD diagnostic), the remaining ~1,490 cells being near-redundant tumblers tied to the same factors. Effective admission ≈ `p^(eff_rank)` ≈ 0.3^5 = 0.24%.
- **Setup-agnostic.** Same algorithm, per-setup N values, uniform code path.
- **Cheap composition.** Downstream pyramid, refinement, EV grinders operate on the presignal-reduced subset. Pyramid on a pre-filtered candidate pool is drastically faster than universe-wide pyramid and produces tighter results.
- **Room for overfit protection.** Current carve is deliberately loose (no overfit protection) so downstream consensus-style protection mechanisms (permutation null, stability selection across universe subsamples, walk-forward hold-out) can be added without collapsing signal to zero. Overfit protection is a whole-pipeline phase; not presignal-specific.

### 4.8 Status

**Built and validated:**
- Per-example trajectory extraction — `research/presignal_sma_band_extract.py` (`build_ticker_cache`, `scan_lr_single`, `build_tradable_mask`).
- Universe scan with bit-consistent bands, tradable gate, signal-bar keying — `research/presignal_sma_band_scan.py`.
- Pre-flight EX check (examples must pass freshly-built bands); scan aborts if violated.
- Pyramid alignment — `research/presignal_sma_band_pyramid_align.py`. Maps pyramid `signal_date` to presignal signal bar, reports coverage/precision/lift.
- Visualization — `research/presignal_sma_band_viz.py` (single-SMA, HTF×SMA10), `research/presignal_sma_band_option1_viz.py` (no-σ comparison).

**Accepted tradeoffs:**
- Axis-aligned box is looser than convex hull of examples. Candidate can pass every cell at a combination of values no single example produced. Accept for simplicity.
- BASE/DTSS remain wide because their example sets span multiple regimes. Downstream grinders carve further.
- Examples bypass tradable filter — an example with a non-tradable entry day still contributes to bands and still passes. Artifact of 100% EX invariant; if labelling data is clean this is a non-issue.

### 4.9 Open items

- **Curve-fit / overfit protection phase** — deferred. Likely a whole-pipeline mechanism, not presignal-specific.
- **Forward-scan runner** — deferred until consensus pipeline is ready.
- **Handoff to classifier labeler** — see `CLASSIFIER_SPEC.md §15`. Classification logic lives there; the presignal grinder's role ends at emitting the pool.

---

## 5. Implementation files and outputs

### 5.1 Current (§4) implementation

- `research/presignal_sma_band_extract.py` — core feature helpers: `build_ticker_cache` (daily+weekly log-MAs, rolling SD-of-log-MAs, weekly indexing, tradable mask), `scan_lr_single` (single-bar scan-path lr extraction for band consistency), `build_tradable_mask` (3-criteria per-bar tradable filter). Per-setup Phase 1 trajectories saved to `research/presignal_sma_band/{setup}_trajectories.pkl`.
- `research/presignal_sma_band_scan.py` — universe scan: vectorized `scan_ticker_fast` (strict-AND over all cells, NaN-lenient, tradable-gated, example-bypass), bands built via `build_bands_from_examples` with pre-flight EX check, signal-bar rekey, §5.2 cluster dedup. Per-setup output to `research/presignal_sma_band_scan/{setup}_passes.pkl`.
- `research/presignal_sma_band_pyramid_align.py` — pyramid alignment: loads latest `pyramid_{setup}_mp_sig*.json` from `local_runner/cache/`, picks tier matching `summary.final_total`, maps `signal_date` to bar index, reports coverage/precision at pre-dedup and post-dedup levels.

### 5.2 Legacy / shared utility files (§15 / §3.4-era, still valid)

- `research/visual_shape_compare.py` — daily F1 module (hull utilities, NaN-lenient `check_F1_batch`, `scan_ticker`, `dedupe_clusters`). §4 reuses `lookup_idx`, `dates_as_str`, `dedupe_clusters`.
- `research/location_axis.py` — Location descriptor module (§15).
- `research/presignal_grinder_all.py` — runs §15 F1 + Location per setup.
- `research/presignal_weekly_aggregate.py` — weekly aggregation (close[E-1] anchor, per-ticker indexing). §4 reuses `build_ticker_weekly_indexing`.
- `research/presignal_weekly_stage_a.py`, `research/presignal_weekly_stage_b.py` — §15 weekly pass + daily∩weekly intersection.
- `research/n_derivation_cache.py` — per-setup N derivation on expression cache. §4 reads `N_bars` from its per-setup JSONs.
- `research/presignal_ma_corridor.py` — §3.4 MA feature extraction (superseded by §4's `presignal_sma_band_extract.py`).

### 5.3 §3.5 σ-cloud/LOO-walk-up rejected artifacts

- `research/presignal_sma_band_viz.py` — single-SMA overlay with ±σ clouds and candidate-overlay verdicts for HTF×SMA10. Used to build intuition for §3.5.
- `research/presignal_sma_band_option1_viz.py` — 2x2 comparison of σ-clouds vs no-σ bands for HTF×SMA10 daily and BASE×SMA5 weekly. Led to dropping σ.
- `research/presignal_sma_band_ranker.py` — global cell ranker + walk-up driver + boundary detection. Diagnosed the LOO-drop-before-elbow ordering inversion.
- `research/presignal_sma_band_nosigma_test.py` — A/B test of σ vs no-σ on same 5000-bar market sample. Showed HTF/BF/3-4DB hit ≤2% admission with no-σ; BASE/DTSS still 9–20%.
- `research/presignal_sma_band_ex_diag.py` — diagnosed the 1e-15 float-precision gap causing band-edge example failures.
- `research/presignal_sma_band_diag_base_weekly.py` — diagnosed σ blow-out from volatile examples on BASE short-term weekly cells.

### 5.4 Diagnostic files (§15 / §3.4)

- `research/presignal_hull_and_keep_diagnostics.py` — §15 hull-contributor and per-descriptor keep-rate diagnostic.
- `research/presignal_weekly_diagnostics.py` — §15 anchor-pos reach distribution.
- `research/presignal_ma_shape_test.py` — §3.4 entry-bar-only MA-vector separation test.
- `research/presignal_ma_heatmap.py`, `research/presignal_weekly_ma_heatmap.py` — §3.4 per-cell reject-rate heatmaps.
- `research/presignal_ma_corridor_diag.py` — §3.4 effective-rank / SVD diagnostic.

### 5.5 Output directories

- `research/presignal_sma_band/` — §4 Phase 1 trajectories per setup.
- `research/presignal_sma_band_scan/` — §4 universe scan passes (signal-bar keyed) and pyramid alignment JSON.
- `research/presignal_sma_band_ranker/` — §3.5 walk-up diagnostics (rejected).
- `research/presignal_grinder_all/` — §15 daily outputs per setup.
- `research/presignal_weekly_stage_a/`, `research/presignal_weekly_stage_b/` — §15 weekly and daily∩weekly outputs.
- `research/presignal_diagnostics/` — §15 diagnostic results.
- `research/presignal_ma_heatmap/`, `research/presignal_weekly_ma_heatmap/` — §3.4 heatmap data.
- `research/presignal_ma_shape_test/` — §3.4 entry-bar test results.
- `research/presignal_ma_corridor/` — §3.4 per-setup selection JSONs.

### 5.6 Next work phases

1. **Pyramid example-subsample consensus pipeline** — see §7. Replaces the prior "pyramid pipeline optimization" (now folded in) and the prior "whole-pipeline overfit protection phase" (partially addressed by §6 sign-coherence + chain; the rest is in §7).
2. **Replicate §6 stack on HTF and BASE** — same setup-agnostic algorithm with per-setup parameters from data. DTSS / 3-4DB are fade-class and out of scope.
3. **Handoff to classifier labeler** — gated on §7. Tighter pre-classifier pool (§6 stack ∩ §7 consensus pyramid) should improve labeler separation. Labeler mechanic spec'd in `CLASSIFIER_SPEC.md §15`.
4. **Forward-scan runner** — post-close script that evaluates §6 stack for tomorrow-entry AND consensus pyramid fires today, intersects, emits tomorrow's watchlist. Gated on §7 validation.

---

## 6. Current direction — sign-coherence MA cells + carve-greedy chain stack (2026-04-27)

The deployable presignal filter for BF is the stack of two forward-stable mechanisms. Each is binary, threshold-free (zero is the only threshold), and validated chronologically. Both replace the §4 bbox for forward use; the §4 cells survive in concept (sign-only version embedded into §6.1 below).

### 6.1 Sign-coherence MA cells

For each (MA period × weekly offset × sign-test-type) cell, keep the cell only if 100% of BF examples agree on the sign at that cell. Threshold = zero (structural, not example-derived). Sign tests:

- `close vs MA` — was close above or below MA at offset k weeks before entry.
- `MA short vs MA long` — was short MA above long MA at offset k.
- `MA slope (1-week)` — was MA[t] above MA[t-1] at offset k.
- `MA trajectory` — sign of MA[anchor-k] vs MA[anchor]; the §4 cell, sign-only version.

Each surviving cell is binary at a specific weekly offset before entry. Per-offset anchoring gives the filter a natural "this is happening now" property the chain alone lacks.

NaN-lenient: an example or candidate with NaN at a cell auto-passes that cell. All-finite gate on examples (n_finite == n_ex) at filter-build time prevents short-history examples from creating spurious cells. Per-example pre-flight self-pass check before scan.

### 6.2 Carve-greedy chain (4 events on BF)

State-machine chain over universal weekly events, built carve-greedy under 100% example pass. For BF, captures the new-k-week-high cascade structure (20w, 10w, 5w highs into entry) plus a state transition. Chain length is structurally capped by the breakout-at-anchor mechanic (§3.6); carve-greedy build optimization doesn't extend it further. Forward-stable on its own via chronological hold-out.

### 6.3 Stack mechanic

Both filters must pass for a candidate to admit. The two are mostly independent (correlation ratio close to 1 in compound-admission tests), so combined carve compounds. Stack walk-forward FR is approximately the sum of each filter's failures (the two filters fail on different examples), which sits modestly above the ≤10% target — the trade-off for adding the chain's complementary signal on top of the MA-only filter.

### 6.4 What survives from prior architectures

- Weekly anchor rule (§3.3): anchor week truncated at close[E-1]. Reused unchanged.
- §5.2 cluster dedup logic: applied to stack output before consumer.
- Per-setup `N_daily` and `W_N` from `research/n_derivation_cache/` and `research/presignal_weekly_stage_a/`. Reused unchanged.
- §4.3 tradable filter at signal bar E-1: same three criteria, same threshold values, applied to the stack output.
- §4.5 example eligibility: every labelled example's E forced into the eligibility set regardless of date cutoff or tradable status.

### 6.5 What this filter is NOT

- **Not pyramid-validated.** Earlier "pyramid intersection" numbers reflect agreement with a curve-fit pyramid that itself fails forward (§3.8). Stack deployment relies on the stack alone, not on pyramid intersection as a closer.
- **Not at the few-per-week operational target.** Standalone admission across the universe is too high to deploy without a downstream forward-validated closer. The natural closer would be a forward-validated pyramid — see §7.
- **Not setup-portable verbatim.** §6 was derived on BF only. HTF and BASE need their own runs (same algorithm, per-setup parameters from data; setup-agnostic code path per §2.1). DTSS / 3-4DB are fade-class setups and out of scope per `feedback_classifier_breakout_scope` memory.

### 6.6 Implementation files

In `presignal-quality-research` worktree, `research/`:
- `bf_weekly_universality.py` — universal-event filter (events firing in 100% of BF examples in their W_N=44 weekly lookback).
- `bf_chain_carve_greedy_weekly.py` — carve-greedy chain build (max marginal universe-rejection per step under 100% example pass).
- `bf_chain_stop_diagnostic.py` — chain-length-cap diagnostic (per-example end-bar tally, candidate-blocking analysis).
- `bf_chain_triangulate.py` — pyramid signals through chain + chain-end-bar distribution.
- `bf_sign_coherence_weekly.py` — sign-coherence cells (close-vs-MA, MA-vs-MA, MA-slope, MA-trajectory) and admission test.
- `bf_sign_coherence_walkforward.py` — chronological hold-out FR for sign-coherence.
- `bf_walkforward_chain_stack.py` — chronological hold-out FR for chain, sign-coherence, and stack.
- `bf_stack_test.py` — chain × sign-coherence stack admission test (examples, samples, pyramid signals).
- `bf_pyramid_walkforward.py` — pyramid 30/15 chronological hold-out (§3.8).
- `bf_expr_trajectory_filter.py` / `bf_expr_trajectory_walkforward.py` / `bf_expr_trajectory_random_consensus.py` — failed expression-cache extension (§3.7).

Result JSONs in same dir; counts/dates live there, not in this doc.

### 6.7 Rejected a priori (extending §2.2)

- **Point-value envelopes on cell values** (bbox, σ-envelope, hulls). Walk-forward demonstrated they don't generalize at the example budget available — §3.6 and §6.1 confirm.
- **Threshold-defined behaviors** in any cell or chain event. Behaviors must be transitions or comparisons against zero, not range membership.
- **Universe-subsample consensus alone as overfit protection.** The existing pyramid pipeline does this, and pyramid still fails 100% forward (§3.8). Universe-subsample addresses a different overfit mode than what's actually killing forward generalization.
- **Random example-subsample consensus on sign-coherence cells.** Tautologically equals the full-set 100%-agreement filter (§3.7).

The §6 stack is the current best presignal model. Verified this session via independent re-eval.

---

## 7. Pending build — pyramid example-subsample consensus pipeline

Pyramid's 100% forward-FR (§3.8) disqualifies the existing pyramid pipeline as operational closer. The deployable §6 stack is forward-stable but admits too much for standalone deployment without a closer. The hypothesis to test next is **example-subsample consensus** on pyramid: many runs on different random 80% subsets of examples, conditions intersected across runs. Analogous to the existing universe-subsample consensus pipeline but along the example axis, which is where the overfit lives.

### 7.1 Plan

1. **Pre-filter universe via §6 stack** for speed. Each pyramid run operates on the subset that passes the stack instead of the full universe, cutting per-grind cost substantially.
2. **Run pyramid N times** (initial N ≈ 10) with different random 80% subsets of the BF example set. Use the existing `override_example_dfs` parameter to `run_pyramid()`. Output redirected to a worktree directory (skips Railway mirror).
3. **Intersect conditions across the N runs.** Conditions surviving in all (or most) subsets are example-set-robust.
4. **Validate the intersected conditions via walk-forward chronological hold-out** — same 30/15 split used for the disqualifying test in §3.8, plus more folds if needed.

### 7.2 Success criteria

- Pre-filter must be lossless on examples (§2.1's 100% example pass) — already validated for the §6 stack.
- Walk-forward held-out coverage substantially above zero (the §3.8 baseline). Target: ≥80% held-out coverage, i.e. ≤20% FR per fold.
- Operational pool size (chosen-conditions-fired bars, post-dedup): in the range the original spec §4.6 targeted (low hundreds per setup over 5.5y).

### 7.3 What's not yet tested

- Whether pyramid's beam search is sensitive enough to example-subsample variation to actually produce different conditions per run. If pyramid converges to the same conditions regardless of which 80% is used, consensus won't help and a different mechanism is needed.
- Whether 80% is the right subset fraction. Smaller subsamples produce more diverse conditions per run but fewer survive the intersection. Tune from initial runs.
- Whether the §6 pre-filter introduces forward bias when stacked with consensus pyramid. Safe guess is no since pre-filter is example-pass-by-construction and pyramid wouldn't see filtered-out bars, but worth verifying with a pre-filter-off control.
- Whether the pipeline replicates on HTF and BASE with the same algorithm (§2.1's setup-agnostic invariant). Run BF first; if it works, replicate.
