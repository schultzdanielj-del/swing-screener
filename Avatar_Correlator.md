# Avatar Correlator — Component Spec

Authoritative doc for the chart-shape candidate generator. Replaces the presignal grinder + pyramid grinder for setup matching. Upstream consumer of the curated example bank; produces the ranked candidate list that the classifier (entry-candle outcome attachment) and downstream EV grinder (regime conditioning) operate on.

The component is at research stage. No spec or build yet. The architectural principles in §3 are committed; the open design questions are in §5.

---

## 1. Purpose

A chart setup is pivot geometry in ADR units — bar counts and ADR-normalized price moves between structural pivot points. The Avatar Correlator builds a single canonical reference (the **avatar**) from a curated bank of flawless example setups, then scores every universe window by similarity to that avatar.

Output is a continuous similarity score per candidate, sorted descending. There is no admit/reject decision inside this component. Every universe bar is scored; the watchlist cutoff happens downstream on the joint conditional EV surface.

The component is **setup-agnostic** (same algorithm, different bank) and **direction-agnostic** (long/short carried by the sign of bank pivot moves, not by code).

Two-phase architecture:

- **Phase 1 — Avatar construction.** Decompose each bank example into its pivot sequence (ordered `(bars, ADR_distance, direction)` steps), aggregate across the bank into a reference avatar.
- **Phase 2 — Universe scoring.** Apply the same pivot decomposition to universe windows, score each candidate by similarity to the avatar, output ranked list.

The principle that separates this component from the prior pyramid + presignal stack: **zero bank-derived parameters in the scoring function.** The similarity metric is fixed; the bank supplies anchor positions only. The pyramid + presignal failure mode — parameters tuned against examples that don't generalize forward — is structurally absent.

What this component does NOT do:

- Filter or threshold candidates
- Attach entry-fired or W/L outcomes (classifier's job)
- Apply regime conditioning (EV grinder's job)
- Pick the watchlist cutoff (joint conditional EV surface, downstream of this component)

---

## 2. EXACT spec

Single-anchor approach-A probe at `research/avatar_phase_probe.py`. Setup-agnostic recognizer reads phase boundaries from the Google Sheet "Setup phases" and applies identical math to every phase regardless of phase-type label.

### 2.1 Bank entry shape

Per setup, one bank entry specifies:
- ticker, asof_date, entry_date (asof + 1 trading bar)
- n_bars (window length in trading bars; natural span from setup-start to asof)
- klass: `breakout` | `fade` | `parabolic` (drives §3.10 class filter dispatch)
- phase_boundary_dates: list of K−1 internal boundary dates from the spreadsheet
- phase_types: list of K labels (uptrend, pullback, range, retrace) — metadata only, no control flow

### 2.2 Per-phase signature (6 dims, generic)

For each phase k, identical formula:

1. `length_frac` = phase_bars / window_bars
2. `end_level` = log(close[phase_end] / close[window_start])
3. `max_level` = log(close[phase_max] / close[window_start])
4. `min_level` = log(close[phase_min] / close[window_start])
5. `directness` = |end_log − start_log| / Σ|delta_log| over phase, in [0, 1]
6. `argmax_position` = (bar_index_of_max − phase_start) / phase_length, in [0, 1]

Total signature dim = 6 × K. No setup-specific math; bank values per setup encode the expected pattern (uptrend phases have argmax≈1, pullback phases have argmax≈0, ranges have low directness, etc.).

### 2.3 Distance metric

Plain Euclidean over the per-phase signature. No bank-derived weights, no z-scoring, no bank-supplied normalization factors. Bank examples supply target positions only (per §3.4).

### 2.4 Candidate-side fit (avatar-template DP segmentation)

Per candidate window, search over phase-boundary positions for the K-phase split that minimizes Euclidean distance to the bank's signature. K and phase types are taken from the bank entry; the candidate has no static decomposition.

DP search complexity: O(n²) per candidate for K=3, O(n) for K=2, O(1) for K=1. Numba @njit'd inner loops with precomputed range-max/min/argmax matrices and prefix-path arrays for log-return.

### 2.5 Hard structural constraint inside DP fit

For K ≥ 2 setups, skip splits where `phase_0_max < close[asof]`. Kills candidates whose right-side close is above the leftside peak (DTSS = right side broke above leftside peak; BASE = already broken out of range).

For K = 1, the constraint is trivially true (phase 0 is the whole window).

### 2.6 Tradable mask

Per-bar checks (compute_tradable):
- close ≥ TRADABLE_MIN_PRICE
- 20-bar rolling ADRP ≥ TRADABLE_MIN_ADRP
- 20-bar dollar volume ≥ TRADABLE_MIN_DVOL
- close × shares_outstanding ≥ TRADABLE_MIN_MCAP (when shares data available)

Per-window check: ALL bars in candidate window have close ≥ TRADABLE_MIN_PRICE (catches "started as penny stock then ripped" cases that an asof-only check passes through).

### 2.7 Per-ticker eligibility

From `local_runner/cache/fundamentals_cache.json`:
- Skip ticker if industry ∈ EXCLUDED_INDUSTRIES (currently {"Biotechnology"}).
- Tickers without fundamentals data (typically ETFs/ETNs) pass through; market cap check is skipped when shares_outstanding is missing.

### 2.8 Class filters (§3.10)

- `breakout` (BF, BASE): close[asof] ≤ AVWAP[asof] anchored at argmax-high bar.
- `fade` (DTSS, 3-4DB): close[asof] ≤ max-high over [start, asof − FADE_EXCLUDE_LAST]. See §4 for known leak.
- `parabolic` (PARS): no class filter (signature's argmax_position handles peak-at-end requirement).

### 2.9 Pipeline

Two-stage parallel:
1. Sequential pre-pass: per ticker, apply tradable mask + class filter + finite-window check. Build flat `closes_flat` array and per-eligible `eligible_end_idxs` array (in flat-array index space).
2. Numba `prange` DP fit over all eligible candidates in parallel (`parallel_dp_fit_{1,2,3}phase`).
3. Sequential aggregation: per-ticker greedy non-overlap dedup; global heap to TOP_N.

### 2.10 Outputs per setup

Written to `research/avatar_viability_bank/`:
- `<setup>_phase_probe_results.json` — bank metadata + top-N ranked candidates with fit boundaries.
- `<setup>_phase_probe_top<N>.png` — 5×6 grid of log-close overlays (bank avatar in black α=0.35, candidate in blue, vertical lines at bank boundaries gray solid + candidate DP-fit boundaries blue dashed).

### 2.11 HTF structural post-filter

After per-anchor z-score combine and top-N selection, HTF candidates pass through 9 hard structural rules. Implemented as `htf_post_filter()` in `research/avatar_probe_from_db.py`, gated on `setup == "htf"`. Other setups pass through untouched.

Each candidate's ignite/flag phase boundaries are read from its anchoring bank's `phase_types` combined with the candidate's DP-fit boundaries. ADR(30) at the candidate's asof bar. "NaN-exempt" rules pass when input data isn't computable (insufficient history).

1. Ignite move ≥ 2.0 ADR. `(close[last_ignite] − ref) / ADR(30)`. ref = `close[bar_before_ignite]` when available, else `open[first_ignite_bar]` (IPO).
2. Flag-pullback ratio ≤ 0.6. `((ignite_high − min_low_in_flag) / ignite_high) / ((close[last_ignite] − ref) / ref)`.
3. No close in flag phase above ignite_high.
4. 30-bar pre-ignite lookback. No close ≥ ignite_high in the 30 bars before first ignite bar. Exempt when ignite is phase 0 (no pre-history).
5. `close[last_ignite] > D1 SMA(200)`. NaN-exempt.
6. `close[last_ignite] ≥ D1 SMA(50) × 0.97`. NaN-exempt.
7. `close[last_ignite] > D1 SMA(330)`. NaN-exempt.
8. `close[last_ignite] > weekly EMA(8)`. W-FRI resample from daily, EMA `adjust=False`. NaN-exempt below 30 weekly bars.
9. `close[last_ignite] > weekly EMA(21)`. Same resample. NaN-exempt.

PNG and JSON renderers consume the filtered list — only survivors are saved.

### 2.12 DTSS structural post-filter

After per-anchor z-score combine and top-N selection, DTSS candidates pass through 9 hard structural rules. Implemented as `dtss_post_filter()` in `research/avatar_probe_from_db.py`, gated on `setup == "dtss"`.

Each candidate's pullback/retrace boundaries are read from the anchoring bank's `phase_types` (uptrend/pullback/retrace) combined with the candidate's DP-fit boundaries. ADR(30) at the candidate's asof bar. NaN-exempt rules pass when input data isn't computable.

1. `max(high in retrace) ≤ max(high over uptrend+pullback)`. Leftside = phases 0+1 combined; rightside = the retrace phase.
2. Bowl depth ≥ 4.0 ADR. `(high[pullback_start] − low[retrace_start]) / ADR(30)`.
3. Retrace fraction ≥ 0.6. `(close[asof] − low[retrace_start]) / (high[pullback_start] − low[retrace_start])`.
4. `close[asof] > D1 SMA(200)`. NaN-exempt.
5. `close[asof] > D1 SMA(50)`. NaN-exempt.
6. `close[asof] ≥ D1 SMA(330) × 1.15`. NaN-exempt.
7. `close[asof] > weekly EMA(8)`. W-FRI resample, EMA `adjust=False`. NaN-exempt below 30 weekly bars.
8. `close[asof] > weekly EMA(21)`. Same resample. NaN-exempt below 30 weekly bars.
9. `close[asof] ≥ weekly SMA(50) × 1.15`. NaN-exempt below 50 weekly bars.

PNG and JSON renderers consume the filtered list — only survivors are saved.

### 2.13 BF structural post-filter

After per-anchor z-score combine and top-N selection, BF candidates pass through 12 hard structural rules. Implemented as `bf_check_candidate()` in `research/avatar_probe_from_db.py`, gated on `setup == "bf"`.

Each candidate's uptrend/flag boundaries are read from the anchoring bank's `phase_types` (uptrend → flag) combined with the candidate's DP-fit boundaries. ADR(30) at the candidate's asof bar. Daily EMA8/EMA21 from `closes[:end_idx+1]` with `ewm(span=N, adjust=False)`. Weekly W-FRI resample, EMA `adjust=False`. NaN-exempt rules pass when input data isn't computable.

1. No close in flag phase > `high[flag_first_bar]` (strict).
2. `(flag_start_high − uptrend_start_low) / ADR(30) ≥ 3.5`.
3. `((flag_start_high − min_low_in_flag) / flag_start_high) / ((flag_start_high − uptrend_start_low) / uptrend_start_low) ≤ 0.6`.
4. `max((D1 EMA21 − close) / ADR(30))` over flag phase ≤ 0.7.
5. `max((D1 EMA8 − close) / ADR(30))` over flag phase ≤ 1.5.
6. `close[asof] ≥ D1 SMA(200) × 1.20`. NaN-exempt below 200 daily bars.
7. `close[asof] > D1 SMA(50)`. NaN-exempt below 50 daily bars.
8. `close[asof] ≥ D1 EMA(8) × 0.95`.
9. `close[asof] ≥ D1 EMA(21) × 0.95`.
10. `close[asof] > weekly EMA(8)`. NaN-exempt below 30 weekly bars.
11. `close[asof] > weekly EMA(21)`. NaN-exempt below 30 weekly bars.
12. `close[asof] ≥ weekly SMA(50) × 1.15`. NaN-exempt below 50 weekly bars.

PNG and JSON renderers consume the filtered list — only survivors are saved.

### 2.14 BASE structural post-filter

After per-anchor z-score combine and top-N selection, BASE candidates pass through 8 hard structural rules. Implemented as `base_check_candidate()` in `research/avatar_probe_from_db.py`, gated on `setup == "base"`.

Each candidate's window spans the uptrend → range phases (2-phase taxonomy from the bank). ADR(30) at the candidate's asof bar. Daily EMA8/EMA21 from `closes[:end_idx+1]` with `ewm(span=N, adjust=False)`. Weekly W-FRI resample, EMA `adjust=False`. NaN-exempt rules pass when input data isn't computable.

1. **Shallowest H- algo line.** Find all H- algo lines (per `algo_line_detector.py`, raw output without dedup so per-asof shallowest selection isn't biased by post-asof touches) whose origin sits within the candidate's window `[end_idx − n_bars + 1, end_idx]`, whose second touch is at or before asof, and which are unbroken through asof-1 (`broken_idx == -1` or `broken_idx == end_idx`). The shallowest such line (smallest `|slope_per_bar|`) must not have `close[asof] > line_value_at_asof`. No qualifying H- line = pass.
2. `(close[asof] − MA) / ADR(30) ≥ −1.0` for each of D1 SMA200, D1 SMA50, D1 EMA8, D1 EMA21, weekly EMA8, weekly EMA21, weekly SMA50. NaN-exempt per-MA when insufficient history.
3. `(close[asof] − D1 EMA8) / ADR(30) ≤ 2.0`.
4. `(close[asof] − D1 EMA21) / ADR(30) ≤ 2.0`.
5. `(close[asof] − D1 SMA50) / ADR(30) ≤ 3.0`. NaN-exempt below 50 daily bars.
6. `(close[asof] − weekly EMA8) / ADR(30) ≤ 2.0`. NaN-exempt below 1 weekly bar.
7. `(close[asof] − weekly EMA21) / ADR(30) ≤ 4.0`. NaN-exempt below 1 weekly bar.
8. `(close[asof] − D1 SMA330) / ADR(30) ≥ 1.0`. NaN-exempt below 330 daily bars.

PNG and JSON renderers consume the filtered list — only survivors are saved.

---

## 3. Details you need to know

Architectural principles. Any design proposal in §5 must respect these.

### 3.1 Bank curation discipline — add only flawless

The bank holds only flawless instances of the setup. Mediocre examples pull the avatar toward "no-man's-land" — a centroid that resembles none of the actual setup variations.

This is the inverse of conventional ML data discipline. Examples here are not i.i.d. samples from a distribution to characterize; they are imperfect attempts at an ideal to define. Mediocre examples are *anti-data* — they actively degrade the avatar by pulling its position off the bullseye.

Operational rules:

- Add only flawless examples to the bank.
- A candidate that "kinda looks like" the setup does not belong in the bank, even if it's a real instance. It belongs in the ranker output, not the reference set.
- Bank size is not a quality criterion. Small-and-flawless beats large-and-noisy.

### 3.2 Pivot geometry is the feature space

Chart shape is not an open feature-engineering problem. The setup's structural language is pivot-to-pivot ADR distances on the price axis and bar counts on the time axis. Reward and risk are themselves pivot ADR distances — the entire trade decision is geometric.

Alternative representations (Z-norm trajectory, image hashing, compression distance, expression-cache features) are either lossy compressions of the underlying geometry or detours through abstractions that have nothing to do with the trade structure. They are rejected as the primary representation.

The remaining structural choice is pivot detection convention. See §5.1.

### 3.3 Output is a ranker, not a filter

Every universe bar gets a score. Nothing is excluded by the Avatar Correlator. Top-K cutoff happens downstream on the joint conditional EV surface, where the data draws the contour empirically.

This is the structural protection against the pyramid + presignal failure mode. Filters require thresholds. Thresholds require parameters. Parameters tuned against the bank produce "fits the bank, not the setup" overfit. A pure ranker has no parameter to fit and no threshold to overfit.

### 3.4 Zero bank-derived parameters in the scoring function

The similarity metric is fixed before the bank exists, or derives only from structural choices that do not look at bank values. The bank supplies anchor positions; nothing else.

Drop one bank example, recompute the avatar, and the similarity function itself does not change. Only the avatar's position shifts — slightly, if the bank is reasonably tight. This is the structural property that makes the architecture overfit-resistant.

### 3.5 Calibration test is LOO ranking percentile

The relevant calibration measure is: drop bank example E_i, rebuild the avatar from the remaining bank, compute E_i's rank percentile in universe scoring. If E_i lands in the top 0.1% across LOO samples, the avatar generalizes.

This is NOT the same as filter pass-rate LOO — the test that pyramid and the 50-engine session failed. Filter pass-rate LOO measures threshold-overfit. Ranking percentile LOO measures avatar coherence. The two can diverge: an example that fails its own filter under threshold-LOO can still land in the top 0.1% under ranking-LOO.

### 3.6 Cutoff is a contour on the joint EV surface, not a single number

The watchlist cutoff is empirical and lives downstream of this component. The classifier attaches entry-fired and W/L outcomes to historical match candidates. The EV grinder attaches regime scores. The joint conditional EV surface (shape similarity decile × regime decile) is computed over thousands of historical W/L outcomes; the cutoff is the EV-zero contour on that surface.

In high-regime cells the contour cuts at lower shape similarity (good market lets sloppier charts in). In low-regime cells the contour cuts at higher shape similarity (bad market requires tighter match). The regime-shape interaction is a property of the data, not a rule to encode.

The Avatar Correlator's job ends at the ranked output. Cutoff selection is a downstream composition concern.

### 3.7 Two data sources, two roles

- **Bank.** Small, hand-curated, defines the bullseye. Statistical role: anchor reference. Quality matters; quantity does not. With N=13 the mechanism functions; the avatar's confidence is N-bounded but the math does not break.
- **Universe match-set.** Large, populated by the ranker, W/L-classified by the classifier downstream. Statistical role: edge curve estimation. Provides thousands of historical W/L outcomes, so the joint EV surface has high statistical power even when the bank is tiny.

The bank does NOT need to be statistically representative of all setup variations. It needs to be a clean reference. Variation is captured by the universe match-set.

### 3.8 Setup-agnostic, direction-agnostic

Same algorithm runs against any bank. Per-setup behavior emerges from per-setup bank content. No per-setup or per-class branches in control flow.

Direction (long vs short) is carried by the sign of pivot ADR moves in the bank. Long banks produce avatars with one step-direction pattern; short banks produce the mirror. The algorithm does not read direction labels.

### 3.9 Rejected a priori

- **Filter-with-threshold architectures.** Any architecture that imposes a similarity cutoff inside this component reintroduces the pyramid + presignal failure mode.
- **Bank-derived scoring parameters.** Any scoring function whose coefficients, weights, or thresholds are tuned against bank values.
- **Per-setup branches in control flow.** Per-setup parameters differ because data differs; the algorithm is uniform.
- **Multi-output tiers** (tight/medium/loose). One ranked list per setup.
- **Trim-the-bank fallbacks** when an example fails LOO. Failing LOO is diagnostic information about the bank or the metric, not a license to drop examples until the math works.

### 3.10 Setup-class structural filters

Filters that cut candidates lacking the setup's structural posture, applied before scoring. Direction-derived from the avatar's class, not from setup-specific branches.

- **Breakout class** (BF, BASE, HTF). Pass if `close[asof] ≤ AVWAP[asof]`, where AVWAP is anchored at the argmax-high bar in the window. The breakout has not fired yet at the asof bar; price is still under resistance/AVWAP. A chart where close > AVWAP has already broken out (too late) and is excluded.
- **Fade class** (DTSS, 3-4DB). Pass if `close[asof] ≤ high[argmax_high_bar]`, where argmax is over `[start, asof-10]`. The last 10 bars near asof are excluded so the argmax picks the *prior* pivot high (the first top), not whatever is forming around the second top. If close[asof] is above that prior pivot, the fade is invalid because price has broken through the level it was supposed to fade off of.

Both filters are structural (sign-only, no tunable threshold). A flawless example always passes its own filter at its own asof bar.

The class label (breakout vs fade) comes from the bank's setup metadata, not a per-setup branch in scoring code. The same code path applies one of the two structural rules based on class.

---

## 4. Known bugs

### 4.1 DTSS metric ranks magnitude-proximity over shape correctness

The 6-dim signature compares absolute log-return levels (end_level, max_level, min_level) per phase. For DTSS, two structurally-different candidates can score similarly:

- A candidate whose retrace fell well short of the leftside peak (failed retrace, structurally invalid as DTSS) but whose absolute uptrend magnitude happens to land near the bank's.
- A candidate whose retrace touched the leftside peak cleanly (structurally correct DTSS) but whose absolute uptrend magnitude is smaller than the bank's.

The metric prefers the first because uptrend-magnitude proximity drives a smaller distance, even though the second is the structurally correct DTSS. The diagnostic feature ("phase 2 max relative to phase 0 max") is implicit but not weighted directly.

Fix in §6: add `phase_max_rel_to_window` per-phase dim = phase_k_max_level / window_max_level. Same formula every phase (generic, no DTSS-specific math). For CELH, both phase 0 and phase 2 reach 1.000; failed-retrace candidates have phase 2 well below 1.0; correct-retrace candidates regardless of absolute magnitude have phase 2 near 1.0.

### 4.2 §3.10 fade filter leak when phase 2 length > exclude_last

The fade filter picks argmax over `[start, asof − FADE_EXCLUDE_LAST]`. If phase 2 (retrace) is longer than `FADE_EXCLUDE_LAST` bars, the argmax can land inside phase 2 (the retrace) rather than on the structural leftside peak (phase 0). Filter then enforces close[asof] ≤ retrace's own argmax — meaningless constraint.

Currently masked by the in-DP-fit structural constraint (§2.5: `phase_0_max ≥ close[asof]`) which is the more accurate enforcement once boundaries are determined. The §3.10 filter remains as a coarse pre-pass but is structurally suboptimal.

Stricter fix in §6: replace `phase_0_max ≥ close[asof]` with `phase_0_max ≥ phase_2_max`. The current constraint passes candidates where phase 2 broke above the leftside peak intra-phase but came back down by asof (right-side chopping above the leftside peak).

### 4.3 Single-anchor metric can't represent multi-flavor setups

With one bank example per setup, the metric optimizes for proximity to that specific bank's magnitude/duration profile. Setups with broader natural variation (e.g., both 3x and 12x BASE patterns) have flavor-mismatched candidates penalized even when shape is correct.

Fix in §6: multi-anchor 1-NN aggregation (per §3.7, §5.2). Each candidate's distance = min over N bank anchors. Per-candidate precompute (rmax/rmin/argmax matrices, prefix_path, levels) is independent of bank example, so refactor amortizes across N anchors.

### 4.4 Distance has no intrinsic interpretation

Per §3.3 the recognizer is a ranker, not a filter. A day where today's #1 distance is small might be "best of strong field" or "best of mediocre field" — same number, different trade decision. The architectural answer is the joint EV surface (§3.6), which requires the classifier + EV grinder downstream and historical W/L outcomes to populate.

Interim fix in §6: per-candidate percentile flag against the empirical distance distribution from full historical scan. Adds context "this distance is at the Pxx of historical candidates" without requiring W/L labels.

### 4.5 Multi-anchor combine mishandles consistent matches

The current combine pipeline (per-anchor z-score normalization, then per-(ticker, asof) min-z dedup) has three structural issues that compound:

- **Per-anchor z-score is apples-to-oranges.** Each bank's raw distance distribution has its own shape (different anchor n_bars, different bank shape, different signature dim values). A z = −2 against one bank means "real outlier match" if that bank's distribution is tight, or "less bad than typical" if wide. The two are treated as equivalent in the combine.
- **Min-z favors anchor-specific outliers over consistent matches.** A candidate that fits all anchors moderately well gets moderate z's everywhere → mediocre min-z. A candidate that scores extremely well against one specific anchor and terribly against the rest gets one great z → wins min-z. Curated bank-quality examples (which by construction match all anchors moderately) lose to single-anchor outliers.
- **The dedup discards information.** When the same (ticker, asof) appears under multiple anchors with z's of −0.5, −0.7, −0.9, −1.1, −1.3, only the −1.3 entry survives. The fact that this candidate also scored well across the other anchors (a stable multi-flavor match) is thrown away.

Effect: curated examples consistently sit at moderate per-anchor z's and lose the ranking race to anchor-specific outliers; per-phase scoring on a clean single-day puts curated examples at top 1-17% of universe but the combine layer pushes them deeper or out of the output entirely.

Fix in §6.7: replace per-anchor z-score → min-z with mean rank percentile across valid-scoring anchors.

### 4.6 Diversity cap is dead code

`avatar_probe_from_db.py` (May 4 `.bak_pre_meanrank`, line ~1117) computes `cap_per_anchor = max(8, COMBINED_TOP_N // 3)`, and the loop at lines 1131–1144 maintains `anchor_count[a_t]` per anchor — but the loop never gates on `anchor_count.get(a_t, 0) >= cap_per_anchor`. The cap exists in name only; the loop appends every passing candidate until it hits `COMBINED_TOP_N`.

Effect: with min-z combine, whichever anchor has the longest negative z-tail on a given asof fills every slot. Confirmed empirically — on `--today` 2026-05-01: HTF 30/30 QUBT, DTSS 20/30 CELH, BF 17/30 DAC, BASE 30/30 ERO.

Likely explains the today-vs-historical asymmetry documented in §7.17. On a "today" date the dominant anchor's pattern is what the universe broadly resembles, so top-30 looks like real setups. On a historical bank-asof, the dominant anchor changes — bank examples surface only if they happen to be in that date's dominant anchor's left tail, which is uncorrelated with shape match to the bank.

Fix: add `if anchor_count.get(a_t, 0) >= cap_per_anchor: continue` after the post-filter check.

Discovered 2026-05-09 while reproducing the May 4 webp images.

**Update 2026-05-09 (cap-fix verification):** the cap fix mechanically works — capped runs show 10/N from any single anchor (vs 30/30 uncapped on HTF and BASE today). However, capped historical scans at bank-asof dates **do not surface bank tickers**: HTF (AFG, OGI at 2021-02-04), DTSS (KYMR, DNLI at 2024-11-08), BF (AA, HYMC at 2025-12-10), BASE (ERO at its own bank_asof 2025-11-21) all remain MISS in top-30 with cap on. The cap was a real bug but is not the cause of the historical bank-surfacing failure. Side effect: HTF capped today produces only 16 survivors (vs 30 uncapped) because the strict HTF post-filter drops most non-QUBT-anchored candidates that the cap forces in.

---

## 5. Pending research

### 5.1 Pivot detection convention

Several conventions exist; the choice affects granularity but not what is measured.

- **N-bar fractal.** Pivot high if `H[t]` exceeds `H[t±1..N]`; symmetric pivot low. Simple, parameterized by N.
- **ZigZag at fixed % threshold.** Walks the bar series, marks pivots at every reversal exceeding X% from the prior pivot. Parameterized by X.
- **ATR-reversal threshold.** Like ZigZag but the reversal trigger is N×ATR rather than %. Self-scales to volatility, removes the fixed-percentage knob.

Selection criteria:

- The same convention applies uniformly to bank and universe — pick once, run identically on both.
- Tolerance to noise: small wiggles should not register as pivots.
- Minimum step size that captures setup geometry without fragmenting steps into dozens of micro-pivots.
- Stability under bank vs universe distribution differences.

Decision pending.

### 5.2 Avatar form

Once Phase 1 produces a sequence of `(bars, ADR_distance, direction)` steps per bank example, the avatar can be aggregated several ways.

- **Single centroid.** Average step counts and ADR distances across the bank. One reference shape; simplest. The natural form when the bank is tightly unimodal. Vulnerable to multi-modal banks producing chimera averages that resemble none of the actual sub-shapes.
- **Centroid + per-step spread.** Centroid plus per-feature IQR or std. Candidates score on weighted deviation per dimension. Captures known variation tolerance.
- **Multi-centroid mixture.** Cluster bank examples in pivot-sequence space (e.g., k-means or DBSCAN on step-vector representation), produce one centroid per cluster, candidates score against nearest. Handles multi-modality if it exists in the bank.
- **Full anchor cloud with 1-NN distance.** No aggregation; bank examples themselves are the avatar; candidates score by distance to nearest bank example. Preserves all variation; structurally has zero bank-derived parameters and the cleanest overfit profile.

Selection depends on whether the curated bank is unimodal. With strict §3.1 curation discipline, the bank should be tight and unimodal, in which case single centroid suffices. If the bank shows multi-modal structure, the cloud approach handles it without committing to a flavor count.

Diagnostic before commit: pairwise distance matrix on the bank in pivot-sequence space, examine for cluster structure.

### 5.3 Existing bank quality audit

Current setup banks were curated under the prior add-only rule, not under the strict "add only if flawless" rule that emerges from the bullseye framing in §3.1.

If existing banks contain mediocre examples, the avatar starts pulled into no-man's-land before Phase 1 even completes. Strict reading of §3.1 implies re-auditing each bank and either re-curating for flawlessness or accepting the current banks as legacy starting points and only sharpening forward.

Trade-off: re-curation produces a tighter avatar from the start; legacy acceptance preserves accumulated curation work and lets the avatar improve as new flawless examples are added. Decision pending.

### 5.4 Composition method for the joint EV surface

Downstream of this component, candidates carry shape similarity score, classifier outcome, and EV grinder regime score. Composition into final watchlist rank can take several forms.

- **Conditional EV bin lookup.** Bin by `(shape_decile, regime_decile)`, compute conditional EV per bin from historical W/L, rank candidates by their bin's EV. Captures regime-shape interaction natively.
- **RRF (reciprocal rank fusion).** Combine (shape rank, regime rank) by `Σ 1/(k + rank_i)`. Treats components as independent; cannot capture interaction.
- **Multiplicative.** `predicted_wr × predicted_mfe` style. Treats components as independent factors; cannot capture interaction.

Conditional EV bin lookup is tentatively preferred because the regime-shape interaction is a real property of the data (good regime substitutes for tight shape and vice versa, see §3.6) and the other methods cannot capture it. With ~10×10 = 100 cells and thousands of historical candidates, statistical power per cell is sufficient.

This is downstream of the Avatar Correlator proper but shapes what its output needs to expose to consumers. Decision pending alongside §5.1 and §5.2.

### 5.5 Sanity check protocol before trusting output

Two checks any built version must pass before downstream consumers act on its output.

- **Same-data different-seeds variance.** Run pivot detection + avatar construction with different stochastic choices on the same bank (e.g., different tiebreakers in pivot detection if any are introduced). Variance across seeds should be low. High variance = no signal; the algorithm is fitting noise.
- **Real bank vs permuted bank divergence.** Build the avatar with the real bank; compare to avatars built from N random pseudo-examples drawn from the universe. Top-K rankings should diverge substantially. If real and permuted produce similar rankings, the avatar is not capturing setup-specific structure.

These mirror the project-wide sanity checks for any new signal mechanism.

### 5.6 Viability gate findings — RESOLVED, approach-A built per §2

(Historical record. Findings rolled into §2 EXACT spec; load-bearing lessons preserved here.)


A first probe of the architecture used per-setup kneedle-N windows, log(close) trajectories, Pearson correlation, the class-appropriate §3.10 filter, and per-bar tradable masking. Two configurations: single-example avatar and bank 1-NN max-Pearson (each candidate scored against its best-matching bank example).

Outcome:

- **Breakout class (BF probe)**: top-ranked candidates show plausible visual look-alikes — base/rise into a tight right-edge consolidation. The §3.10 AVWAP filter cleanly removes already-broken-out charts (close > AVWAP) without cancelling the avatar's own self-match. Bank 1-NN raises the non-bank-candidate ceiling correlation substantially over single-example, and confirms the §3.7 separation-of-roles: bank as anchor reference, universe as variation source.
- **Fade class (DTSS probe)**: top-ranked candidates are mostly false positives — straight uptrends and basing patterns rather than fade structures. Diagnosis: at E-1, the discriminating geometry of a breakout (rip + flag) is already visible, so log-close correlation captures it. The discriminating geometry of a fade (rejection at the second top, rollover) has not yet happened at E-1 — at that bar the trajectory looks indistinguishable from a healthy uptrend candidate. Bank 1-NN does not fix this, confirming the issue is the *feature representation* (log-close at E-1), not the avatar form.

Implication: single-stream log-close correlation is insufficient for fade-class setups. A representation that encodes structural phase character (trends vs. ranges, with their typical lengths and ADR magnitudes per phase position) is required. Pure log-close cannot distinguish a setup that is *about to* fail at resistance from one that is still trending up.

### 5.7 Phase-decomposition representation hypothesis — RESOLVED, validated

(Historical record. Single-anchor approach-A produced trade-quality top-30s for DTSS, BASE, PARS on Dan's eyeball. Generic per-phase 6-dim signature with log-return anchoring was sufficient. Setup-agnostic recognizer confirmed.)


Each setup is a *sequence of phases* of two types — trend and range — with characteristic length and ADR magnitude per phase position across the bank.

Coarse decompositions (subject to refinement during marking):

- **BF / HTF**: trend → short range
- **BASE**: trend → large range
- **DTSS**: trend → pullback → range → trend
- **3-4DB**: pending decomposition

Per-phase aggregate `(length, signed ADR-magnitude, range size in ADR)` across the bank functions as the setup's structural fingerprint — bank-derived in *positions* (not violating §3.4 because the scoring function itself is unchanged), but capturing the structural information pure log-close cannot represent.

Open question: how to obtain the phase decomposition. Three paths:

- **A — manual phase marking on bank examples.** Bank curator marks phase boundaries (and types) on each bank example. Algorithm aggregates per-phase `(length, ADR-magnitude, range)` into setup signature, then matches universe candidates against that signature. Setup-agnostic in code; cost is one-time per-example labeling.
- **B — auto phase detection via parameter-free rule.** Some structural rule (e.g., monotonic-run length) classifies bars as trend vs range. Cleaner in code but the rule's edge cases risk leaking as hidden parameters that distort the bank's phase signature.
- **C — implicit structural features.** Continuous per-bar features (drawdown-from-running-max, cumulative monotonic-run-length, etc.) that encode trend-vs-range character without explicit boundaries. No labeling, but the link to "average length and ADR-magnitude per phase" becomes indirect.

A is structurally cleanest and most aligned with the hypothesis; the labeling is bounded one-time work. B is rejected for now. C is a hedge if A's results don't validate.

Test plan: A on a small probe (≈5 BF + 5 DTSS examples) before committing to full-bank labeling. If a manually-aggregated phase signature pulls the right candidates, scale to full bank. If not, the phase-decomposition hypothesis is itself wrong and the architecture revisits at the representation level.

Open sub-questions for the build of approach A:

- Phase decomposition format (per-example: bar offsets for boundaries + phase-type labels).
- Phase-type vocabulary — minimal is `trend` / `range` / `pullback`; finer if the data demands it.
- Candidate-side phase decomposition convention — can it be auto-detected with a structural rule that doesn't reintroduce the §3.4 leak, or does it require some other mechanism.
- Distance metric for phase-signature comparison — sum of squared per-phase scalar diffs, or similar.

---

## 6. Pending build

### 6.1 `phase_max_rel_to_window` — 7th per-phase dim

Add generic per-phase scalar: `phase_k_max_level / window_max_level`. Same formula every phase. Captures "how high this phase reached vs the entire window's peak" structurally. Targets bug §4.1 (DTSS magnitude-vs-shape ordering). Per-phase signature grows 6 → 7 dims; DTSS 18 → 21d, BASE 12 → 14d, PARS 6 → 7d.

### 6.2 Stricter §3.10 / DP-fit constraint

Replace `phase_0_max ≥ close[asof]` with `phase_0_max ≥ phase_2_max` (or generically: phase_0_max ≥ max over later phases). Closes bug §4.2 — kills candidates where phase 2 broke above leftside peak intra-phase even if close[asof] came back below.

### 6.3 Multi-anchor 1-NN with amortized precompute

Refactor `dp_fit_*phase` to compute per-candidate matrices (rmax/rmin/argmax/prefix_path/levels) once, then run inner search loop N times against each bank anchor. Distance per candidate = min over N anchors. Resolves bug §4.3.

Expected scaling on current hardware: 10-anchor DTSS in ~2-3 minutes (vs ~17 min naive linear).

### 6.4 Per-candidate percentile flag

Save full historical dedup'd distance distribution per setup as a `.npy` file alongside existing JSON+PNG outputs. Live-day query function reports per-candidate percentile against this distribution + per-day-min-distance percentile for day-strength context. Resolves bug §4.4 interim.

Code partially in place (distance distribution save in scan() — not yet validated end-to-end).

### 6.5 Sector exclusions

Industry-level exclusion via fundamentals_cache.json is in place (§2.7). Currently {"Biotechnology"}. Sector-level exclusion (e.g., "Energy") trivial to add — list already keyed off `info.get("sector")` in the data structure.

### 6.6 Bank curation is a non-build item

Curating multiple flawless examples per setup is gruntwork on Dan's side, not algorithm work. Single tab per setup in the "Setup phases" Google Sheet. The recognizer reads phase boundaries from the sheet at runtime; new bank entries do not require code changes.

### 6.7 Mean rank percentile combine

Replace the multi-anchor combine layer (per-anchor z-score → min-z dedup, lines around 1086–1113 of `avatar_probe_from_db.py`) with mean rank percentile across valid-scoring anchors. Targets bug §4.5.

Mechanism per scan:

- For each anchor, sort that anchor's valid candidates (skip those with structural-constraint distance = 1e18) ascending by raw distance. Each candidate gets `rank_percentile = rank / total_valid_count` within that anchor.
- For each unique (ticker, asof) pair, gather rank percentiles from every anchor that scored it validly. Take mean.
- Final ranking: ascending by mean rank percentile.
- Track which anchor produced each candidate's BEST individual rank percentile, and use that anchor's interpretation (n_bars, fit_boundaries, phase_types) when running setup-specific post-filters (§2.11–§2.14).

Properties: distribution-shape invariant; rewards consistent multi-anchor matches; uses information from every anchor that produced a valid score; no parameter tuning.

Doesn't touch: per-anchor scan, structural constraint inside DP fit (§2.5), greedy non-overlap dedup within ticker, bank's own (ticker, asof) skip from its own anchor, or any setup-specific post-filter. Only replaces the combine layer.

---

## 7. In-progress: HTF multi-anchor produces low-quality top picks (2026-05-04)

### 7.1 Symptom

Single-anchor DTSS scan with the original 6-d signature produced trade-quality top picks. Building HTF on the same architecture (10 banks, distance-based 1-NN combine, per-setup constraints, length lock, AVWAP rule, chart-high gate, inter-phase deltas, internal_move dim) produces top picks that don't look like HTFs at all. Dan's specific finding: **higher raw distance correlates with better-looking charts**, not lower. The metric is rewarding signature mimicry of bank quirks, not HTF-ness.

### 7.2 Concrete example

ACES 2026-05-01 vs CR bank: distance 0.084 (rank #1). Per-dim breakdown: 26 of 27 dims within ±0.04 of CR's signature. ACES's "ignite" pick = 2026-04-27 = +0.3% close-to-prev-close gap, body 10% of range, RED bar. CR's actual ignite (2023-10-24) = +13.6% gap, body 92% of range, green hammer. The signature's `internal_move` dim correctly differs (0.127 bank vs 0.003 candidate, sq diff 0.015) but is one of 27 equal-weighted dims summed in Euclidean distance. Discrimination is averaged out.

### 7.3 Working hypothesis (Dan, 2026-05-04)

Something introduced when switching from single-bank (DTSS) to multi-bank scoring broke ranking quality. The CR-only HTF test (excluding the other 9 anchors) is the clean isolation. If CR-only produces good results → the multi-anchor combine is the bug. If CR-only also produces garbage → the metric's per-bank scoring itself is mis-calibrated for HTF's structure (short load-bearing phases).

### 7.4 Resets done in this session

Reverted these additions to mimic the original DTSS-style approach:

- `signature_from_partition` back to 6 dims per phase (dropped `phase_internal_move` and inter-phase deltas)
- `dp_fit_2phase` and `dp_fit_3phase` back to comparing only the 6-d-per-phase block
- `n_dims = 6 * K` in the wrapper

Still in place but should be reviewed/disabled for the simple-test:

- `PHASE_LENGTH_LOCKS = {("htf", "ignite"): 4}`
- `PRE_CONTEXT_LOOKBACK = {"htf": 80}` (chart-high within 1 ADR gate)
- `PER_SETUP_AVWAP_RULES = {"htf": [("range", "ignite")]}`
- `SKIP_CLASS_FILTER_SETUPS = {"htf"}`
- `PER_SETUP_CONSTRAINTS["htf"]` keeps only `("ignite", "ge_asof")` (range-vs-asof was already dropped)

### 7.5 Pending immediate next test

Run `python research/avatar_probe_from_db.py --setup htf --today --exclude AFG,XPEV,OGI,SOUN,ASTS,QUBT,CRCL,EQX,PL` (CR-only). Eyeball candidates. If they look like HTFs → the multi-anchor combine + bolt-ons were the issue. If still garbage → revisit the metric architecture entirely.

### 7.6 Other findings worth retaining

- The pre-context filter ("chart_high − avatar_high ≤ 1×ADR") and AVWAP rule do filter SOME candidates (16-35% of today candidates dropped) but don't address the close-trajectory mimicry issue.
- Phase-internal-move dim (added then reverted) DID encode the discriminating gap magnitude correctly, but at 1/27 weight it gets diluted in Euclidean distance.
- Dan's per-setup rules (HTF: range_max ≤ asof, ignite_max ≥ asof, ignite ≤ 4 bars, chart-high within 1 ADR, no AVWAP from range above ignite) are correct as stated. Implementation is correct. They gate but don't rank.
- Bank examples for HTF have wide variance in n_bars (16-127) and ignite internal_move (+13% to +168%). The "specific magnitude per bank" approach forces candidates to mimic specific values, not HTF-ness in general.

### 7.7 Phase-anchored aggregates (FIX, applied 2026-05-04)

**The original `signature_from_partition` anchored ALL per-phase log levels at the window's first close (`log(close[0])`).** This meant the phase boundaries Dan marked were used ONLY to demarcate which bars belonged to which phase — never as the actual anchor for measuring each phase's behavior.

For HTF this hides the most discriminating feature: a 1-bar ignite phase's close is measured RELATIVE TO WINDOW START, not relative to the close BEFORE the phase started. CR's ignite ($93.90 close, prev close $82.69) appears as -0.005 (window-anchored) vs ACES's "ignite" ($35.73 close, prev close $35.61) as -0.041. The two look "similar" at -0.005 vs -0.041. They are wildly different at +12.7% vs +0.3% (phase-anchored).

For DTSS this matters less because phase 0 starts AT the window start (so window-anchored = phase-anchored for phase 0) and phase magnitudes are so large that even window-anchored aggregates differ.

**Fix applied:** in `signature_from_partition` and in `dp_fit_2phase` / `dp_fit_3phase`, the `end_level`, `max_level`, `min_level` per-phase dims are now anchored at `log(close[phase_start - 1])` instead of `log(close[window_start])`. For phase 0 (which starts at window start), the two are equivalent. For later phases, dims now reflect phase-internal magnitude.

Dim count unchanged (6 per phase). Architecture unchanged. Only the anchor point shifted to be PHASE-RELATIVE.

Untested as of code commit. Next session should run CR-only HTF today to verify ACES drops out and a real HTF (XE/RMAX/FCEL/TRT/ARMG/CNC) rises.

### 7.8 Constraints config currently disabled (for the CR-only isolation test)

These were stripped to mimic original DTSS minimalism. Re-enable when we know phase-anchored aggregates work.

```python
PHASE_LENGTH_LOCKS = {}              # was {("htf", "ignite"): 4}
PRE_CONTEXT_LOOKBACK = {}             # was {"htf": 80}
PER_SETUP_AVWAP_RULES = {}            # was {"htf": [("range", "ignite")]}
SKIP_CLASS_FILTER_SETUPS = {"htf"}    # kept (HTF doesn't use breakout class filter)
PER_SETUP_CONSTRAINTS["htf"] = [("ignite", "ge_asof")]   # kept
```

The inter-phase delta block and `phase_internal_move` dim were also removed when reverting to DTSS-minimal. Phase-anchored aggregates obviate the need for `phase_internal_move` since the phase's `end_level` dim now IS the phase's internal move (for 1-bar phases, exactly the close-to-prev-close gap).


### 7.9 Settled metric: ADR per-phase signature + per-anchor z-score combine (2026-05-04, after 3-hour autonomous iteration)

After exhausting the metric+combine search space against four Dan-validated HTF tickers (RMAX, TRT, OGN, XE — REMX in the autonomous prompt was a typo for RMAX, confirmed by Dan), the settled config:

**Score (`--score adr_sig`):**
- Per-phase 3-d signature: `(length_bars, net_size_adr, range_size_adr)` — duration in raw bars (not fraction; fraction's [0,1] scale was drowned by ADR-scale dims), net move and intra-phase range-size both in candidate-self-ADR units (ADR(30) at asof in price units; settled at 30 to surface RMAX which the standard 20-day ADR misses).
- DP search finds candidate's phase boundaries that minimize squared distance to bank's signature.
- Setup-agnostic by §3.8 (same code, no per-setup branches).

**Combine (in `run_one_setup`):**
- Each anchor produces its own distance distribution; per-anchor z-score `(dist - mean)/std` puts every anchor on a common scale (uniform across examples).
- Each candidate's combined score = its lowest (most negative, best) z-score across all anchors.
- Per-anchor diversity cap: `max(8, top_n // 3)` slots per anchor — prevents one bank with high-std distribution from monopolizing.

**Result on 2026-05-04 today scan: 4/4 of the named targets:**
- TRT (#4, QUBT-anchored, z=-1.29)
- RMAX (#5, QUBT-anchored, z=-1.26)
- OGN (#8, QUBT-anchored, z=-1.21)
- XE (#11, CRCL-anchored, z=-1.00)

**Verified setup-agnostic across HTF, DTSS, BASE.** Same algorithm, same ADR_LOOKBACK=30, same z-score combine, same cap=10. Each setup's per-anchor scans surface candidates whose structure matches that setup's bank examples. BASE candidates verified to look correct in TC2000 by Dan even though the thumbnail grid was less obvious. BF/PARS/3-4DB skipped — no phased bank entries (curation work).

### 7.10 Why ADR_LOOKBACK = 30 (and not 20)

The ADR sweep (§7.14) showed RMAX surfaces only at ADR_LOOKBACK ≥ 30. RMAX's biggest 1-bar move is around 4-5 ADR with a 30-day window, large enough to match bank ignites; with ADR(20) RMAX's ignite normalizes smaller and falls below threshold.
- REMX's "rally" was multi-bar + gradual; its DP-found "ignite" picks any 1-2 bar segment with at most 1.95 ADR net.
- The bank doesn't cover the "slow-accumulator currently at high" pattern. Dan said earlier "REMX isn't HTF I was thinking of something else" then re-asked for it; consistent with REMX being borderline.

**Bank curation work needed (out of metric scope):** add a "slow-accumulator" HTF example (mild multi-bar rally + tight flag at high) to the 10-bank set. Once banked, REMX-style candidates will match.

### 7.11 Approaches tested and ruled out (don't re-explore)

Combine strategies (× adr_sig with raw bars):
- Raw distance sort: 1/4 (one bank's distance scale dominates)
- Median-normalize, IQR-norm, max-norm, log-distance: 0-2/4
- Bank-sumsq-normalize: 0/4 (duration² in bank_sig dominates the divisor)
- Percentile-rank, within-anchor-rank: 2/4 (each anchor's #1-#3 fills slots regardless of match quality)
- Z-score truncated to top 100: hurt XE
- Robust z-score (median/MAD): hurt XE (CRCL has only 2 candidates, MAD undefined)
- RRF (reciprocal rank fusion): 1/4
- Cosine on signature vector: 0/4 (candidates align on long-phase-0 direction)

Signature variants:
- `length_frac` instead of raw bars: 1/4 (duration disregarded — drowned by ADR dims)
- + universal `drawdown_from_window_high_adr` dim: 2/4 (banks aren't always at-high; mismatch hurts)
- adr_bar (bar-by-bar relative-L2): 0-1/4 (rewards daily-wiggle similarity, not HTF structure)

### 7.12 Pending — bank curation to surface REMX

Add a "slow-accumulator HTF" example to the bank: a stock that ran up gradually over 30+ bars (rather than a 1-bar gap), with the asof at the recent-high consolidation. The current 10-bank set is biased toward 1-bar-rip ignites; this gap explains why REMX (and similar slow-rally stocks) won't surface regardless of metric choice.

### 7.13 Detailed comparison: REMX vs each bank's signature (diagnostic)

REMX 66-bar window: rally from $79 to $107 over the window (+27% absolute, +9.94 ADR), biggest 1-bar move = 1.95 ADR (occurred at bar 4, very early). Last 5 closes are flat-ish (100-106 range). REMX is at recent high.

DP-found candidate signatures vs each bank:

| Bank | dist | Mismatch in shape |
|------|------|-------------------|
| CRCL (K=2, 6 bars) | 8.54 | smallest dist but only because window is tiny — CRCL matches anything short |
| OGI | 32.5 | range went UP +3.93 vs bank +1.77; range_size 8.96 vs 4.21 (REMX range too wide) |
| PL | 42.0 | range_net +0.11 vs +1.10; range_size 8.96 vs 4.20 |
| CR | 60.5 | range went UP +0.14 vs bank's -3.90 (sign flip on range direction) |
| XPEV | 68.4 | range_size 8.96 vs 2.14 — REMX range is 4x wider |
| QUBT | 69.0 | ignite +2.49 vs bank +7.39 — REMX no rip |
| AFG | 76.2 | range_net +1.17 vs -5.73; range_size 8.96 vs 5.73 — opposite range trajectory |
| ASTS | 189.2 | range_size 10.84 vs 3.35; range_net +8.47 — REMX range too wide and rallying |
| EQX | 234.0 | range_size 14.59 vs 5.83; range_net +10.44 vs -1.32 — REMX has multi-bar rally |
| SOUN | 331.3 | even worse mismatches |

**The diagnostic pattern: REMX's "range" phase is itself a multi-bar rally** (range_size 8-15 ADR depending on bank's window length). All 10 banks have range phases that are tight-to-moderate (range_size 2-6 ADR). REMX is structurally a different flavor.

Confirmation: no metric variant can resolve this without bank curation work.

### 7.14 ADR_LOOKBACK sweep (autonomous-iteration finding)

Tested ADR_LOOKBACK ∈ {14, 20, 25, 30, 35, 40, 45, 50, 60, 90} as a single tunable. Result on the named target list (REMX, TRT, OGN, XE):

| ADR | REMX | TRT | OGN | XE | RMAX | result |
|-----|-----|-----|-----|-----|-----|--------|
| 14 | - | - | - | #11 | - | 1/4 |
| 20 (production) | - | #7 | #5 | #11 | - | 3/4 |
| 25 | - | #4 | #5 | #11 | - | 3/4 |
| 30 | - | #4 | #8 | #11 | #5 | 3/4 (+ RMAX) |
| 35 | - | #5 | #10 | #11 | #3 | 3/4 (+ RMAX) |
| 40 | - | #5 | #10 | #11 | #2 | 3/4 (+ RMAX) |
| 45 | - | #5 | #10 | #11 | **#1** | 3/4 (+ RMAX) |
| 50-60 | - | #5 | #10 | #11 | #2 | 3/4 (+ RMAX) |
| 90 | - | #5 | #10 | #11 | #3 | 2/4 (XE drops) |

Key finding: ADR_LOOKBACK ≥ 30 surfaces RMAX as a top match (along with TRT/OGN/XE). ADR=45 gives RMAX rank #1.

Production left at ADR(20) per convention. If Dan's "REMX" was a typo for **RMAX** (he previously listed RMAX as a Dan-validated HTF), changing `ADR_LOOKBACK` to 30-50 yields all four named tickers in top 11. REMX (the actual ticker) doesn't surface at any ADR value — its biggest 1-bar move (1.95 ADR) is too small for any bank's ignite (4-7 ADR) regardless of ADR window.

**Resolution (2026-05-04):** Dan confirmed REMX was a typo for RMAX. Production switched to ADR_LOOKBACK = 30. 4/4 of (RMAX, TRT, OGN, XE) in top 11 of HTF today scan.

### 7.15 Per-setup sanity filters — HTF, DTSS, BF, and BASE all done

HTF filter stack defined in §2.11 (9 rules). DTSS filter stack defined in §2.12 (9 rules). BF filter stack defined in §2.13 (12 rules). BASE filter stack defined in §2.14 (8 rules). All rules are bank-derived (every bank example passes its own rule against itself) or NaN-exempt. See §7.16 for an open metric-ranking diagnostic surfaced after wiring BASE.

### 7.16 BASE phase taxonomy is too loose — needs rebuild (2026-05-04)

This session locked the 8 BASE post-filter rules (now §2.14) and wired `base_check_candidate()` mirroring HTF/DTSS/BF. Standard top-30 historical scan returned zero curated BASE examples out of 38 — only ERO 2025-11-20 (one bar before the bank's own asof) appeared, and only because of its bank-self-match path.

Post-filter is not the bottleneck:
- 33 of 38 examples pass §2.14 rules 2–8 at their entry_date−1 asof bar.
- 37 of 38 pass rule 1 (shallowest H- algo line) for at least one anchor's window length.
- Combined, ~32 of 38 examples pass the entire §2.14 stack against at least one anchor.

Class filter (§3.10) cut most of them at the pre-pass:
- §3.10's `close[asof] ≤ AVWAP[asof]` test depends on the anchor's n_bars (which sets the AVWAP window). A curated example passes against its own anchor's window but fails against other anchors' window lengths.
- Per Dan's directive, class filters are disabled at runtime by expanding `SKIP_CLASS_FILTER_SETUPS` to {htf, dtss, bf, base, 3-4db, pars}. Spec/code cleanup (rip §3.10 logic from `avatar_phase_probe.py`, delete §3.10 + §2.8, renumber §2.9–§2.14) is pending Dan's go-ahead.
- Re-running with class filter off still produced 0 of 38 examples in top-30 — the class filter wasn't the only cut.

Full-depth diagnostic (PER_ANCHOR=99999, COMBINED_TOP_N=99999, render skipped) ran for all 4 setups to determine whether the metric ranking failure is universal or BASE-specific. **Result: BASE-specific.**

| setup | survivors | z range | examples found / total | best unbanked rank | best unbanked z |
|---|---:|---|---|---:|---:|
| HTF | 57,700 | −6.88 to +2.98 | 6/28 | MOD 2023-06-01 → 2,381 | −1.86 |
| DTSS | 21,196 | −2.20 to +2.22 | 6/60 | VCTR 2025-02-07 → 1,617 | −0.06 |
| BF | 58,403 | −4.33 to +1.89 | 11/45 | AXON 2021-02-03 → 3,153 | −2.50 |
| BASE | 73,812 | **−0.030 to ~0** | 4/38 | IONQ-WS → 26,284 | **−0.029** |

HTF and BF show meaningful z-spread and surface curated unbanked examples at competitive ranks (MOD at 2,381 with z=−1.86; AXON at 3,153 with z=−2.50). The metric is doing real work for those setups. DTSS sits in between — extremes exist but most examples bunch around z=−0.05. BASE alone has flat-z collapse and buries curated examples at rank 26k+.

**Why BASE specifically.** The 2-phase taxonomy (`uptrend → range`) is structurally too loose. The `range` phase has no tightness requirement in the per-phase signature (the 6 dims are length_frac, end/max/min log-return, directness, argmax_position — none of these enforce that the range is *compressing* toward asof). Any sideways pattern with a prior uptrend trivially matches. Random universe charts with long sideways periods saturate the metric.

**Next direction (Dan, 2026-05-04): "we need better phases for BASE."** The 2-phase structure should be replaced with a taxonomy that encodes the distinguishing structural feature — likely the tightness/compression of the late-range as it approaches asof. Candidate taxonomies to investigate next session:
- Split `range` into substages (e.g., `uptrend → wide_range → tightening`).
- Mirror BF's 3-phase structure (`range → ignite → flag`) at the longer scale.
- Per `project_avatar_base_equals_bf_weekly.md`: recompute BASE on weekly bars and apply BF's existing taxonomy directly (literal resample claim).

Once a new BASE phase taxonomy is designed, the 11 BASE bank entries' `phases_json` need re-marking, then revisit the §2.14 rules (rules 1–8 reference `uptrend` and `range` boundaries by name — may need updating for the new phase types). Re-run sanity check + historical scan + 38-example diagnostic.

The other hypothesis from earlier this session — magnitude-invariant level dims — is **not** the right fix here. HTF/BF/DTSS share the same metric core and don't show flat-z collapse, so magnitude sensitivity isn't the universal villain. Setting that hypothesis aside.

Existing §2.14 rules and §7.15 status remain valid against the current 2-phase BASE; just inadequate as a discriminator. The phase rebuild is the unblocker.

Diagnostic output files (one per setup, retained for reference):
- `research/avatar_viability_bank/HTF_historical_top99999.json`
- `research/avatar_viability_bank/DTSS_historical_top99999.json`
- `research/avatar_viability_bank/BF_historical_top99999.json`
- `research/avatar_viability_bank/BASE_historical_top99999.json`

### 7.17 Combine layer identified as primary ranking failure mode (2026-05-05)

Continued from §7.16. Investigation chain:

- Of 38 BASE curated examples (11 bank + 27 non-bank), only 4 appeared in saved historical scan output. Bank's own (ticker, asof) is excluded from its own anchor's per-anchor scan; survives only via other anchors. Other anchors with shorter n_bars often fail the structural constraint (`phase_0_max ≥ close[asof]`) because their window length cuts off the candidate's prior uptrend. Greedy non-overlap dedup picks adjacent-day windows over the curated exact date in tiebreakers. Stacked together, these mechanics drop most curated examples before ranking.
- A close_in_stack signature (close at asof relative to all 10 relevant MAs) showed clean separation: bank median ~1.0 with std ~0.09, non-bank median ~1.0 with similar tightness, random universe median ~0.54. Static separation 0.92σ. Adding it as a per-phase signature dim with squared-difference penalty did NOT translate to ranking lift — the per-anchor z-score recomputation washed out the signal because the new dim's per-candidate penalty inflated each anchor's std and compressed all z's toward zero.
- Single-day scan with §2.14 post-filter disabled, on 5 non-bank examples (post-filter would not be the cut for these): curated examples landed at top 0.8% to 16.6% of universe — per-phase scoring HAS signal. None reached top 30 even on a clean single-day scan with the noise floor cleared. Flat-z within the survivor pool means scoring resolution isn't sharp enough.
- Conclusion: the multi-anchor combine layer (per-anchor z-score → min-z dedup) is the primary ranking failure mode. Three issues stack: per-anchor z-score normalization is apples-to-oranges across anchors with different distance distribution shapes; min-z favors anchor-specific outliers over consistent multi-anchor matches; the dedup discards rank information from all but one anchor per candidate. Documented as bug §4.5.

Path forward: mean rank percentile combine (§6.7) replaces the broken min-z layer. Doesn't touch per-anchor scan, structural constraint, greedy dedup, or post-filters. After wiring, re-run the 5-test single-day scan to confirm curated examples lift toward top 30, then full historical to confirm a meaningful fraction of all 38 curated BASE examples surface, then sanity-check HTF/BF/DTSS/3-4DB top-30 to confirm no regression.

If the combine fix alone doesn't lift curated examples enough, close_in_stack as a pre-filter (drop universe candidates with `close_in_stack < 0.85` BEFORE per-anchor scan) is a separate composable option — predicted to cut ~60% of universe noise while keeping ~92% of curated examples through the gate. Has not been validated as needed yet.

**Update 2026-05-09:** the specific mechanism is the cap-not-enforced bug — see §4.6. The diversity cap that was supposed to mitigate the min-z monopoly was dead code: every slot in the top-30 ended up filled by whichever anchor had the longest negative z-tail on that asof. Reproduced May 4 today output exactly with the scrapped script (HTF 30/30 QUBT, DTSS 20/30 CELH, BF 17/30 DAC) — confirms the bug is structural, not a transient.

**Update 2026-05-09 (α-vs-β diagnostic on BASE; cap-fixed isolated codepath; outputs in `research/avatar_alpha_vs_beta/`):** two diagnostics run to determine whether the BASE failure is metric-heterogeneity (bank scattered in signature space — α) or bug-shaped (date/dedup-specific — β).

- **D1 — pairwise B_i vs B_j (56 valid pairs of 110 attempted; rest fall out because B_i's ticker history doesn't extend back B_j.n_bars before B_i.bank_asof, or B_i fails tradable mask at its own asof).** Median universe percentile of bank pairs = 0.215 (P25=0.081, P75=0.451, max=0.874). Verdict: **mixed** — bank is moderately coherent in signature space but the spread is wide enough that some bank pairs sit in the 45–87th percentile of the universe distribution against the other bank. The metric carries signal but isn't tight.

- **D2 — ERO swept across 19 trading days [2025-11-10, 2025-12-05] (post-filter `base_check_candidate` skipped for runtime; raw z-score top-100 with cap=33/anchor).** ERO surfaced top-30 on 2/19 dates — 2025-11-18 at rank 23 and 2025-11-20 at rank 2. Both within 3 trading days *before* ERO's own bank_asof 2025-11-21. On 2025-11-21 itself: NOT_IN_TOP_100 (the bank-self-skip mechanic excluding the exact (ticker, asof) pair from its own anchor's scan). After 2025-11-21, ERO never resurfaces in the top-100 on any swept date. Verdict: **narrow-β**.

- **Side observation:** top-5 on every date in the sweep is monopolized by ERO-anchored candidates even with cap=33/anchor — ERO has the longest negative z-tail on these dates by a wide margin. The bank-self-skip blocks ERO at the exact bank_asof but a 1-bar-earlier window slips through (see 2025-11-20: ERO ticker scoring vs ERO bank's signature, ranking #2). And capping at 33 still leaves ERO anchor controlling the top-5 — cap doesn't dethrone the dominant anchor's tail, it just lets a few candidates from other anchors join the top-30.

Combined picture: the cap bug is real and fixed, but the BASE failure has additional structural causes — bank-self-skip at exact bank_asof, ERO-anchor monopoly even with cap, and a metric whose D1 spread (0.08 to 0.87 across bank pairs) is too wide to reliably surface other banks against ERO's distribution.
