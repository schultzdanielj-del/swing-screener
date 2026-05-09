# ScanPerfect Project Audit — Historical Narrative

**Audit window:** project inception (2026-02-16) through 2026-05-09.

**The original purpose of the project**, plainly stated in `SWING_SCREENER_PROJECT.md` and `BUGS.md`: a nightly automated watchlist that finds 2–7 high-quality setups per day, ranked by EV (win rate × median captured move), with losers under 1 ADR and winners running 5–6 ADR median, in service of compounding ~2.5% monthly for 20 years. Every component — grinder, refinement, vetting UI, EV grinder, profit grinder, scan tuning, health check — was scoped to serve that watchlist.

**The blocker.** Three months of work never produced a setup-finding scan that the rest of the project could build on. The project is stuck at the very first stage. Everything downstream of "here is a setup candidate" works: the win/loss/no-enter classifier, the MFE measurement, the EV grinder that finds market-regime features per setup, the exit and profit grinders that pick exit strategies. The single broken step is the scanner that should produce the setup candidates in the first place.

The puzzle is not generative diversity. Across a thumbnail grid of every setup example in the bank, the charts look almost the same — small variations on a single visual idea per setup, and even cross-setup the family resemblance is high. So the question is not "why can't we discriminate across diverse structures." It is "why can't we surface charts that look like things we have many examples of." Two methodological styles were tried:

- **Style 1 — condition-based bounding box.** Brute-force beam search over a ~16k-expression cache, picking conditions that fence in the example set and exclude the universe. Close to working, but overfit-heavy by default. When overfit protection (walk-forward + permutation null + multi-comparison correction) was applied to it the way the project's data scientists configured it, the surviving rule set on each setup ended up at roughly one condition that admitted nearly the whole tradable universe.
- **Style 2 — pure shape correlation.** No filtering, only ranking by structural similarity to the example bank. Signal so weak that bank examples themselves did not reach the top 10 of their own correlation lists — they were buried behind hundreds of unrelated, often visually garbage charts.

Both poles, as implemented, fell short by ~10× on the operational metric the project cared about. The only place the system crosses any quality bar is DTSS under the avatar correlator, where it works only after bolting 9 hand-discovered hard filters on top of the similarity score — effectively re-importing Style 1 to rescue Style 2. BF, BASE, and HTF do not work under that hybrid either.

**A separate trader, working in the same problem domain, has reportedly built a working version of this system using simple methods.** What is known about that implementation is narrow but specific: each setup is broken into phases, and the phases are defined at least in terms of distance and time. The scoring/admission mechanism on top of those phase descriptions is not known — it could be a per-phase bounding-box scan (Style 1 with phase-aware features), a per-phase distance/time template scored by some simple metric (Style 2 with minimal descriptors), or any of several other simple compositions. That outside reference point is the single most important framing for what follows: the *problem class* has been demonstrated to be solvable by someone running a simple phase-decomposition. The question of what failed in *this* project is therefore a question of implementation, calibration, build quality, phase-definition quality, or unexamined upstream assumptions — not a question of whether the underlying problem is solvable.

**This audit does not pronounce any approach dead.** Every "did not produce usable output" finding below should be read as "this specific implementation, as tested under the validation regime applied at the time, produced these measured numbers." The numbers are real; their interpretation as a permanent verdict on the approach is not. Any of the failures may turn out to be a calibration choice (e.g., alpha=0.01 vs 0.05 — a decision an analyst makes, not a fact about the data), a labeling bug, an example-set composition problem, or a partial implementation never fully debugged.

**Method.** Clean past-tense narrative drawn from git log (1,385 commits over Feb–May 2026), every root and shelved `.md` spec, all five worktrees, and the dated artefacts in `archive/`. No forward-looking recommendations; the closing synthesis names the upstream assumptions that recurred across the failures.

---

## Era 1 — Origin and v1: chart pipeline → Spiderweb → Pyramid (2026-02-16 → 2026-03-05)

The repository was scaffolded on **2026-02-16** as a chart-rendering pipeline — bulk OHLCV fetch via `yfinance`, a setup library with three classes (3-4DB, DTSS, HTF), and matplotlib chart generation with EMA/SMA overlays and AVWAP. The first three days produced an example management UI: editable entry dates, gallery views, extension overlays. By **2026-02-19** the project had been renamed ScanPerfect, `ta_knowledge.md` had been seeded, and `pcf.md` codified TC2000 PCF syntax — the project's TA vocabulary was formalised before any quantitative scan existed.

The first scan was a TC2000 PCF condition list. **2026-02-21** added a DTSS scan endpoint with 12 conditions ANDed on a single bar that all 23 examples passed. This was the project's first clean win: a hand-curated boolean filter that retained 100% of the example set. A profiling engine (~365 PCF-equivalent measurements per ticker) was built immediately afterward, followed within hours by a Discovery Engine, an Outcome Engine, and a Management Optimizer (**2026-02-22**). The architecture at that moment treated condition discovery as a static optimisation problem: profile examples, score features by consistency × selectivity, brute-force the management parameters.

Within a day Dan wrote `local_runner/` and named the search engine **THE GRINDER**. The first grinder used a "Spiderweb search" — a single-tier brute-force over a 1,338-expression library, with a frontend slider to control filter aggressiveness. The Spiderweb was vectorised with numpy broadcasting, then matmul-pre-screened (78× faster), then parallelised across 8 workers (an i5-12600K). The expression library expanded from 1,338 → 2,271 → 2,541 → 4,017 across 2026-02-23 alone.

The first signs of the recurring problem appeared during these expansions: a "DTSS bespoke matrix" was added on 2026-02-23 (LSP/AVWAP expressions specific to DTSS) and the very next commit *stripped* it, making the grinder "100% generic." The principle that emerged here — same algorithm across all setups, no per-setup branches — locked in early and would govern every subsequent build (it remains a binding constraint in the avatar correlator's spec on 2026-05-09). The first scan-quality crisis followed immediately: a single-tier search couldn't get signal counts down. **2026-02-23 23:17** added the **Pyramidal Grinder** — six tiers nested from D1 to monthly, with peak-target ≤3 signals/day at each historical tier. This was the architectural answer to "scan recall is too high": stack tiers of conditions on top of one another, with each tier searching the survivors of the previous. The expression series cache was built on **2026-02-24** to make this tractable; nightly automation followed the same day.

The build then expanded outward: backtest_runner, signal_distribution analytics, NaN-handling fixes (2026-02-24), outcome grinder with auto-derived ADR/MFE floors (2026-02-26), parity tests between exit and outcome grinders, LSP detector v2 (87% exact match on 23 validated examples), Expression Engine V2 with HTF resampling (2026-02-27), algo line detector (2026-02-28, 44 expressions), multi-pass daily→weekly→monthly pyramid (2026-02-27). The expression library reached **12,175 expressions** by 2026-02-28: 4,017 daily + 80 LSP + 44 algo + 8,034 HTF. By **2026-03-01** the exit grinder had been folded in; **2026-03-02** added the pipeline dashboard, signal filter (dedup + exit + rank), and the first vetting UI.

The era ended with the system functionally complete on paper but operationally suspect. Multiple crash points revealed that the pipeline's many bespoke wirings hid integration bugs — Railway-side state diverged from the desktop's local state; vetting writes didn't persist; agent step IDs didn't match UI step IDs; pipeline jobs got stuck on stale state. By **2026-03-05** the working architecture had grown a significant tail of documentation in `archive/shelved_docs/` that would be formally retired in the next era's pivot: `DARTBOARD_DESIGN.md` (additive scoring washes out discrimination), `EXPRESSION_ENGINE_V2.md` (incomplete plan for new capabilities, now superseded), `MULTISTAGE_EXIT_GRINDER.md` (folded into profit_grinder), `REFINEMENT_GRIND_FIX.md` (obsolete), `TODO_REWRITE.md` (LSP rewrite plan abandoned).

The era's signature failure mode was already visible: **scan output volatility**. Grind #4 with 62 examples produced 168 signals; grind #5 with 68 examples produced 1,691 — a 10× swing from a 6-example delta (later catalogued as BUG-001, "D1 over-locking destroys downstream tiers"). The system's discrimination was not stable as the example set grew. The fix proposed at the time — a hard row-floor constraint on D1 — was implemented as a fixed `D1 cap=15` and assumed sufficient. It was not. The same instability would resurface, in different mathematical clothing, in every era that followed.

### Era 1 detailed catalogue — Component build sequence and naming

The era's component churn ran fast enough to be hard to track. The actual build order, drawn from git log:

- **2026-02-16** (day one): repo scaffold, `yfinance` bulk fetch, setup library (3-4DB, DTSS, HTF), matplotlib chart pipeline with EMA8/21, SMA50/200, AVWAP overlay, dark theme.
- **2026-02-17**: 3-4DB examples manifest (21 examples), `OHLCV data for 21 3-4db examples (6mo before + 15d after chart dates)`, pinned entry dates, 18 PCF conditions in `conditions.json`, scan-condition discovery.
- **2026-02-18**: front-end scaffold with FastAPI backend + interactive candlestick chart. Project state doc as source of truth.
- **2026-02-19**: rename to ScanPerfect. `Save Example` POST endpoint. Examples page with D1 charts + scrollable feed. Migrated from `mplfinance` to pure matplotlib (mplfinance version pinning issues). 60-day windows, 3-column grid. SMA extension analysis (5yr % from 50/200 SMA charts) added — this was the first "extension" measurement, which would become a major feature class. `ta_knowledge.md` seeded with: market stages, channel rules, AVWAP, wave cycles, VIX, stage-2 transitions, stage-3 dip/correction (5%-of-200SMA threshold), stage-4 (eyeball + TA confirmation), 50-extension pyramid/mountain structure, statistical extension ceilings (AAPL/blue-chip ADR ceilings, IPO profiles, bimodal extension clustering, fade at statistical ceiling). MOC lines (highest-RVOL candle highs/lows as permanent levels). LSP definition + MOC stacking. Net-gamma section + GEX rules. Intraday VWAP foothold sequence. Algo lines (volume-based trendlines, penny precision).
- **2026-02-20**: universe OHLCV fetcher pulled 11,682 tickers ("the full market"). Tradable universe filter (price ≥ $1, 20d avg dollar vol ≥ $5M) reduced to ~4K. ANALYSIS_SYSTEM.md added — methodology document. 5-year backtest endpoint. Scan results stored in DB.
- **2026-02-21**: DTSS LSP data for 26 examples. Setup build process document. ANALYSIS_SYSTEM.md rewritten as a 7-step process. `pcf.md` added (TC2000 PCF language reference). DTSS scan endpoint with 12 ANDed conditions (23/23 pass on examples). Profiling Engine (Step 3): ~365 PCF-equivalent measurements per ticker. Profiling Engine + bulk OHLCV endpoint + Discovery Engine (#2) "scores features for consistency × selectivity." Build TODO tracker added.
- **2026-02-22**: Outcome Engine (#3) — forward outcome precomputation for management optimisation. Management Optimizer (#4) — exhaustive trade management sweep. API & DB Integration (#5) — wire all analysis engines into Railway. LSP detector v2: 87% exact match on 23 validated DTSS examples. Discovery run on 500-ticker universe with LSP features. FastProfiler with caching + concurrent fetching. ANALYSIS_SYSTEM.md updated with current DTSS pipeline status. Then later in day: profiler workflow rewritten as "PCF-native expressions, TA-grounded rules, 5min budget, per-setup configs." Expression generator + compute engine + test runner. **`local_runner/` created. THE GRINDER named.** "THE GRINDER v2 — Spiderweb search with frontend slider." Fix agent UTC timestamps. Full traceback on errors.
- **2026-02-23**: Grinder v3 — split matrix, nightly auto-rebuild, clean warnings. Stop button + auto-reset stuck jobs. Stale expression cache auto-regenerate when brute_expressions.py changes. **2026-02-23 10:23**: "DTSS bespoke matrix — LSP/AVWAP expression engine + setup-specific expression loader." **2026-02-23 12:45 (one day later, same day commits)**: "Strip bespoke system — make grinder 100% generic" — the architectural decision that locked in for the rest of the project. Phase 2 bespoke re-filter folded into post-Phase-1 layer. Expression library expanded 1,338 → 2,271 → 2,541 → 4,017 in successive commits. **Pyramidal grinder added 2026-02-23 23:17** — the first multi-tier nested-time-horizon search (D1 → 1wk → 1mo → 3mo → 6mo → 1yr).
- **2026-02-24**: Step 5c expression series cache builder + pyramid grinder integration (24/24). Nightly update automation. NaN bug: "require ALL examples have valid values for expression ranges." Determinism in pyramid grinder + restored NaN fix. Smart NaN handling + remove expansion cap. backtest_runner.py for visual signal verification. Historical tab: signal prevalence bar chart + SPY candlestick bubble overlay. Beam=10000 matmul-pre-screened spiderweb (vectorised). Multi-run --runs flag for comparison.
- **2026-02-25**: SWING_SCREENER_PROJECT.md rewritten — removed stale vision/PCF content, reflected pyramid grinder. Dead files deleted (DTSS_NEXT_CHAT_PROMPT.md, SETUP_BUILD_PROCESS.md, src/ vision pipeline). Legacy data deleted (data_legacy/, data/ohlcv/3-4db/, data/charts/3-4db/). Multi-run comparison summary table. EV_OPTIMIZER.md design doc + AVWAP entry hypothesis test (busted — 0/23). Steps 6-9 pipeline rewritten. **Step 6: Exit grinder design — 2,119 post-signal expressions, floor-based scoring, re-run architecture**. exit_expressions.py (220 base + 1512 bool aggs = 1732 total). Default min-trigger-pct = 1.0 (must trigger on all examples). Exit expression library rebuilt to 4,310 exprs + parallelised. **Step 7: outcome_grinder.py** — classify signals as outcome/non-outcome. Boolean aggregation handling (close_above_avgc50_count_true_15b).
- **2026-02-26**: Cleanup — 26 dead/superseded files removed. lsp_detector.py + classify_universe.py restored. VISION.md and EV_OPTIMIZER.md removed. Step 7 outcome grind rewritten with signal-bar anchoring + 1 ADR filter + segment expression library. **Critical fix**: `load_example_data` uses 5yr cache instead of 150-bar API snippets. Hard gate: validate_examples aborts if any example fails. Grinder always finishes + always includes example signals. Best-node tracking (prefer deeper levels at same peak). **Exit grinder ranking fix**: median_pct_move primary, avg_pct_move tiebreak. Exit grinder uses local 5yr OHLCV cache instead of Railway API. Failed expression name logging. Max-forward default 252 bars (1 year), then reverted to 120 (1 quarter). 16 broken exit expressions fixed (period→window). FTAI exit diagnostic. CELH exit-during-formation bug discovered. Outcome grinder Phase 1 rewrite. GrindStorage system + migration script. Boolean aggregations in exit_compute use full history lookback. All grinders refuse to save if any example fails. Outcome grinder uses ExpressionEngine for exact parity with exit grinder. **Parity test**: verifies exit + outcome grinders compute identical values. Single computation path for shared expressions across all grinders. Outcome grinder measures signal-bar-close to exit-bar-close. Auto-compute ADR + MFE floors from earliest signal bars. **AAOI LSP relabelled** (highest unbroken pivot Dec 19 @ $24.08). BRK-B → BRKB normalisation. /api/universe/insert-ohlcv endpoint.
- **2026-02-27**: **EXPRESSION_ENGINE_V2 build plan** — LSP levels, HTF expressions, contextual AVWAPs. Task A: LSP Detector V2 (DataFrame-based, precomputed). Task B: 80 LSP expressions registered. Task C: HTF resampling + cache builder integration. Task F: All grinders use expr cache (unified computation path). EXPR_CACHE_WORKERS env var. OOM fix: true chunked submission + free universe cache after prep. Expression cache REQUIRED, all fallbacks removed. **Multi-pass pyramid grinder: daily→weekly→monthly sequential passes.**
- **2026-02-28**: Task 3.5 algo line rules added to ta_knowledge.md. **Algo line detector + expression integration (44 expressions)**. Pyramid grinder labels: Daily+LSP → Daily+LSP+Algo (4,141 exprs). All .md files updated: 12,175 exprs (4,017 daily + 80 LSP + 44 algo + 8,034 HTF).
- **2026-03-01**: 264 sig/pk3 with algo. Task 3.6 exit grinder upgrade (LSP, algo line, AVWAP base expressions). Exit grinder timestamped + latest save pattern. AVWAP base expressions + compute ops to exit grinder. Exit grinder hardcoded 100% example pass — no exceptions, no configurable override. Entry-relative expressions to exit grinder. **Task 3.7: multi-stage exit grinder** (standalone, preserves single-stage). Multi-stage exit grinder v3: multi-pass exhaustive search, single command. v4: full parallelisation across all CPU cores. Removed S99 backstop, require 100% condition fire on all examples. **MULTISTAGE_EXIT_GRINDER.md added** — architecture, rules, usage docs.
- **2026-03-02**: **Pipeline dashboard** — local web UI for step-by-step grinder control. **Railway pipeline dashboard** — remote job queue + live logs. **Signal filter — dedup + exit + rank for chart vetting**. Derive ADR floor from deduplicated example signal bars. Exclude existing examples from signal filter output. **v2 pipeline job polling on desktop agent**. Vetting UI + API endpoints (5+ commits). Pre-existing AI vetting groundwork — pending review gate (YES goes to pending, not straight to examples). Earnings overlay: purple E markers on earnings + reaction candles. Pipeline UI rename: signal_brute, sample_expansion, sample_review, setup_grinder_a/b, market_grind. Unified app: rail nav + setup analysis with steps 1–7 + nightly + watchlist placeholder + vetting embedded. Reload Samples button. AI sample review via Claude CLI. AI review with visible reasoning (the start of the AI second-pass vetting). Prefetch next 3 charts during vetting for instant flipping.
- **2026-03-03**: Pipeline fixed (BUG-002, BUG-003 from BUGS.md). Re-grind needed. Auto-clean dead jobs on every run (eliminate pipeline jamming). Fix matrix builder fingerprint mismatch — use cache even when expr counts differ. Grinder param inputs (beam/depth/peak) to pipeline UI. Earnings dates DB cache + nightly scrape. Wire SUBMIT FOR AUDIT to agent pipeline (fully automated AI review). Convergence-based quality score on home page + grind history. Pending review gate: YES goes to pending, not straight to examples. AI sample review via Claude CLI. Expert-level AI review prompt with quality grading. Agent auto-reviews pending samples every 15s. Honest AI review prompt: only checks what it can actually evaluate. AI review: pass image as file path in prompt.
- **2026-03-04**: Add profit grinder (Step 4) — exit grind from entry bar high. Step 4 setup grinder: blackout re-grind + condition pruner + signal_filter --conditions-file. Add setup_refiner.py (merged condition prune + signal filter). Stage isolation: blackout re-grind writes to separate files. Two rule violations fixed: profit_grinder parallelism + pyramid_grinder no-abort. Setup_refiner: load exit condition from profit grinder not signal_exit_grinder. Pruning: in-memory pass (seconds not hours), isolated upload endpoint. Replace prune_conditions with pass-through (skip expensive rescan for now). Save filter_power per condition; prune_conditions uses JSON data only. Step A/B/C/D: split setup_grinder → 4a/4b, exit grind choice endpoints, ExitGrinderPage (4a), 4b update. Profit_grinder uses exit expression library. Server reads new profit_grinder output format.
- **2026-03-05**: Parallelise grind_exits across all CPU cores. **Profit grinder rewritten to use expression cache (12,175 exprs) instead of exit expression library (446 exprs).** Multistage_exit_grinder rewritten same. Exclude boolean aggregations from exit grinders, bump thresholds to 50, median-primary scoring. Setup_refiner reads 'results' key. Exit direction handles 'below'/'above'. Setup_refiner reads signals from pyramid JSON instead of re-scanning universe. Step 4 rebuild session complete, grinder architecture documented. Condition pruning blocker — fix via leave-one-out on expr cache. **Optimise LOO pruning: single-pass boolean matrix — ~50-100× faster.** Skip cache-excluded tickers (BRK-B, SMMT, VUZI). Load only 87 needed columns from NPZ instead of all 12,175. Step 4 complete, Step 5 Market Grinder next. **Market Grinder design: winner/loser classification.**

Era 1's 19-day arc went from "bulk-fetch yfinance + plot charts" to "12K-expression multi-pass pyramid grinder + multi-stage exit + profit grinder + AI vetting + pipeline UI" — feature surface that took most projects of this size months to assemble. The fragility that emerged in Era 2 (BUG-001 D1 over-locking, BUG-002 pipeline-agent step ID mismatch, BUG-003 grinder→Railway upload) was a direct consequence of that pace: each component was wired in fast enough that integration drift accumulated unnoticed until Dan ran a live audit.

---

## Era 2 — The bounding-box grinder stack (2026-03-06 → 2026-03-22)

This era is Style 1's first full build-out. On **2026-03-06** Dan committed `PIPELINE_V2.md` — the architectural pivot away from the v1 Railway-centric flow toward a 7-step pipeline run as direct subprocesses on the desktop. Within minutes the v1 docs went into `archive/shelved_docs/`: `DARTBOARD_DESIGN.md` (additive scoring washes out discrimination — Dan tested it and rejected it), `MULTISTAGE_EXIT_GRINDER.md` (folded into a future profit grinder), `REFINEMENT_GRIND_FIX.md`, `TODO_REWRITE.md` (LSP rewrite plan abandoned), and an early `EXPRESSION_ENGINE_V2.md` plan that was incomplete. The pivot's premise was that v1's distributed wiring had become too brittle to debug; v2 would be a single local pipeline owned end-to-end.

The grinder substrate stayed: a brute-force search over the expression cache, scoring expression bounding boxes (low/high pairs) on the example set with a 5% margin and rejecting any condition that did not pass 100% of examples. The pyramid grinder organised this hierarchically — D1 as the seed tier, then 1wk, 1mo, 3mo, 6mo, 1yr, each searching the universe survivors of the previous tier with a `peak_target ≤ 3` signals/day cap. The arithmetic was clean: 100% example pass × peak-target ≤ 3 × multi-pass D→W→M expression library = "signal count must be low and consistent — 2-7/day," in the words of `BUGS.md`.

The first failure of Style 1 was discovered on **2026-03-06** during an audit and entered the BUGS file as **BUG-001 — "D1 Tier Over-Locking Destroys Downstream Tiers."** Grind #4 with 62 examples produced 168 raw signals; grind #5 with 68 examples produced **1,691** — a ten-fold increase from a six-example delta. Root cause as written at the time: D1's beam search greedily locked all 29 conditions it could find on the larger example set; those 29 conditions filtered the 1wk matrix from ~1,364 surviving rows down to 166. The 1wk tier then had nearly nothing to grind, so it locked weak conditions; everything below collapsed. The recorded fix was a hard cap at `D1=15` conditions. Two more bugs followed in the same audit window: BUG-002 (pipeline-agent step ID mismatch — every UI button posted "Unknown step" because the v2 step IDs were never wired into the agent), BUG-003 (grinder results never uploaded to Railway, so the UI was reading 2026-02-23 phase-1 spiderweb data while the desktop had real grinds going). Both were fixed by **2026-03-08**.

`PIPELINE_V2.md` then formalised the seven-step vetting loop: Signal Grind → Exit Grind → Refinement Grind → Vet → EV Grinder → Scan Tuning → Profit Grind → Health Check, with health-driven promote/revert versioning. The "tight for building, loose for live" doctrine emerged in the same document — a depth slider exposed by the refinement grinder would be cranked to maximum during example-building (deliberately overfit, fast vetting) and then dialled down for live scanning to lower curve-fit risk. This was the first acknowledgement in the doc surface that Style 1 had a curve-fit problem, but the proposed remedy was a runtime knob, not a methodological re-think.

Through mid-March the grinder stack was hardened: depth progression saved per level (2026-03-20), Scan Tuning UI built (two tabs, SPY bubble chart, auto-save settings), example matching switched to hardcoded `entry_date` after bar-index drift across cache rebuilds was caught (2026-03-20). The `EV_GRINDER.md` design landed: ~4M correlative features (256 instruments × 15,805 expressions + setup-specific OHLCV + Yahoo fundamentals) screened by univariate decile WR/MFE spread, dedup'd at |r|≥0.95, scored as percentile-weighted average per signal. By **2026-03-21** Dan had reached "DTSS Phase 4 complete" — Phase 2 grinder result of 78% WR / 6.4 ADR median across 182 conditions, and EV grinder operational. On paper the system was finishable.

What the documentation did not record at the time, but the research over the next month revealed, was that the 78% WR / 6.4 ADR figure was an *in-sample* statistic on the very example set the conditions had been grown to fit. Walk-forward validation of pyramid output had not yet been performed. The bounding-box mechanic — pick the min/max of an expression value across the example set, optionally widen by 5% margin, and require every live signal to fall inside — is mathematically a memorisation of the example set's edges. With 60-odd examples and a 16k expression library, the system could always find a stack of conditions that fenced in the examples while excluding the universe; the question of whether those conditions would generalise to a 61st example was deferred. The deferral lasted six weeks.

Two adjacent design decisions also locked in here that would constrain every later attempt:

1. **Expression cache as substrate.** `EXPRESSION_ENGINE_V2.md` declared the 15,805-expression library "complete and not being rebuilt. The brute force search against this library is the correct method. What changes is what the search is optimizing against." This pinned the project to scoring against a precomputed feature library rather than learning representations from the OHLCV directly. Later worktree probes (`long_lookback_probe_results.md`, **2026-03-07**) confirmed that longer-lookback structural features had median |Pearson| of 0.94+ with existing pool expressions — the library was already correlation-saturated, and adding new features mostly added redundancy.

2. **Same algorithm across all setups.** The strip-bespoke commit on 2026-02-23 had codified this; every spec from `SIGNAL_GRINDER.md` onward enforced "no per-setup branches in code." Given the family resemblance across setup examples, this was the right principle — a single mechanism *should* be able to surface them. That the same mechanism would later fail differently per setup (DTSS finding a working stack, BF/BASE/HTF not) under the avatar correlator was a signal that the failure was not in the per-setup branching but in the mechanism itself.

The era closed with a complete, working Style 1 stack ready for its first overnight grind cluster. The conditions for Era 3 were now in place: the consensus pipeline would run the grinder many times to test stability of its output.

---

## Era 3 — Consensus pipeline and the first walk-forward truth (2026-03-22 → 2026-04-27)

By **2026-03-21** Dan had connected the dots: the pyramid grinder's output was unstable across runs ("beam search instability" appeared in `TODO.md`). The same example set, the same expression cache, the same parameters — different signals out, run to run. Two responses emerged on the same day. The first was the **consensus engine** (`consensus_engine.py` added 2026-03-21), which would run the grinder many times and select only conditions that survived a stability + binomial test against shuffled-label nulls. The second was a complete teardown of the docs surface: `CONSENSUS_SPEC.md` was added and within hours merged into `SIGNAL_GRINDER.md` and `REFINEMENT_GRINDER.md` — the consensus mechanic was being treated as part of the grinder spec, not as a separate component.

The build was meticulous. The `v2-consensus` branch laid down 11 increments between **2026-03-23 and 2026-03-24**: CLI skeleton, `--output-dir` + Railway-mirror suppression, deterministic seeding, `--permute` for fake-example generation, `--scan-only` + `--conditions-file` plumbing, signal_exit_grinder `--conditions-file`, signal-mode rewrite (bootstrap z-score + condition locking with 5% margin), refinement-mode (two-test consensus + binomial, depth progression), `run_consensus_pipeline.py` orchestrator, and a self-verifying 9-step mini-pipeline test. By **2026-03-25** the first overnight consensus run had executed; the expr cache was 65GB on disk; the profit grinder's 24GB RAM spike on big populations was fixed by writing the forward-data matrix to a memmap during build (commit `7f36235`). The `test_consensus_pipeline.py --setup dtss` 9-of-9 pass on 2026-03-24 was the moment the consensus pipeline was declared mechanically correct.

Then the work pivoted sideways for a fortnight. The expression cache was migrated off yfinance to **EODHD** (2026-03-31), float16 storage cut 111 GB → some smaller footprint, the partial-candle engine eliminated HTF look-ahead bias (2026-04-01), AVWAP was extracted from the LSP/algo detectors (2026-04-02 — "AVWAP is independent of LSP and algo lines. Code says so, docs were wrong"), `OHLCV_CACHE.md` formalised the cache contract (2026-04-02), `FORWARD_PROP_SPEC.md` designed the four-file forward-prop append for nightly incremental work (2026-04-02), `setup_forward_prop.py` generated the lookback + state files (2026-04-02). The **forward-prop engine** itself landed on 2026-04-07 — one new bar per ticker in ~19 min vs. 124 min for a full rebuild. `CLAUDE.md` was added on 2026-04-07, formalising the project rules. By **2026-04-08** the intermediate cache (`.im` files — 196 columns rather than 16k expression columns) had replaced the .npz scan path in `signal_filter` for live scanning; the .npz cache became the grinder cache only.

This was the surface activity. Underneath, the consensus pipeline had not been run end-to-end on real data — every commit between 2026-03-26 and 2026-04-09 was either infrastructure or optimization, not validation. The actual real run was deferred for "speed optimization." Then on **2026-04-09** Sessions 2–5 of the consensus pipeline build resumed: bootstrap z-score in signal mode, refinement mode, orchestrator. **2026-04-10** brought the orchestrator full integration. **2026-04-11** documented "Session 5 deep optimization: bar count fixes + tradable filter + correct beam default" — and Dan added an iron rule to `CONSENSUS.md`: no quality cuts.

The same day, **2026-04-11**, the second major shelving wave hit `archive/shelved_docs/`: `EV_GRINDER.md` (deferred to a future "live EV ranked watchlist" build), `PROFIT_GRINDER.md` (same), `ANALYSIS_SYSTEM.md` (v1 conceptual overview, fully superseded), `HANDOFF_PARALLELIZATION.md`. `SHELVED.md` was renamed from `TODO.md` — a symbolic admission that the long roadmap had become primarily a graveyard of tried-and-discarded approaches. The `SIGNAL_GRINDER.md` rewrite the same day moved every promised feature into "Pending build."

Through mid-April the consensus pipeline still hadn't produced its production verdict. Optimization work continued: zstd-wrapped `.npz` storage codec replaced zlib, float16→float32 cast deferred to use site, sub-phase timers added (2026-04-11), zstd reader bug fixed (2026-04-12). Signal classification got a rewrite (2026-04-13) separating classification from management. By **2026-04-21** a CLAUDE.md update added the rule "Read ta_knowledge.md before ANY TA work" and Dan declared the **classifier rebuild mandate** — the prior `classify_cluster` + `forward_tape_panel` stack was scrapped. (Era 4 picks this up.)

The walk-forward truth landed on **2026-04-27**. A held-out research run on the BF setup, conducted in the `swing-screener-refinement-research` worktree, performed a 30/15 chronological split: train conditions on the first 30 chronological examples, then test whether the last 15 examples appeared in the resulting pyramid signal output. Result: **0 of 15 held-outs admitted**. The pyramid grinder, run as the project's load-bearing condition discovery engine for two months, did not generalise across time at all. The same finding's auxiliary data showed the pyramid had effectively been memorising example-specific bounding boxes; the boxes were tight by construction (5% margin around exact min/max) and any held-out example whose value fell outside any box was excluded by AND-conjunction.

This was the first hard look-out finding the project had produced, and it was a complete failure. The corollary findings stacked:

- The dual-gate analysis (per-condition shuffle-label null + winner-keep test) at calibrated alpha=0.01 admitted **0 of 15** scope/window combinations under greedy-by-IS-burn search and 0 of 15 under greedy-by-HOLDOUT-burn. Three "admissible" cases at alpha=0.05 (BF/365 d*=1 +2.1pp, BF/540 d*=9 +10.1pp, BF+BASE/270 d*=1 +0.3pp) all dropped at alpha=0.01 — they were partly false positives. The +10.1pp BF/540 case was kicking out 64% of held-out winners.
- Per-expression with FDR correction across all scope/window combinations: **only 7 expressions** cleared the dual gate across all setups combined. HTF picked one boolean ("ct_cmf20_positive_7") for +0.35pp lift. BF picked one extension expression for +2.10pp lift. BASE picked one HTF-weekly expression for +2.11pp lift. These are the post-overfit-protection condition counts — almost exactly what Dan summarised when reviewing this audit's framing as "the conditions melted way to just two conditions and let almost the entire market in."
- Rolling hold-out across 24 anchor dates × 5 scopes at 90-day window: admit rates 0–5% at calibrated alpha. The implication recorded in the findings file: rules are not cross-time-period robust; a rule that admits in one quarter likely won't admit in a different quarter; quarterly regrind is essential.
- The legacy "LOSS-vs-SAC differential" test that the refinement grinder spec had relied on was discovered to fail completely on every setup — sacrifice pool was the wrong baseline because labelled losses are *more* setup-shape similar to winners than the broad sacrifice pool is. The test had been measuring noise.

This is Era 3's load-bearing measurement: **under the specific overfit-protection regime the team applied (alpha=0.01, BH-FDR correction, dual-gate per-condition shuffle-null + winner-keep), the bounding-box-via-beam-search style on these example sets produced one or two marginal-lift expressions per setup that admit close to the universe.** Whether that means the style itself does not work, or whether the protection regime was too strict given the example count, was not separately tested. The `EV_GRINDER.md` design and everything downstream of it had assumed the upstream pyramid would produce a stable, condition-rich signal set; that assumption no longer matched the data the protection regime returned.

Era 3 ended with a doc surface that captured the failure honestly. `REFINEMENT_GRINDER.md` "Pending research — L14 overfit characterization" recorded the design hinge: the WIN-retention floor question. Four named options were laid out, none picked: 95% retention skips refinement on all three setups; 90% lets BF carve a small amount; 80% adds HTF; or derive the floor from a measured criterion (must exceed permutation-null differential carve, ~5% of pool). The L14 refinement design was placed in Pending build with that question unresolved. The implementation never started.

Two adjacent decisions during Era 3 also closed off avenues that would matter later:
- **2026-04-23**: the OHLCV cache distribution-adjustment was found buggy (ETF/dividend-payer bias). Cache was rebuilt properly. All numerics derived against the old cache had to be re-derived. This invalidated months of in-sample tuning evidence overnight.
- **2026-04-25**: a "Rescue (full)" commit pulled the presignal grinder + classifier infrastructure + ext50/research artifacts from a worktree and merged into v2 — a salvage of work that had been on a parallel branch. `CLASSIFIER_SPEC.md` and `PRESIGNAL_GRINDER.md` landed at root the same day. The parallel-branch instinct — research-in-isolation with the main branch frozen — would govern the rest of the project.

The pyramid grinder was, at this point, treated by the spec surface as not-overfit-protected. `SIGNAL_GRINDER.md` recorded: "Pyramid signals are not overfit-protected. Don't treat pyramid as ground truth. Agreement with pyramid is not forward validation. Walk-forward on the filter itself is the only validation." Whether that conclusion was correct, or whether the protection mechanism that produced the verdict had its own bugs/calibration issues, was not separately interrogated. The project entered Era 4 looking for a different upstream mechanism while leaving the pyramid in place as a non-promoted candidate generator.

---

## Era 4 — Presignal grinder, classifier rebuild, and the failure ledger (2026-04-21 → 2026-04-29)

Era 4 is where the team responded to Era 3's measured numbers by rebuilding the upstream substrate. On **2026-04-21** Dan declared the **classifier rebuild mandate** — the prior `classify_cluster` + `forward_tape_panel` stack was scrapped. The replacement would consume the **presignal grinder's** output as its input, not raw pyramid signals. The presignal grinder was the new conceptual upstream: a per-setup pre-classifier pool filter that admitted clusters whose pre-signal feature profile matched an example-defined region. The promise was 100% example pass rate by construction with universe admission tight enough to feed a downstream classifier.

`PRESIGNAL_GRINDER.md` was committed on **2026-04-25** in its current form, and its §3 reads as a failure ledger that catalogues, in order, every variation the project tried over the two weeks leading up to it:

- **§3.1 Cache-basis strict-AND** (16k features × N+1 bars, AND-conjunction over per-feature bounds): zero wild hits. Compound-probability collapse at scale — every feature's individual ~50% admit rate compounded past 16k features admits effectively zero universe bars.
- **§3.2 Two-stage composition** (4-axis OHLC outer + 8k inner): zero wild hits. Same collapse; Stage 1 effectively pass-through, Stage 2 inherited the same compound-probability problem.
- **§3.3 F1 hull + 5-descriptor Location** (the geometric bounding-box approach): calibration-spread 167× across setups versus a ≤10× target. Four of the six Location descriptors were near-pass-through (85–100% admit), so the effective filter was F1+D1+D4b — and the volume of admitted bars tracked F1 hull-area variance rather than carrying any structural signal.
- **§3.4 MA-corridor cells with reject-rate kneedle**: ~500× too loose at projection. Effective rank 2–7 across 287–831 cells — the cells were pathologically redundant.
- **§3.5 σ-cloud union bands with LOO walk-up**: rejected 2026-04-21 because σ blew out from one volatile example (AXTI) and the LOO-drop ordering inverted relative to spec. The **single-example-band-edge sensitivity** that would later reappear in the L14 labeler with OSCR.
- **§3.6 Carve-greedy weekly chain** (2026-04-27): structurally capped at length 4 on BF. Anchor-pinning was incompatible with breakout setups whose terminal events fire at the anchor itself.
- **§3.7 Expression-trajectory sign-coherence** (2026-04-27): walk-forward false-rejection rate far above target. The 100%-agreement gate on the high-dimensional cell pool overfitted regardless of the ensemble approach used.
- **§3.8 Pyramid as operational closer** (2026-04-27): disqualified — the same 0/15 BF held-out finding from Era 3, formally moved into the presignal failure ledger.
- **§4 Bbox-on-WIN** (the basic L14 refinement primitive): walk-forward false-rejection rate 73–90% across setups. Point memorisation; no forward generalisation.

In parallel — and the parallelism is important because it shows how many independent attacks on the same wall ran at once — the **`presignal-quality-research` worktree** ran an 8-hour autonomous overnight session on **2026-04-28 → 2026-04-29** building 50 engines and 30 scripts, all targeting one operational metric: ≤0.01% universe admission with 100% held-out coverage on multi-cut walk-forward. The session reached the admission target with two stack variants (V33 per-bar Euclidean chunked + V47 chunked-Mahalanobis) — admission of 0.0013–0.0026% across 25/20, 30/15, and 35/10 chronological splits, with 100% held-out coverage and full-population permutation null pass. The mechanism that achieved this was **per-bar / per-chunk strict-AND on Z-norm trajectories**: split each 50-bar Z-norm trajectory into K chunks, set each chunk's threshold to the max distance over the chunk's example bars only, AND across chunks. ANDing many tight per-bar bands compounded dramatically.

Then leave-one-out validation was run. **Per-engine LOO admit was 27–75% across the chunked engines; compose LOO across the full stack was 0/44 on V33, 1/44 (2.3%) on V47, 11.4% on V49 (drop two worst-LOO chunks).** The per-bar strict-AND mechanism was memorising example bar positions: each example often defined the edge of one chunk; dropping that example shrunk the chunk's bbox past the dropped example. This is the same compound-probability-collapse failure mode as PRESIGNAL §3.1, but now directly measured. In the runlog Dan and the agent named it explicitly: "100% held-out coverage requirement and LOO bank-robustness are in fundamental tension for chunked bbox engines. This is a real structural limit, not an optimisation I missed."

The session also tried multiplicative slack on the chunked thresholds:
- 1.10× slack: admission unchanged (0.0026%), LOO compose still 0/44.
- 2.0× slack: LOO compose 4/44, admission rose to 0.21% on the 25/20 cut — 21× the target.

LOO-self-distance threshold (instead of `max(test_d)`): held-out test coverage dropped to **0/20**. The LOO-tightened threshold was tighter than what held-out examples needed to admit. The cross-setup replication on V47 was equally damning: BF achieved admission and 15/15 coverage; HTF coverage dropped to 67–82% of held-outs; BASE to 56%. The "setup-agnostic" code worked uniformly; the *outcomes* did not — chunked thresholds calibrated on BF were too tight for HTF and BASE despite the family resemblance Dan would later flag in this audit.

The runlog's headline assessment is the single sharpest summary the project produced of where Style 1 ended:

> The chunked stack is deployment-ready for finding analogs of the 45 known BF examples but NOT robust to bank composition (LOO). For LOO-robust admission, fall back to V3+V5+V11+V16+V17+V18 stack (no chunked engines): admission ~8.5% with 100% LOO admit on Mahalanobis engines. ~330× looser than chunked.

That is the operational summary of Style 1 as built: the only configuration tested that retained robustness to a 46th example admitted 8.5% of the universe (~1100 bars/year); the only configuration tested that hit the operational tightness target (~3 bars/year) memorised the existing 45 example positions in a way that did not survive LOO. Whether a different chunking, threshold mechanism, slack policy, or pre-filter would split that tradeoff differently was not exhaustively explored — the runlog explicitly handed the choice to the user. The agent's own conclusion was that strict-AND on chunked Z-norm trajectories has a structural limit; that conclusion is one machine's read of the data, not a closed proof.

The **`refinement-overfit-research` worktree's** parallel finding on **2026-04-27** (which Era 3 covered) closed the analytical loop. The dual-gate framework (per-condition shuffle-label null + winner-keep test) at calibrated alpha=0.01 with BH-FDR multiple-comparison correction admitted **seven** expressions across all scope/window combinations on HTF/BF/BASE combined. Three of the seven shipped as the per-setup d=1 production rules:

| setup | scope | window | expression | hold-out lift | winner-keep |
|---|---|---|---|---|---|
| HTF | htf alone | 540 days | `ct_cmf20_positive_7` | +0.35pp | 34/37 |
| BF | bf alone | 365 days | `high_vs_xavgc50_atr14` | +2.10pp | 35/40 |
| BASE | bf+base combined | 540 days | `w_st_es_ext50_rsi7_lt_30_20` | +2.11pp | 59/59 |

These were the final residue of Style 1 once honest forward validation was applied. One single condition per setup, lift between 0.35 and 2.11 percentage points over baseline win rate. The conditions admit nearly the entire tradable universe — they are not setup scanners; they are mild WR-lift filters. This is precisely Dan's later phrasing in this audit's framing: "after implementing overfit protection the conditions melted way to just two conditions and let almost the entire market in."

Era 4 also produced two sharp diagnostic flips that, while not central to the scan-quality question, shaped how the team understood prior results:

- **2026-04-15, MFE_CAPTURE_PROJECT.md (parked 2026-04-17)**: the multi-exit OR-set grinder hit the 70% mean MFE capture target across all setups (HTF 0.762, BF 0.768, BASE 0.836, DTSS 0.795). Then the late caveats landed. First, capture was measured on winners-only — the curated examples were handpicked winners, not the full pyramid signal pool, and the 0.76–0.84 numbers were in-sample on a biased sample. Second, the optimisation objective `realized_move / per-example MFE` did not match the downstream consumer's job (winner/loser classification + mean ADR on winners). Third, the fire-bar-vs-MFE diagnostic *inverted* the project's premise: 53–79% of exits fired *before* the MFE bar. The "indicator lag ceiling" narrative that had motivated multi-stage exit work was wrong.
- **2026-04-26**: the L14 labeler shipped (`mfe_during_life ≥ T_setup`, T = `min(example mfe_during_life)`). The OSCR LOO sensitivity for HTF — drop one example, T jumps from 2.376 → 4.216 (+77%), admission drops 38.6% → 26.0% — was the same single-example-band-edge phenomenon flagged in PRESIGNAL §3.5. The labeler is, by construction, "as forgiving as the weakest example" in the bank. This was accepted as ship state.

By the close of Era 4, every variation of Style 1 the team had run had produced numbers ~10× off the operational target on at least one axis (admission, LOO, walk-forward FR, lift over null), or been formally deferred to `archive/shelved_docs/`. `MANAGEMENT_GRINDER.md` carried a deferral banner — "cannot be built until signal filter classification (Problem 1) is solved." `EV_GRINDER.md` and `PROFIT_GRINDER.md` had been shelved on 2026-04-25 to `archive/shelved_docs/`. `ENTRY_GRINDER.md` had been deferred — the v1 ratchet search returned 0 monotonic survivors out of 10,000 candidate paths, which the spec interpreted as either "no monotonic stop-tightening path holds all examples" or "the example set itself contains edge cases preventing any tight stop." The downstream stack that Dan flagged in this audit as working — the classifier, MFE measurement, EV grinder, exit/profit grinders — was not what was broken, and Era 4 made that ordering crisp: the upstream substrate had no output that downstream consumers could rely on.

The pivot to Style 2 happened almost immediately. On **2026-05-03** the nightly was stripped to infrastructure-only — "remove signal scan from pipeline." The auto-scan was retired. Era 5 begins with that retirement and a reframe of the upstream problem from "find conditions that fence the examples in" to "rank candidates by how much they look like the examples."

### Era 4 detailed catalogue — L14 refinement-overfit research (13 experiments)

The `swing-screener-refinement-research` worktree's autonomous research run on **2026-04-26 → 2026-04-27** executed thirteen numbered experiments under hard constraints: worktree-only writes, no spec edits, all winners + all examples must pass any rule by construction (full-pile bounds rebuild verified before shipping), forward-scan compatible, setup labels metadata only (algorithm identical across setups). The results, in the project's own words from `research/refinement_overfit_research/FINDINGS_2026-04-27.txt`:

| # | Experiment | Verdict |
|---|---|---|
| 1 | Stability across seeds (n_null=100 then 500) | Stable. d* @ alpha=0.01 across 10 seeds = identical (None — no greedy admit at calibrated alpha) |
| 2 | Rolling hold-out windows (24 anchor dates × 5 scopes at 90-day window each, ~109 dual-gate evaluations) | First-pick expression varies wildly across anchors; admit rate near random at calibrated alpha |
| 3 | Synthetic null sanity | Pure synthetic gaussian iid: 0% admit at any alpha. Real-data S2 (real BF data, random labels): 1/3 seeds had 72% admit at alpha=0.05 — motivated alpha=0.01 calibration. At alpha=0.01: real FPR ≈ 0.4% (under nominal 1%) |
| 4 | Hold-out window length | 90 days underpowered; 180 marginal; 365/540 days have admissible signal |
| 5 | Shuffle count convergence | n_null=500 plateau (spread ±0.04 vs ±0.25 at n_null=100; doubling to 1000 marginal) |
| 6+7 | Search method + depth halt | greedy-IS = greedy-HOLDOUT at calibrated alpha (both 0/15 admit); beam infeasible at 16K-feature scale; greedy natural halt is the depth rule |
| 8 | Inner sig gate on/off | 0/6 configs differ; on is 5–7× slower; ship OFF |
| 9 | NaN handling | P1 (NaN-as-outside) wins by spec match; no shipping difference |
| 10 | Significance alpha calibration | alpha=0.01 calibrated (real FPR 0.4%); alpha=0.05 inflates real FPR to 7% |
| 11 | Setup-restricted scope-pick | Pick by setup-restricted lift; metric agrees with loss-burn metric for 2/3 setups |
| 12 | Cross-class scope failure | N/A — no fade labelers exist |
| 13 | Bounding-box margin | 0% best; larger margins inflate eligible count without admitting more |

Per-scope rolling-hold-out admit rates at 90-day window across 24 anchor dates:

| Scope | n_anchors | admit @ alpha=0.05 | admit @ alpha=0.01 |
|---|---|---|---|
| HTF | 21 | 14% | 5% |
| BF | 20 | 15% | 0% |
| BASE | 21 | 10% | 5% |
| HTF+BF | 23 | 9% | 4% |
| HTF+BF+BASE | 24 | 8% | 0% |

The implication recorded in the findings file verbatim: "Time-stability is poor — a rule that admits in one quarter likely won't admit in a different quarter. Quarterly regrind is essential — don't let any rule run more than 1 quarter."

Top first-pick expressions per scope across anchors (showing how unstable the first pick was across time periods):

- HTF: 47(3), 45(3), 16163(3), 6816(2), 418(2)
- BF: 6016(6), 11999(3), 423(3), 10012(2) — `423` (high_vs_xavgc50_atr14) only takes over in the most recent 3/20 anchors
- BASE: 414(10) dominates, 9862(3), 594(1) — most stable per-period
- HTF+BF: 16163(10), 1401(4), 10037(2)
- HTF+BF+BASE: 821(6), 816(5), 250(2), 1388(2), 10037(2)

The seven expressions that cleared the per-expression FDR-corrected dual gate across all scope/window combos:

| scope | window | expr_idx | name | category | burn | win-keep |
|---|---|---|---|---|---|---|
| HTF | 540 | 3829 | ct_cmf20_positive_7 | boolean | 3 | 34/37 |
| HTF | 540 | 3830 | ct_cmf20_positive_10 | boolean | 2 | 35/37 |
| BF | 365 | 423 | high_vs_xavgc50_atr14 | extension | 5 | 35/40 |
| BF | 540 | 8591 | w_ct_c_gt_maxc50_1_10 | htf_weekly | 3 | 67/70 |
| BF | 540 | 5126 | ct_es_ext200_cci14_lt_neg100_30 | extension_structure | 3 | 65/70 |
| BASE | 540 | 2865 | ct_h_gt_maxh20_1_50 | boolean | 2 | 56/59 |
| HTF+BF | 365 | 3186 | ct_close_near_high_20 | boolean | 2 | 59/61 |

What did NOT survive (recorded verbatim in the findings):
- Greedy-by-IS-burn at calibrated alpha: 0/15 admit. Run B's three "admissible" cases at alpha=0.05 (BF/365 d*=1 +2.1pp, BF/540 d*=9 +10.1pp, BF+BASE/270 d*=1 +0.3pp) DROP at alpha=0.01 — partly false positives. The +10.1pp BF/540 case was kicking out 64% of held-out winners.
- Greedy-by-HOLDOUT-burn: 0/15 admit. Picks aggressive carve-rules; Gate 2 (winner-keep) catches them.
- Mahalanobis (full grid) / isolation forest / PCA-then-box / random-feature ensemble: most don't admit at calibrated alpha after correction.
- All-three-combined scope (htf+bf+base): 0 admit at any window. Adding BASE to the pool damages discriminability.
- htf+base scope: 1 admit at 540 days only.

The doc handed Dan four named operating-point options — A (95% retention floor → refinement skips on all three setups), B (90% → BF only), C (80% → HTF and BF, BASE skips), D (derive floor from a measured criterion such as "hold-out LOSS carve must exceed the population-permutation-null differential carve, ~5% of pool"). None was picked. The L14 refinement build never started.

### Era 4 detailed catalogue — Presignal-quality-research V1–V47 engine ledger

The `.claude/worktrees/presignal-quality-research/research/_runlog.md` is the headline narrative of the 8-hour overnight session on **2026-04-28 → 2026-04-29**. The session built ~50 numbered engines under the kickoff target of ≤0.01% universe admission with 100% held-out coverage on multi-cut walk-forward and a hard constraint that "100% of labelled examples pass through (invariant — no trimming)." The audit checklist (`_audit.md`) required every V to pass eleven items including determinism, no-bbox, direction-blind, setup-agnostic, ≥2× separation on its own, permutation null pass, walk-forward 30/15 + multi-cut, ≥80% LOO admit, bank-stability, and sample-size-honest CI reporting. Engines that failed any required item were quarantined to `research/_quarantined/` with a compliance-log row.

The trajectory of admission as the chunking mechanism was refined:

- V31 (10 chunks of Z-norm 50-bar close): 4.6% combined
- V32 (25 chunks): 1.96%
- V33 (50 chunks, per-bar): 0.59% on a 5k-bar sample
- V33 + V34 weekly + V35 weekly RS + V36 log-return + V37 monthly + V38 HL range + V39 volume + V40 open + V41 deltas + V42 typical + V43 low + V44 close-position-in-bar (each chunked the same way) + scalar engines V5/V11/V17/V18 + V16 sixstack mask = **0.0026%**

The final stack composition with per-engine solo admission:

| engine | feature | chunk count | solo admission |
|---|---|---|---|
| V33 | Z-norm 50-bar close (per-bar) | 50 | 1.60% |
| V34 | Z-norm 30-week close | 30 | 2.85% |
| V35 | Z-norm 30-week RS vs SPY | 30 | 3.33% |
| V36 | cumulative 50-bar log-return | 50 | 9.55% |
| V37 | Z-norm 24-month close | 24 | 3.95% |
| V38 | Z-norm 50-bar high–low range | 50 | 1.59% |
| V39 | Z-norm 50-bar log-volume | 50 | 1.74% |
| V40 | Z-norm 50-bar open | 50 | 1.64% |
| V41 | Z-norm 50-bar bar-level log-return DELTAS | 50 | **0.19%** (tightest single chunked engine) |
| V42 | Z-norm 50-bar typical price (H+L+C)/3 | 50 | 1.80% |
| V43 | Z-norm 50-bar low | 50 | 1.54% |
| V44 | Z-norm 50-bar close-position-in-bar | 50 | 3.44% |
| V5 | Mahalanobis on retained scalars (`pos_in_range_252`, `bars_since_252h`) | n/a | 56% |
| V11 | Mahalanobis on retained scalars (`bars_since_pivot_low`, `bars_since_window_high`) | n/a | 50% |
| V16 | §6 sixstack precomputed mask (binary) | n/a | 18% |
| V17 | Mahalanobis on retained scalar (`rs_pos_in_range_252`) | n/a | 65% |
| V18 | Mahalanobis on retained scalar (`n_higher_lows_30`) | n/a | 60% |

V47 was the same architecture upgraded to chunked-Mahalanobis (covariance-aware per-chunk thresholds with shrinkage=0.15, 25 chunks of 2 bars per chunk):

| split | univ admission | n bars admitted | held-out cov |
|---|---|---|---|
| 25/20 | 0.00128% (1/78,102) | 1 | 20/20 |
| 30/15 | 0.0000% (0/78,102) | 0 | 15/15 |
| 35/10 | 0.0000% (0/78,102) | 0 | 10/10 |

Per-engine LOO admit on V47 (drop each example, build filter from 44, check if dropped example admits):

| engine | LOO admit |
|---|---|
| chunked_close (V47-equivalent of V33) | 35/45 = 77.8% (vs V33 per-bar: 26/44 = 59.1%) |
| chunked_open | 33/45 = 73.3% |
| chunked_hl_range | 30/45 = 66.7% |
| chunked_delta | 31/45 = 68.9% |
| chunked_vol | 29/45 = 64.4% |
| chunked_cpos | 27/45 = 60.0% |
| chunked_logret | 40/45 = 88.9% |
| chunked_weekly | 38/45 = 84.4% |

The full trade-off across stacks tested:

| stack | 25/20 adm | 30/15 adm | 35/10 adm | per-engine LOO admit | compose LOO admit |
|---|---|---|---|---|---|
| V33 (per-bar Euclidean chunked + scalars) | 0.0026% | 0.0026% | 0.0013% | 27–75% | 0/44 (0%) |
| V47 (25-chunk Mahal × 9 chunked + scalars + V16) | 0.0013% | 0% | 0% | 60–89% | 1/44 (2.3%) |
| V49 (drop cpos+vol = 7 chunked + scalars + V16) | 0.020% | 0.0013% | 0% | 60–89% | 5/44 (11.4%) |
| V48 (5 best-LOO chunked + scalars + V16) | 0.4452% | 0.2771% | 0.0088% | 78–89% | 21/44 (47.7%) |
| Pure scalar (V3 + V5 + V11 + V16 + V17 + V18) | 9.83% | 8.54% | 5.28% | 97–100% | full LOO admit |

Cross-setup replication of V47 (same code, different bank):

| setup | n_examples | 25/20 adm | 30/15 adm | 35/10 adm | held-out cov |
|---|---|---|---|---|---|
| BF | 44 | 0.0013% (1) | 0% (0) | 0% (0) | 20/20, 15/15, 10/10 |
| HTF | 26 (8 too short) | 0% (0) | 0% (0) | 0% (0) | 9/11 (82%), 6/8 (75%), 4/6 (67%) |
| BASE | 35 | 0% (0) | — | — | 9/16 (56%) |

The runlog's verbatim assessment of the LOO problem: "The chunked per-bar strict-AND mechanism is **fragile to bank composition** — each example often defines a chunk's edge, so dropping that example shrinks the bbox past it. This is the §3.1 spec warning materialising — strict-AND over many per-feature bounds memorises example positions." Slack experiments confirmed: 1.10× slack didn't move LOO; 2.0× slack moved LOO compose to 4/44 but admission rose to 0.21% on the 25/20 cut (21× target). LOO-self-distance threshold (instead of `max(test_d)`): held-out test coverage dropped to **0/20**.

Engines V1–V30 plateaued at ~7–8% admission with all shape/state engines positively correlated 1.5–4× (joint / marginal-product ratio). The pre-chunk explorations included DTW, shapelet, multi-anchor, RS variants, joint Mahalanobis, supervised discriminator. The chunking breakthrough collapsed admission from 7% to 0.0013%. The final session output named two operationally distinct stacks with the user picking based on whether "100% admission target priority or LOO admit priority dominates."

### Era 5 detailed catalogue — Avatar HTF autonomous session 2026-05-04

The bank-curator worktree's `research/work_log.md` records a 3-hour autonomous metric iteration session on HTF with five constraints re-audited at every iteration:

1. Phases, duration, and ADR are MANDATORY
2. Setup-agnostic (same algorithm across all setups, no per-setup branches)
3. All 10 bank examples used (multi-anchor)
4. REMX, TRT, OGN, XE all must be near the top of the multi-anchor list
5. After every idea, re-audit against this prompt

Score modes tested (per-phase 3-d signature `(bars, net_adr, range_adr)`):

- `adr_sig` (per-phase 3-d signature): BEST
- `adr_bar` (bar-by-bar relative-L2): worse than `adr_sig` across all combines
- Length scales: raw bars (better) vs `length_frac` (worse — duration disregarded)

Combine strategies tested (× `adr_sig` with raw bars):

| Strategy | Result | Notes |
|---|---|---|
| S1_raw (sort by raw dist) | 1/4 | OGI dominates due to small distance scale |
| S2_median normalize | 1/4 | |
| S3_percentile | 2/4 | Each anchor's #1–#10 fills first slots regardless of quality |
| **S4_zscore** | **3/4** | TRT, OGN, XE in top 30 |
| S5_maxnorm | 1/4 | |
| S6_bank_sumsq | 0/4 | Duration² (43²=1849) dominates the normalizer |
| S7_within_anchor_rank | 2/4 | |
| S8_zscore_truncated (top 100) | 1/4 | Truncation messed up X scores |
| S9_log_dist | 1/4 | |
| S10_iqr_norm | 2/4 | |
| S11_rrf | 1/4 | Reciprocal rank fusion |
| S13_combined_zr | 3/4 | min(z, rank/100) — same as S4 in practice |
| S14_log_zscore | 2/4 | Z on log(dist) |
| S15_robust_z (median/MAD) | 2/4 | XE drops out (CRCL distribution too small) |
| S16_per_anchor_capped | 3/4 | (with z-score, equivalent to S4) |
| S17/S18 | 3/4 | Variants of S4 — same outcome |

Signature variants tested:

- length_bars + net_adr + range_adr (3 dims/phase): BEST, 3/4
- length_frac + net_adr + range_adr: worse (1/4) — duration disregarded
- 3 dims/phase + universal `drawdown_from_window_high_adr`: WORSE (2/4) — banks aren't always at-high
- adr_bar (bar-by-bar): worst across all combines

Bank inspection — all 10 HTF banks have 1–3 bar ignites:

| Bank | asof | K | n_bars | ignite | net_adr | range_adr |
|---|---|---|---|---|---|---|
| XPEV | 2020-11-11 | 3 | 54 | 3 bars | +5.05 | 3.39 |
| AFG | 2021-02-03 | 3 | 48 | 2 bars | +5.50 | 0.19 |
| OGI | 2021-02-05 | 3 | 61 | 1 bar | +2.08 | 0.00 |
| CR | 2023-10-30 | 3 | 66 | 1 bar | +3.78 | 0.00 |
| SOUN | 2024-02-23 | 3 | 122 | 2 bars | +5.69 | 0.22 |
| ASTS | 2024-05-24 | 3 | 89 | 3 bars | +6.41 | 2.77 |
| QUBT | 2024-11-19 | 3 | 16 | 2 bars | +7.39 | 4.26 |
| CRCL | 2025-06-12 | 2 | 6 | 3 bars | +1.58 | 1.58 |
| EQX | 2025-08-21 | 3 | 127 | 2 bars | +4.26 | 0.57 |
| PL | 2025-09-10 | 3 | 67 | 1 bar | +6.03 | 0.00 |

REMX biggest 1-bar move was 1.95 ADR — too small to match any bank's 4–7 ADR ignite. The session concluded "REMX structurally doesn't have a sharp rip" and that bank curation gap (no slow-accumulator HTF example) was preventing REMX from surfacing at any combine variant.

Cap-value sweep on the per-anchor diversity cap:

| cap | TRT | OGN | XE | result |
|-----|-----|-----|-----|--------|
| 3 | - | - | #4 | 1/4 |
| 5 | - | #5 | #6 | 2/4 |
| 6 | - | #5 | #7 | 2/4 |
| 7 | #7 | #5 | #8 | 3/4 (min cap for TRT) |
| 10 (production) | #7 | #5 | #11 | 3/4 |
| no cap | #7 | #5 | #30 | 3/4 |

Bonus experiments (post-baseline):

- At-high bonus (1/(1+drawdown_adr)): hurt TRT (drawdown 1.39 = reduced bonus). 1/4 with simple bonus.
- Log-compressed signature: 1/4 (compression hurt OGN/XE tight matches).
- Winsorized z-score: 2/4 (XE drops, CRCL distribution too small).
- Min-norm (dist / anchor's #1 dist): 1/4 (per-anchor top fills slots equally).
- Average z-score across anchors: 3/4 different positions (XE #1, TRT/OGN #16-17).
- Median z-score: 1/4.

ADR_LOOKBACK sweep — the breakthrough finding:

| ADR | REMX | TRT | OGN | XE | RMAX | result |
|-----|-----|-----|-----|-----|-----|--------|
| 14 | - | - | - | #11 | - | 1/4 |
| 20 (default) | - | #7 | #5 | #11 | - | 3/4 |
| 25 | - | #4 | #5 | #11 | - | 3/4 |
| 30 | - | #4 | #8 | #11 | #5 | 3/4 + RMAX |
| 35 | - | #5 | #10 | #11 | #3 | 3/4 + RMAX |
| 40 | - | #5 | #10 | #11 | #2 | 3/4 + RMAX |
| 45 | - | #5 | #10 | #11 | **#1** | 3/4 + RMAX |
| 50 | - | #5 | #10 | #11 | #2 | 3/4 + RMAX |
| 60 | - | #5 | #10 | #11 | #2 | 3/4 + RMAX |

Dan confirmed REMX was a typo for RMAX. With `ADR_LOOKBACK=30` the settled config achieved 4/4 of (RMAX, TRT, OGN, XE) in HTF top 11. Same metric verified setup-agnostic on DTSS and BASE (Dan confirmed BASE candidates look correct in TC2000). The session's verdict line: "Phase 1 (metric work) complete. Phase 2 (per-setup sanity filters) is the next session's focus."

Total iterations recorded: "30+ combine strategies × 4 score modes × multiple signature variants × cap-value sweep × ADR-window sweep × Pearson/cosine variants × at-high bonus × log compression × winsorization × multi-resolution combine. All variations at or below the 3/4 ceiling. REMX requires bank curation (no metric variation makes a multi-bar slow rally match a 1-bar sharp rip in any of the 10 banks)."

### Era 4 detailed catalogue — Presignal-quality-research script-level engine catalogue (V1–V50)

The `.claude/worktrees/presignal-quality-research/research/` directory contains every engine script run during the 8-hour overnight session. The shared loader+evaluator is `_engine_lib.py`; the audit checklist is in `_audit.md`; the headline narrative is `_runlog.md`. There is no `_compliance_log.md` and no `_quarantined/` directory — the formal pass/fail table format described in the audit checklist was never populated; the V-by-V record exists only in the runlog narrative.

`_engine_lib.py` exposes the canonical per-V audit `evaluate_engine(setup, build_features, feature_dim, min_history, n_univ_sample=20000, seed=42, distance_fn=euclid_min_to_bank, ...)` which for each engine: (1) builds features for every example at `sig_idx = e_idx-1`, (2) draws a shared deterministic universe sample (same `(seed, n_target, ohlcv state, bars_per_ticker)` produces byte-identical sample so per-engine pass masks compose cleanly), (3) runs multi-cut walk-forward (chronological splits at 30/45, 25/45, 35/45 train fractions; per split, train bank from train_idx, threshold = `max(test_d)`, compute `train_self_dist`/`test_dist`/`univ_dist`), (4) determinism check by re-running the primary 30/15 distance and `np.array_equal`, (5) permutation null with `n_ex` fake bars at `seed+99991` re-running through the same 30/15. The result JSON includes a `primary_split_dump` with per-univ-bar pass mask aligned to `(ticker, sig_date)` so `compose_engines.py` can n-way intersect.

`compose_engines.py` is the n-way AND helper: load any number of `engine_*_results.json`, intersect on `(ticker, entry_date)` for tests and `(ticker, sig_date)` for universe, AND the per-engine pass masks, report joint admission. For 2-engine composes also computes `ind_ratio = joint / (pa * pb)` (1.0 = exact independence; >1 = positively correlated). This is how the 1.5–4× correlation observation across V1–V30 was measured — the engines were not independent, so ANDing them did not produce the multiplicative carve they would have under independence.

**Pre-chunking engines V1–V30:**

- **V1** `engine_v1_znn.py` — seed engine. 50-bar Z-normalized close trajectory, 1-NN Euclidean to a 30-example chronological train bank, 15 held-out test. No feature picking. "The trajectory IS the feature." Standalone admission ~22.76% at 100% coverage.
- **V2** — V1 plus volume Z-trajectory and ATR(14)-normalized range Z-trajectory; combined distance = sqrt(d_close² + d_volume² + d_range²). Sum-of-squares dilutes any one informative channel.
- **V3** — V1 ported to `_engine_lib.evaluate_engine` for framework parity. `engine_v3_cross_setup.py` runs the same primitive on HTF and BASE.
- **V4** — `WK_LEN=30`-week Z-normed log-close trajectory anchored at the most recent closed weekly bar. Independence test against V3 daily.
- **V5** — ten canonical-TA scalar primitives (drawdown_252, pos_in_range_252, ret_20/50/200, rel_vol_20, atr14_pct_252, ma_fan_10_50, bars_since_252h, rsi_14). Each gated for ≥2× IQR-normalized median separation; survivors fed into Mahalanobis with shrinkage. Cross-setup port via `engine_v5_cross_setup.py`.
- **V6** — sign-coherence weekly cells (6 SMA periods × 30 weekly offsets × 2 sign tests = 360 cells) with explicit `MAX_UNIV_PASS` lift gate to drop universally-true cells.
- **V7** — cumulative log-return trajectory `log(close[i] / close[anchor-L+1])`, NOT Z-normed — preserves magnitude. Two stocks with same shape but 10% vs 200% rally are different.
- **V8** — V3 with `TRAJ_LEN=20` instead of 50. Shorter window may give tighter ball.
- **V9** — V3's 50-bar Z-norm trajectory but Mahalanobis distance with `SHRINKAGE=0.20` instead of Euclidean.
- **V10** — variable-length pivot-anchored trajectory: `pivot_idx = argmin(close[sig_idx-60+1:sig_idx+1])`, resample to L=30 by linear interp, Z-norm in log-close.
- **V11** — pullback-depth scalars (pullback_pct_60, bars_max_to_min, bars_min_to_now, rally_pct_from_pivot, ratio_rally_to_pullback) → ≥2× separation gate → Mahalanobis on retained.
- **V12** — V3 features but DTW with Sakoe-Chiba band W=5 instead of Euclidean. Tightens local-shape match at cost of higher universe match.
- **V13** — V2's three channels but per-channel threshold = max(test_d) per channel, AND admission. Replaces summed Euclidean with envelope AND.
- **V14** — concatenate V3 trajectory + V5 retained scalars + V11 retained scalars → joint Mahalanobis with shrinkage.
- **V15** — does NOT exist in the tree. The slot was either never built or implicitly absorbed by V14.
- **V16** — `engine_v16_sixstack_mask.py`. Loads the precomputed `bf_sixstack_wild_scan_mask.pkl` (`{ticker: bool[1583]}` aligned to cache_start `2020-01-02`). Binary admission gate. Standalone admission ~8.9–18%, 45/45 BF examples in the mask. The §6 sixstack itself is produced upstream by `bf_sixstack_wild_scan.py` from sign-coherence cells over MA stacks. V16 is just the lookup harness. Cross-setup note from runlog: V16 admits only 9/16 BASE examples — it is BF-specific.
- **V17** — daily relative-strength vs SPY scalars (rs_pos_in_range_252, rs_bars_since_252h, rs_slope_50, rs_change_252, rs_change_20). ≥2× separation gate → Mahalanobis on retained.
- **V18** — sequence-event counts (higher-highs/lower-lows in last 30 bars, longest streak of higher closes, inside/outside-bar counts). ≥2× gate → Mahalanobis.
- **V19** — V3's full 50-bar Z-norm trajectory but distance computed only on `RIGHT_K=15` last bars.
- **V20** — 30-week log-RS vs SPY trajectory, Z-norm, 1-NN Euclidean. The weekly-resolution analog of V17.
- **V21** — shapelet discovery: pool of ~1230 k-bar (k=10) subsequences from 30 training examples, score by `dist_universe - dist_examples`, keep top KEEP_TOP. Both ALL-must-match and ANY-can-match flavors tested.
- **V22** — mega-concat of V3 (50d) + V5 retained + V11 retained + V17 retained + V18 retained + V20 (30d) + volume Z-traj (50d) ≈ 136d. PCA fit on training, keep K = smallest cumulative-explained ≥0.95, Mahalanobis in PC space.
- **V23** — bars-since-event scalars (above_sma200/50/20 cross-up, macd_bullish_cross, 5pct_pullback). ≥2× gate.
- **V24** — `MO_LEN=24`-month Z-norm close trajectory. Multi-year structural arc.
- **V25** — K-means cluster training examples into K=3 clusters; bar admits only if within threshold of ALL K cluster centroids.
- **V26** — logistic-regression supervised discriminator: positive class = BF training, negative class = bars admitted by current compose stack but NOT in BF set. Threshold at max-train-score for 100% train coverage.
- **V27** — bull MA alignment binary (SMA10>SMA20>SMA50>SMA200, AND).
- **V28** — RS scalars vs IWM, QQQ, XLK, XLF, XLV (rs_pos_in_range_252 per benchmark), separation-gated.
- **V29** — N=20 random 80% sub-banks of training set; held-out admits to V29 iff ≥K of N sub-banks admit (consensus across lucky-fit-pruned banks).
- **V30** — generic K-chunk scaffold with per-chunk threshold = max(test_d), strict-AND across chunks. The mechanic that V31–V44 specialise.

V1–V30 plateaued at ~7–8% admission. None individually hit the 0.01% target. The runlog records that all shape/state engines were positively correlated 1.5–4× (joint / marginal-product ratio), so composing them compounded poorly.

**Chunking engines V31–V47 (the breakthrough):**

The chunking mechanic is identical across V31–V44: `chunked_pass(train, test, univ, n_chunks)` slices feature dimension into `chunk_size = L // n_chunks` chunks, computes per-chunk Euclidean distance, sets `thr_c = max(test_d_c)`, ANDs `(td <= thr_c) & (ud <= thr_c)` across chunks. Strict-AND of many tight per-bar bands compounds carve dramatically even with high inter-bar correlation, *because over a few bars within an example, examples agree very tightly within their own neighborhood*.

- **V31** `engine_v31_chunked10.py` — `TRAJ_LEN=50, N_CHUNKS=10` → 5-bar chunks of V3 close trajectory. Combined admission ~4.6%.
- **V32** — `N_CHUNKS=25` → 2-bar chunks. Combined ~1.96%.
- **V33** — `N_CHUNKS=50` → 1-bar chunks (per-bar bbox). Solo admission 1.60%, in compose stack reaches 0.59% on 5k sample. `engine_v33_protected.py` adds the per-chunk null lift gate (drops chunks with `lift = null_thr_median / real_thr < 1.5`).
- **V34** — `WK_LEN=30, N_CHUNKS=30` (one per-week chunk on V4's weekly Z-norm log-close). Solo 2.85%.
- **V35** — `WK_LEN=30, N_CHUNKS=30` on V20's weekly log-RS-vs-SPY trajectory. Solo 3.33%.
- **V36** — `TRAJ_LEN=50, N_CHUNKS=50` on V7's cumulative log-return trajectory (preserves magnitude). Solo 9.55%.
- **V37** — `MO_LEN=24, N_CHUNKS=24` per-month chunks on V24's monthly Z-norm log-close. Solo 3.95%.
- **V38** — `TRAJ_LEN=50, N_CHUNKS=50` on Z-normed `(high-low)` per bar. Solo 1.59%.
- **V39** — Z-norm log-volume trajectory, 50 chunks. Solo 1.74%.
- **V40** — Z-norm open trajectory, 50 chunks. Solo 1.64%.
- **V41** — first-difference `log(close[i]/close[i-1])` Z-norm, 50 chunks. Solo **0.19%** — tightest single chunked engine because bar-level momentum has fast-decaying autocorrelation.
- **V42** — typical price `(H+L+C)/3` Z-norm, 50 chunks. Solo 1.80%.
- **V43** — Z-norm low trajectory, 50 chunks. Solo 1.54%.
- **V44** — close-position-in-bar `(close-low)/(high-low)` Z-norm, 50 chunks. Captures bar-shape sequence, level/direction-independent. Solo 3.44%.
- **V45** — per-chunk Mahalanobis instead of per-chunk Euclidean. `TRAJ_LEN=50, N_CHUNKS=10` (5-bar chunks), `SHRINKAGE=0.30`. Per-chunk fits a 5×5 shrunk-cov Mahalanobis on bank, threshold = max bank-LOO-self per chunk. Smoother ellipsoidal bbox, less example-position-memorisation than per-bar V33.
- **V46** — V45 mechanic ported to 7 feature spaces: close, open, hl_range, delta, vol, cpos, logret. All 50-bar trajectories chunked into 10 × 5-bar chunks per-chunk Mahalanobis, strict-AND across all engines.
- **V47** — `engine_v47_chunked_mahal_tighter.py` — the headline stack. `N_CHUNKS=25` (2-bar chunks), `SHRINKAGE=0.15`, `N_UNIV=80000, BARS_PER_TICKER=18`. Nine chunked-Mahal feature spaces (close, open, hl_range, delta, vol, cpos, logret, weekly@WK_LEN=30/15-chunks, monthly@MO_LEN=24/12-chunks). Plus V5/V11/V17/V18 scalar Mahal + V16 sixstack. Cross-setup ports `engine_v47_base.py` and `engine_v47_htf.py` for BASE and HTF.
- **V48** `engine_v48_loo_balanced.py` — keeps only the 4 best-LOO chunked engines + scalar Mahal + V16. LOO compose 47.7%, admission rises 30–45×.
- **V49** `engine_v49_drop2.py` — drops the 2 worst-LOO chunked engines (cpos+vol). 11.4% compose LOO, 0.020/0.0013/0% admission across multi-cut. **Recommended deployment** per the runlog.
- **V50** `engine_v50_50chunks.py` — V49 with 50 per-bar chunks instead of 25 × 2-bar.

**`protected_*.py` series — per-chunk null lift gate.** Built on top of the chunked stack. Common shape:

```
for c in range(n_chunks):
  thr_real = max(test_d_c)                     # chunk-c threshold from test → train bank
  for k in range(n_null):                       # 10 null folds
     fake_bank = univ_c[rng.choice(...)]
     fake_test = univ_c[rng.choice(...)]
     null_thrs.append(max(fake_test→fake_bank))
  lift = median(null_thrs) / thr_real
  if lift >= gate:                              # keep this chunk
    test_pass &= (td <= thr_real)
    univ_pass &= (ud <= thr_real)
    n_kept += 1
```

The deployable `protected_compose.py` is `LIFT_GATE=1.30, N_NULL_FOLDS=10, N_UNIV=80000, BARS_PER_TICKER=18`. The four protection-knob sweeps are `protected_strict.py` (`LIFT_GATE=1.50`), `protected_super_strict.py` (`LIFT_GATE=2.00`), `protected_slack.py` (`LIFT_GATE=1.30` plus `thr_real * 1.10` multiplicative slack on the per-chunk threshold), and `protected_slack2x.py` (`LIFT_GATE=1.30` plus `thr_real * 2.00` slack). Compose AND is taken across all chunked engines + Mahalanobis V5/V11/V17/V18 + binary V16.

**`bank_stability_check.py`** — `N_BOOTSTRAP=10, KEEP_FRAC=0.90` random subsamples of the 30-example training bank; reruns the full protected compose for each. Headline: mean 0.0022%, stddev 0.0006%, range 0.0013–0.0025%, all maintain 15/15 held-out coverage.

**`loo_validate.py`** — strict leave-one-example-out per engine. Per-chunk threshold = `max bank-LOO-self distance × CHUNKED_SLACK` (default 2.00). For Mahalanobis engines, refits Mahal on the bank without `i`, computes max-of-LOO-self-dist over the remaining 44, checks if dropped example admits without slack. Per-engine LOO admit numbers in `_runlog.md`: V5 43/44, V11 44/44, V17 43/44, V18 43/44, V16 44/44, V33 26/44, V36 33/44, V38 18/44, V39 13/44, V40 21/44, V41 12/44, V42 22/44, V43 23/44, V44 17/44. Compose LOO 0/44 for V33-stack, 1/44 for V47, 5/44 for V49, 21/44 for V48.

**BF-chain experiments (`bf_chain_*`, `bf_event_chain_*`, `bf_expr_trajectory_*`).** Alternative non-chunked approaches that pre-date and parallel the V31+ work. They search for ordered behavioral sequences instead of geometric bboxes:

- `bf_behavioral_sequence.py` — pure binary state/event mining. STATEs (e.g., `close > SMA_p`, `SMA_8 > SMA_21`, `SMA_p[t] > SMA_p[t-1]`) recorded at E-1; EVENTs (cross-up, cross-down, new-high, new-low, inside-bar, etc.) recorded as most-recent-occurrence offset across `[E-100, E-1]`. Universal STATEs = TRUE in 100% of 45 examples. Universal EVENTs = at least one occurrence. Order = sort universals by median occurrence offset.
- `bf_event_chain.py` / `bf_event_chain_v2.py` — strict-precedence DAG on universal events. For every (A,B) pair, check if `last_A_offset > last_B_offset` in 100% of 45 examples (A always precedes B). v2 extends with state-transition synthetic events and reports near-strict (≥95%) candidates.
- `bf_chain_state_machine.py` — walks lookback oldest-to-newest with a state machine: `state` advances when `chain[state]` fires. Greedy by median first-occurrence offset (oldest first). Stops at length ~9.
- `bf_chain_beam.py` — `BEAM_K`-wide beam search across orderings (flagged in `_audit.md` as anti-pattern but kept for reference). Length-12 chain achieved.
- `bf_chain_carve_greedy_weekly.py` — primary deliverable for the chain track. 5,045 entities (45 examples + 5,000 random universe samples). Greedy step: among 101 strict-universal weekly-event candidates, pick the one that keeps 100% of examples advancing AND drops the most universe samples. **Stopped at length 4.** Anchor-pinning incompatibility — breakout setups whose terminal events fire at the anchor.
- `bf_chain_clean.py` — single unified state-machine codepath for both example chain derivation and universe scan. `bf_chain_universe.py` and `bf_chain_universe_mp.py` are the universe-scan harnesses. `bf_chain_v2.py` is the longest-universal-AND-THEN derivation over the full event pool.
- `bf_chain_triangulate.py` — cross-checks the carve-greedy chain against pyramid signals. `bf_chain_stop_diagnostic.py` answers "what stopped the carve-greedy chain at length 4" — for each of 45 examples, replays the 4 events, checks what blocks each remaining 97 candidate.
- `bf_expr_trajectory_filter.py` — port of §6 sign-coherence to the FULL 16,216-expression cache instead of just MA cells. Each cell asks: sign of `expression[E-1-k] vs expression[E-1]` for daily offsets k=1..N_DAILY-1. Keep cells where all 45 examples agree on sign. 778k-cell pool. `bf_expr_trajectory_walkforward.py` measures FR stratified by prior-count. `bf_expr_trajectory_consensus.py` does chronological-cutoff consensus and finds it mathematically reduces to the strictest cutoff alone. `bf_expr_trajectory_random_consensus.py` uses N independent random 80% subsamples — the actual consensus-overfit-protection.

**Bare-boolean / clustered-consensus / pyramid-with-sixstack-mask.**

- `_bare_boolean_consensus.py` — pyramid grinds lock wrapped variants (`ct_X_N`, `st_X_N`, `tir_X_N` of an underlying boolean), so name-level consensus under-counts. This script maps wrapped-condition names to bare-boolean concepts and counts grinds where ANY wrapped variant of a bare boolean appeared.
- `_clustered_consensus.py` — cluster-level consensus: cluster expressions by Jaccard similarity ≥ 0.95 on their firing pattern across the §6-active universe. A cluster appears in a grind if any of its members appears.
- `_pyramid_with_sixstack_mask.py` — subprocess wrapper that monkeypatches `pyramid_grinder.compute_tradable_masks` so each ticker's per-bar tradable mask is AND'd with the §6 sixstack mask before pyramid runs. No edits to `local_runner/`.

**Expression-cache codec experiments.**

- `_expr_cache_fast.py` — memmap raw `.npy` (float16) decompressed once at orchestrator startup, plus `.dates.pkl`. Workers `np.lib.format.open_memmap` per ticker — zero-copy, OS-paged. Slicing `data[:, candidate_indices]` only pages the candidate-column bytes. Beats canonical zstd `.npz` (~218 sec CPU per tier × 14 tiers).
- `_expr_cache_lz4.py` — full-data LZ4 frames in a custom `'EL41'` magic format. LZ4 ~3× faster decompress than zstd at ~50% larger files. Same 16,216-column shape so pyramid interface unchanged.
- `_expr_cache_passcols.py` — pass-column-restricted LZ4: pre-encode per-(ticker, pass) files containing ONLY that pass's columns (pass 1 = daily/LSP/algo ~5,504; pass 2 = htf_weekly 5,233; pass 3 = htf_monthly 5,233). Pyramid throws away ~70% of columns per tier, so this avoids decompressing dropped columns at all. Magic `'PCL1'`.

The `_compliance_log.md` formal table format described in `_audit.md` was never populated. The session's verbatim assessment in `_runlog.md`: "The chunked stack delivers the admission target. Multi-cut WF, perm null, bootstrap, and per-chunk null lift gate are the overfit protection layers that pass. LOO failure is a structural limit of the strict-AND mechanism; the alternative (looser threshold) violates the admission constraint."

### Era 4 detailed catalogue — Refinement-overfit research script-level catalogue

The `swing-screener-refinement-research/research/refinement_overfit_research/` directory contains every experiment script for the L14 dual-gate research session. Core infrastructure:

**`loader.py`** — pins read paths to the main repo cache (`MAIN_REPO/local_runner/cache/`), never writes back. `load_setup(setup, ...)` reads `signal_exit_pool_{setup}.json` (labeler), the universe OHLCV pickle, the `{setup}_passes.pkl` presignal pass file, and the latest non-blackout/non-refinement `pyramid_{setup}_mp_*.json`. Categorises labeler `cluster_meta` rows into WIN (status=ENTERED, final_label=WIN), LOSS (ENTERED+LOSS), and SAC (everything else: SKIPPED, REDUNDANT, NOENTRY, presignal-only, pyramid-only). Resolves each row's `signal_bar_idx` to an ISO date via the OHLCV frame. `build_feature_matrix(rows, ...)` looks up the expression-cache row for each (ticker, signal_bar_idx) and stacks into `(n_rows × n_expr) float32`.

**`core.py`** — matrix building + the legacy beam search. Constants: `SAC_SAMPLE_N = 20_000`, `SAC_SAMPLE_SEED = 7`. `build_setup_matrices(setup, ...)` returns `M_win`, `M_loss`, `M_sac` plus row metadata. `bounding_box(M_win)` = `(np.nanmin, np.nanmax)` per column. `valid_winner_mask(M_win)` = True per expression iff every WIN is non-NaN. `outside_mask(M, lo, hi)` = `(M < lo) | (M > hi) | np.isnan(M)`. `carve_per_condition(M_loss, M_sac, lo, hi)` returns per-expression LOSS-outside-rate, SAC-outside-rate, and the gap. `beam_search(M_win, M_loss, valid_mask, beam_width=200, max_depth=20, ...)` is the original LOSS-survivor-minimising beam.

**`cache_matrices.py`** writes the precomputed matrices to `cache/{setup}_matrices.npz`. All downstream experiments call `load_cached(setup)` rather than re-resolving the labeler.

**`methods.py`** — six refinement methods (A–F) used by `run_all_methods.py`. Centerpiece: `per_expression_significance(M_win, M_loss, valid_mask, n_perm=200, seed=42)` shuffles labels across `np.vstack([M_win, M_loss])` n_perm times, recomputes the box from the shuffled "winner" half, counts how many shuffled losers fall outside; returns per-expression `real_elim`, `null_p95`, `null_mean`, `null_counts`. Methods A–F use this `sig` dict as a gate (`sig["real_elim"] > sig["null_p95"]`):

- **Method A** — per-expression filter UNION → AND. Each expression where `real_elim > null_p95` independently ships its WIN box; final rule = AND of all surviving boxes.
- **Method B** — greedy stacking with significance gate. At each step picks max-marginal-burn expression among those passing the per-expression sig test.
- **Method C** — beam search (width 200) restricted to sig-passing expressions.
- **Method D** — `sklearn.RandomForestClassifier` on sig-passing eligible features (NaN/inf imputed with column median); top-K by `feature_importances_`; ships those features' WIN bounding boxes as conditions.
- **Method E** — `sklearn.IsolationForest` per-feature univariate ranking. Trains a per-feature `IsolationForest(n_estimators=50)`, scores LOSS rows, ranks by `(baseline_loss_score - per_feature_score)`. Top-K features ship their WIN bounding boxes.
- **Method F** — diagonal Mahalanobis used as a feature ranker only: ranks features by `mean(|z_loss|)` where `z = (X_l - win_mean) / win_std`. Top-K ship their WIN bounding boxes.

**`dual_gate_runner.py`** — the core mechanic for the entire experiment battery. Imported by experiments #1, #2, #3, #5, #8, #9, #10, #13. Key functions:

- `load_setup_pool(setups)` vstacks `M_win` and `M_loss` across multiple setups into a "scope" pool, keeping parallel `win_dates`, `loss_dates`, `win_is_example`, `loss_is_example` arrays.
- `split_holdout(pool, days)` date split: `cutoff = max_date - days`, train ≤ cutoff, test > cutoff.
- `greedy_with_holdout(M_win_train, M_loss_train, M_win_test, M_loss_test, max_depth=None, margin_pct=0.0)` per-step picks `argmax(out_l_train[~burned_train].sum(axis=0))` over remaining-eligible expressions; halts when no expression burns any remaining trainer-loser. Records `real_test_loss_burn[d]`, `real_test_win_keep[d]`, `real_train_loss_burn[d]` at every depth. Box defined by `win_bounding_box(M_win_train)`, optionally widened by `margin_pct * (hi - lo)`.
- `_worker_run_one(seed)` per-permutation worker: stacks `pool_train = vstack([M_win_train, M_loss_train])`, permutes, splits back into fake winners/losers preserving `n_w`, runs greedy, returns the same depth arrays computed on the REAL test pile.
- `run_null_distribution(...)` `ProcessPoolExecutor` with `_worker_init` initializer; pads short permutation runs by repeating the last value (greedy halt); returns `(n_null × max_depth)` arrays for `null_test_loss_burn`, `null_test_win_keep`, `null_train_loss_burn`.
- `evaluate_dual_gate(real_depths, null_dist, alpha=0.05)` Gate 1: `real_loss > p95(null_loss)`. Gate 2: `real_win_keep >= p5(null_win_keep)`. `admissible = gate1 & gate2`. Picks `d*` = argmax `(real_loss - null_mean_loss)` over admissible depths.
- `build_shipping_rule(M_win_full, M_loss_full, conditions_from_train, win_is_example_full, margin_pct=0.0)` given the d*-truncated train-greedy condition list, recomputes `lo, hi` as `nanmin/nanmax` over the FULL (train+test) winner pile, asserts `all_winners_pass` and `all_examples_pass` by construction.

**Per-experiment scripts.** Each numbered experiment file targets one knob; the result JSON schema mirrors the script's dict assembly:

- **exp_01_stability_n500.py** — re-runs Run A's per-seed stability check at production `n_null=500`. Loads `("htf", "bf")` at 365-day hold-out. Output records per-seed `p95_loss_curve`, `p99_loss_curve`, `p5_win_curve`, `p1_win_curve`, admissible counts, and `d_star`.
- **exp_02_rolling_holdout.py** — walks an anchor date backward in 90-day strides; at each anchor, hold-out window = `(anchor - 90 days, anchor]`. Per-anchor record: scope, anchor, holdout_start, train/test W/L counts, max_depth, `admit_005/001`, `d_star_005/001`, the first-pick expression index.
- **exp_03_synthetic_null.py** — sanity check. Three constructions: `synth_S1` (pure Gaussian iid for both piles, n_win=200, n_loss=200, n_expr=2000); `synth_S2_S3` (real BF data with relabeled splits via random permutation). Reports `n_admissible / n_depths` per construction; framework "passes" if rate ≤ 2× nominal alpha.
- **exp_04_11_window_and_scope.py** — per-setup picker. For all 7 scopes × 3 windows {180, 365, 540}, builds combined pools tagged with per-row setup labels. Computes per-expression d=1 box on training, applies to test, runs `n_null=500` parallel permutation worker. Per-expression p-value = `max(pval_g1, pval_g2)`. Applies Benjamini-Hochberg FDR at alpha=0.05. For each admitted expression, computes a per-setup breakdown and picks per-setup champion by `max(lift_pp_setup)`.
- **exp_05_shuffle_count.py** — convergence sweep on `n_null ∈ {50, 100, 200, 500, 1000, 2000}`. Verdict picks the `n_null` plateau where std stops shrinking ~`1/sqrt(n_null)`.
- **exp_06_07_search_method.py** — three search methods: `greedy_IS` (current, picks max IS-burn at each step), `greedy_HOLDOUT` (picks max test-burn at each step — the "fair" version), `beam_IS` (beam at width 200/500). BEAM tests skipped — explicitly annotated as computationally infeasible at the 16K-expression scale. Optional `peak_target=K` halt rule stops when no improvement for K consecutive steps.
- **exp_08_inner_sig_gate.py** — A/B on an inner per-expression significance gate during greedy. `greedy_with_inner_gate(..., inner_gate=True)` restricts each step's argmax to expressions where `real_burn > null_p95`. Verdict counts configs where `d_star_005` or `d_star_001` differ.
- **exp_09_nan_handling.py** — two NaN policies: P1 nan-as-outside (current, matches `scan_engine` production); P2 nan-as-inside (lenient).
- **exp_10_alpha.py** — Part 1 real-data sweep over `alphas={0.01, 0.025, 0.05, 0.10}`; Part 2 `synthetic_fpr_at_alpha` reuses the synthetic-null S2 across `n_synthetic_seeds=10` to measure empirical false-positive rate per alpha. Chosen alpha is the smallest where `mean_FPR ≤ nominal + 0.005`.
- **exp_13_margin_sweep.py** — margin sweep `m ∈ {0%, 1%, 2%, 5%, 10%}`. 0% wins.
- **exp_per_expression_fdr.py** — the FDR-correction path. Per scope/window, runs the per-expression d=1 dual gate (vectorised: every expression's training-pile box applied to test) across `n_null=500` permutations. Reports four admit counts per scope/window: naive 0.05/0.01, Bonferroni-corrected (`alpha / n_eligible`), and BH-FDR at 0.05/0.01. Returns top-30 expressions by p-value with `{expr_idx, real_loss_burn, real_win_keep, pval_combined, pval_g1, pval_g2, lo, hi}`. **This is the path that actually finds admissible single-expression rules where the depth-greedy doesn't admit anything.**

**Run orchestrators (the higher-level batteries):**

- `run_all_methods.py` runs methods A–F end-to-end on all three setups.
- `run_combined_refinement.py` pools HTF+BF+BASE into one matrix, runs a 30-depth greedy on real labels, compares to 50 shuffled-label runs at each depth. Picks "first significant depth" as the smallest d with p<0.05 and positive net lift.
- `run_depth_with_null.py` per-setup analog: real burn vs n_null=200 permutation null at every depth, derives stopping depth.
- `run_followup_tests.py` three serial tests reusing greedy + null-distribution helpers. Test 1: two-stage refinement (combined gate at MAX_DEPTH=30 → per-setup stage-2 greedy). Test 2: pair-wise scopes {(htf,bf), (htf,base), (bf,base)}. Test 3: combined gate's conditions-at-each-depth applied per setup at depths {5, 10, 16, 20, 30}.
- `run_htfbf_gate_on_base.py` cross-setup transfer: trains on HTF+BF, applies at depths {3, 5, 8, 10, 15, 20, 25, 30} to BASE's pool. Reports `win_drop`.
- `exp_run_a_stability.py` Run A. Stability of the dual gate at fixed depth across 10 RNG seeds, scope=("htf","bf")/days=365/`n_null=100`/`max_depth=15`.
- `exp_run_b_holdout_sweep.py` Run B. Full bbox sweep: 7 scopes × 6 windows {90, 180, 270, 365, 540, 730}.
- `exp_run_c_method_families.py` Run C. Alternative method families through the dual gate, testing `("maha", top_k=20)`, `("maha", top_k=50)`, `("iforest", top_k=20, n_estimators=100)`, `("rf", top_k=50, n_estimators=200)` over 5 scopes × 3 windows × 4 methods.
- `exp_run_d_exhaustive.py` Run D. **D1** per-expression vectorised dual gate (basis of `exp_per_expression_fdr.py`). **D2** `evaluate_pca_box`: NaN-fills the top-200 features by individual loss-burn, fits PCA on training winners with `n_components ∈ {5, 10, 20}`, runs an axis-aligned box in PC space, repeats with shuffled training under refit-PCA. **D3** `evaluate_subspace_ensemble`: trains 50 separate WIN bboxes on disjoint random samples of K=20 eligible features each; signal must pass `vote_pct ∈ {1.0, 0.9, 0.5}` of the 50 sub-boxes.
- `exp_consensus_stability.py` per-condition consensus stability under loser-row subsampling. For each setup, runs `N_RUNS=20` beam_search calls on 50% subsamples of `M_loss` (winner pile fixed). Counts how often each `(expr_idx, lo, hi)` condition appears in the locked set across runs. Repeats with `N_NULL_LABELINGS=5` shuffled-label pools to baseline.
- `exp_differential_carve.py` per-expression carve gap analysis: real `LOSS_outside_rate − SAC_outside_rate` vs the same gap under shuffled labels. n_perm=200. Reports `null_max_gap_p50/p95/p99/max` (FWER null), per-condition empirical p-values.
- `exp_margin_depth_sweep.py` margin × depth grid {0%,2%,5%,10%,20%,50%} × {3,5,8,10,15,20} on a time-split p_train=0.7. Reports `ho_win_retention` and `ho_loss_carve_rate` as a pivot table.
- `exp_multi_expression_stack.py` tests AND-conjunctions of the FDR-admitted expression sets. Progressively conjoins top-1, top-2, ..., top-N. For each conjunction: builds full-pile shipping bounds, runs a dedicated dual-gate-on-conjunction null at n_null=500 to verify the multi-expression rule itself admits at alpha=0.01.
- `exp_permutation_null.py` the first/oldest permutation null (population level). Real beam_search vs `n_perm=200` shuffled-label beam_search runs. Reports the percentile of real max-carve in the null distribution.
- `exp_walk_forward.py` time-split sweep `p_train ∈ {0.5, 0.6, 0.7, 0.8}`. Reports per-split: `in_sample_loss_carve_rate`, `hold_out_loss_carve_rate`, `hold_out_win_drop`. Headline at p=0.5 on bf: 47/76 hold-out winners dropped (62%).
- `verify_picked_rules.py` verification step for the three named picks (`htf` → expr 3829, `bf` → expr 423, `base` → expr 9882 trained on bf+base scope).
- `compile_findings.py` pure aggregator. Reads each result JSON and prints a parameter table (n_null=500, alpha=0.01, margin=0.0, NaN-as-outside, inner-gate-OFF, hold-out=360 days) plus a description of the resulting algorithm shape.

**`method_families.py`.** Consumed by `exp_run_c_method_families.py`. Each method exposes `build(method, M_win_train, M_loss_train, **kwargs) -> rule_dict` and `apply(rule, M) -> bool_mask`. Hard constraint enforced inside every `build`: `assert pass_w.all()` on training winners.

- `build_bbox` — greedy axis-aligned bounding box. Same mechanic as `dual_gate_runner.greedy_with_holdout`.
- `build_mahalanobis` — diagonal-covariance Mahalanobis. First-pass selects `top_k=20` (default) expressions by individual loss-burn under the win box. Computes `win_mean`, `win_std` (`std=ddof=0`, floor `1e-9`). Threshold = `max(z·z over training winners) * (1 + 1e-9) + 1e-9` so all winners pass with float-precision slack. NaN row → outside.
- `build_iforest` — `sklearn.IsolationForest(n_estimators=100, contamination="auto", random_state=42)` on training winners restricted to top_k=20 features. Threshold = `min(score_samples(X_w))`. NaN at apply time replaced with 0.
- `build_rf` — `RandomForestClassifier(n_estimators=200, class_weight="balanced", random_state=42)` on top_k=50 features. Threshold = `min(predict_proba(X_w)[:,1])`.

**Result JSON schemas.** Every result file mirrors its script's output dict. Sample patterns: `{setup}_perm_null.json` has `setup, n_win, n_loss, n_expr, valid_expressions_real, real_carve, real_carve_rate, real_depth, real_history, n_perm, null_carve_mean/std/median/p50/p90/p95/p99/max, real_percentile_in_null, p_value_real_above_null`. `methods_test_{setup}.json` has top-level summary + `results: {A|B|C|D|E|F:{method, n_conditions, loss_burn:{burned, total, rate}, win_check:{kept, total, rate}, collateral:{REDUNDANT|NO_ENTRY|presignal_only|pyramid_only:{burned, total, rate}}, conditions_first10:[[expr_idx, lo, hi], ...]}}`. `exp_per_expression_fdr.json` is a flat list, one record per (scope, days), with `top_30_by_pval` sorted ascending.

### Era 4 detailed catalogue — Presignal grinder §3 failure ledger, expanded

`PRESIGNAL_GRINDER.md` §3 is the most concentrated single record of what the team tried before the avatar pivot. Each sub-section was an independent attempt at producing a per-setup pre-classifier pool filter that admits clusters whose pre-signal feature profile lies inside an example-defined bounding region with 100% example pass rate.

**§3.1 — Cache-basis strict-AND.** The naïve approach: build a bounding box on each of the ~16,000 expressions in the cache from the example values for that expression, then admit any universe bar that falls inside every box. Compound-probability collapse: at scale, every individual feature's ~50% admit rate compounded across thousands of features admits zero universe bars. The numerical observation: zero wild hits at full universe size. The mechanism's failure mode is structural — strict-AND on independent bounded features cannot produce a non-trivial admission set at high feature count. The spec explicitly retired this approach as the foundational warning that subsequent §3.x variants tried to work around.

**§3.2 — Two-stage composition (4-axis OHLC outer + 8k inner).** Attempted to break the compound-probability collapse by structuring the bound as a coarse outer filter (4-axis OHLC envelope) plus a fine inner filter (8k features inside the outer). Same result: zero wild hits. Stage 1 was effectively pass-through (the OHLC envelope at example bounds is too loose to filter), and Stage 2 inherited the §3.1 compound-probability problem on its 8k inner features. The two-stage decomposition did not change the underlying mathematical regime.

**§3.3 — F1 hull + 5-descriptor Location (the geometric bounding-box approach, §15).** Reformulated the problem as a low-dimensional convex hull on a small set of geometric descriptors. Calibration spread across setups was 167× versus a ≤10× target — i.e. parameters that produced reasonable admission on one setup admitted 167× too many or too few bars on another. Diagnostic finding: 4 of the 6 Location descriptors were near-pass-through (85–100% admit on each individually), so the effective filter was reducible to F1+D1+D4b. The volume of admitted bars tracked F1 hull-area variance rather than carrying any structural signal. The descriptor set was carrying noise, not information.

**§3.4 — MA-corridor cells with reject-rate kneedle (§16).** Discretised the example region into MA-corridor cells (combinations of multi-MA stack states); admit any candidate whose cell membership matches an example cell. Selected 287–831 correlated cells per setup at the kneedle of the reject-rate curve. Effective rank 2–7 across those cells — pathological cell redundancy, the cells were not actually independent dimensions of the example region. ~500× too loose at projection (i.e. the operational admission was 500× the target).

**§3.5 — σ-cloud union bands with LOO walk-up (rejected 2026-04-21).** Replaced strict-AND with a per-feature σ-cloud (mean ± k·σ over examples, walk-up k until LOO admits all examples). Failed because σ blew out from one volatile example (AXTI on BF) — the standard deviation was driven by a single outlier so widely that the band became uselessly wide for most of the feature dimensions. The LOO-drop ordering inverted relative to spec: removing AXTI tightened the band more than removing other examples should have. Single-example-band-edge sensitivity, the same shape that would later break the L14 labeler with OSCR.

**§3.6 — Carve-greedy weekly chain (2026-04-27).** Tried a fundamentally different structure: build a sequential chain of weekly anchor-pinned conditions instead of independent bounded features. Structurally capped at length 4 on BF — the chain mechanism could not extend further because the anchor-pinning was incompatible with breakout setups whose terminal events fire at the anchor itself. The mechanism worked but the achievable carve was bounded by the chain length, which was bounded by the setup's anchor mechanics.

**§3.7 — Expression-trajectory sign-coherence (2026-04-27).** Used 100%-agreement gates on expression sign trajectories across examples. Walk-forward false-rejection rate was far above target despite the 100%-agreement gate appearing to be a strong filter. The explanation: the high-dimensional cell pool overfitted regardless of the ensemble approach used — agreement on 100% of examples in-sample does not generalise when the ensemble is built from a high-dimensional pool, because some subset of dimensions will agree by chance.

**§3.8 — Pyramid as operational closer (2026-04-27, formally disqualified).** The pyramid grinder's signal output had been the assumed source of truth through Eras 1–3. The walk-forward test admitted 0 of 15 BF held-outs. Forward false-rejection rate = 100%. The disqualification was clean — the pyramid was retired as a forward-facing closer.

**§4 — Bbox-on-WIN (the basic L14 refinement primitive).** The core mechanism of the L14 refinement grinder: build per-feature bounds from the labelled WIN pile, admit any signal that passes the bounds. Walk-forward false-rejection rate 73–90% across setups. Point memorisation; no forward generalisation.

**§7 (pending).** The pyramid example-subsample consensus pipeline as the proposed closer for the §6 stack — untested at the audit's writing. The §6 stack itself (sign-coherence MA cells + carve-greedy chain stack, locked 2026-04-27) is the current best presignal model for BF; §4 bbox direction was superseded for forward deployment.

The §3 ledger reads as a serial enumeration of mathematical regimes the team explored to escape the strict-AND-on-many-features compound-probability trap. Each successive variant either inherited the same trap (§3.1, §3.2), failed for a different structural reason (§3.3 noise descriptors, §3.4 cell redundancy, §3.5 single-example sensitivity, §3.6 anchor-pinning, §3.7 high-dim agreement), or escaped with too-loose-to-be-operational admission (§3.4 at 500× target). No variant in §3 reached the operational target; §6 (current best) is forward-stable but admits ~14.5% of the universe — too loose to deploy alone.

### Era 5 detailed catalogue — Avatar §4 bug ledger

`Avatar_Correlator.md` §4 catalogued five known bugs as the spec was being iterated. Each is a structural critique of the avatar correlator's metric or combine layer.

**§4.1 — DTSS metric ranks magnitude-proximity over shape correctness.** Failed-retrace candidates (where the chart's phase 2 retrace proportion is wrong) score similarly to correct-retrace candidates because the uptrend-magnitude proximity drives the distance more than the shape-correctness term. The metric mistakes magnitude similarity for structural similarity. Implication: candidates that pass the metric may not actually have the setup's defining structural feature; they merely have similar magnitude per phase.

**§4.2 — §3.10 fade filter leak when phase-2 length > exclude_last.** Argmax detection lands inside the retrace phase rather than at the boundary, causing the filter to misclassify the phase boundary. Edge case: when the retrace phase is long, the argmax of the candidate window can fall inside the retrace itself, breaking the boundary detection.

**§4.3 — Single-anchor metric can't represent multi-flavor setups.** A bank example like a 3× BASE has different per-phase magnitudes than a 12× BASE. The single-anchor metric (mean + std template across the bank) treats both as the same target, but their per-phase distances are not comparable. The implication: setups that span an order of magnitude in scale cannot be represented by a single mean-template; the std grows so wide that the z-distance loses discriminative power. This is the BASE flat-z collapse cause.

**§4.4 — Distance has no intrinsic interpretation.** "Best of strong field" and "best of mediocre field" produce the same numeric distance under the current metric. The metric does not encode whether the field of candidates surrounding a high-quality match is itself diverse or homogeneous — both produce the same #1 rank. A rank-1 candidate could be a tight match in a sparse region or a coincidental match in a dense region; the metric cannot tell.

**§4.5 — Multi-anchor combine mishandles consistent matches.** Three structural issues compound: (a) per-anchor z-score is apples-to-oranges across anchors with different distance distributions (QUBT's distribution has heavy right tail; QUBT-anchored candidates dominate the multi-anchor combine even when QUBT itself is fine); (b) min-z dedup favours single-anchor outliers (a candidate that scores extremely well from one anchor and mediocre from all others wins over a candidate that scores well from all anchors); (c) dedup discards information (the relative consistency across anchors, which is the strongest signal of structural similarity, is collapsed to a single number). The 2026-05-05 finding (`project_avatar_combine_layer_broken` memory) escalated this from "bug ledger entry" to "primary ranking failure mode."

### Era 5 detailed catalogue — Avatar §7 in-progress findings (2026-05-04 → 2026-05-08)

The spec records seventeen numbered §7 sub-sections as the avatar correlator iterated through 2026-05-04 onward. The most consequential:

**§7.7 — Phase-anchored aggregates fix.** Identified as a candidate fix for the §4.4 "distance has no intrinsic interpretation" problem. The hypothesis: anchor each phase's per-phase aggregates to that phase's local context (entry log-return, max log-return within phase, etc.) rather than to a global window-level baseline. Untested at code commit.

**§7.12 — Bank curation for slow-accumulator HTF.** Identified the structural impossibility of REMX-style stocks surfacing at any ADR value because their biggest 1-bar move (1.95 ADR) is below all banks' 4–7 ADR ignites. The metric is doing the right thing; the bank lacks a slow-accumulator example. Pending: add a bank entry for that flavor of HTF.

**§7.16 — BASE phase taxonomy too loose (2026-05-04).** Full-depth diagnostic across 4 setups: HTF and BF show meaningful z-spread and surface curated examples at competitive ranks (MOD #2,381 z=-1.86; AXON #3,153 z=-2.50). DTSS in between. **BASE alone has flat-z collapse** (z range -0.030 to ~0) and buries curated examples at rank 26,284+. Only 4 of 38 curated BASE examples appeared in saved historical scan output. Original interpretation: BASE phase taxonomy was too loose — BASE's defining feature is the MA stack tightness at asof rather than directional phase movement, and the generic phase signature didn't capture that. Project memory `project_avatar_base_ma_tightness` records the conclusion: "Distinctive BASE signature is how tightly bound the MAs are at asof. Don't port HTF rules wholesale." A separate hypothesis (project memory `project_avatar_base_equals_bf_weekly`, 2026-05-04) framed BASE as "structurally a BF viewed on weeklies" — suggesting weekly-tightening-range as a candidate signature dimension.

**§7.17 — Combine layer identified as primary ranking failure mode (2026-05-05).** The diagnostic that closed the BASE plateau analysis. Single-day per-phase scoring puts curated examples at the top 0.8% to 16.6% of the universe — i.e. per-phase scoring HAS signal. None reach top 30 even on clean single-day scan. **Multi-anchor combine (per-anchor z-score → min-z dedup) buries curated examples deeper.** The fix proposed in §6.7: mean rank percentile combine. Convert each anchor's distance to a within-anchor percentile (unit-free, comparable across anchors); average across anchors (punishes candidates that don't rank well from at least one anchor). Proposed but not validated end-to-end at the audit's close.

The reframe between §7.16 and §7.17 is important: on 2026-05-04 the team thought BASE phase taxonomy was the problem (BASE-specific). On 2026-05-05 the diagnosis sharpened — the per-phase scoring has signal across all setups; the combine layer is the cross-cutting failure mode, and BASE just exposes it most clearly because its z-spread is flattest. The earlier `project_avatar_metric_ranking_failure` memory was explicitly marked SUPERSEDED.

### Era 5 detailed catalogue — Avatar BASE plateau (2026-05-06 → 2026-05-08)

The bank-curator worktree's `research/continuation_prompt.md` records a 2-day plateau session on the BASE setup with the same constraint set. Best-so-far code state preserved at `avatar_probe_from_db.py.bak_iter4_kept`: 6-dim per-phase signature with DP-fit boundaries, `min(rp_phase, rp_asof)` per-anchor combine, `rp_asof` as 7-dim ADR-relative MA-state distance at asof, mean-rank-percentile across-anchor combine, multi-bank, per-setup post-filter ON. Result: BASE 5/27, BF 5/33, DTSS 6/50, HTF 3/18.

Round 1 (BASE focus, multi-bank):

| iter | change | BASE | BF | DTSS | HTF | kept? |
|------|--------|------|----|----|----|----|
| 0 | baseline (mean rp, no asof) | 1 | 5 | 4 | 1 | — |
| 1 | + rp_asof, mean(rp_phase, rp_asof) | 3 | 4 | 7 | 2 | revert (BF -1) |
| 2 | cosine on anchored-log full window | 1 | 3 | 3 | 3 | revert (coverage collapse) |
| 3 | rp_asof only | 4 | 2 | 6 | 3 | revert (BF -3) |
| 4 | min(rp_phase, rp_asof) | **5** | **5** | **6** | **3** | **KEPT** |
| 5 | per-dim z-score in dist_asof | 4 | 4 | 7 | 4 | revert |
| 6 | across-anchor median | 4 | 5 | 6 | 6 | revert (BASE -1) |
| 7 | drop-max trim across-anchor | 5 | 4 | 6 | 6 | revert (BF -1) |
| 8 | close_in_stack pre-filter -1 ADR | 5 | 5 | 6 | 4 | revert (BASE held only) |
| 9 | Pearson on log-close full window | 4 | 6 | 2 | 2 | revert (DTSS -4) |

Round 2 (phase recognition focus):

| iter | change | BASE | BF | DTSS | HTF | kept? |
|------|--------|------|----|----|----|----|
| 10 | adr_bar (bar-by-bar phase-anchored ADR) | 4 | 2 | 3 | 1 | revert (coverage collapse) |
| 11 | per-phase cosine on anchored-log | 4 | 2 | 3 | 1 | revert (coverage collapse) |
| 12 | phase-length-ratio constraint 0.20 | 4 | 1 | 5 | 5 | revert (BF -4) |
| 13 | phase-type directional validity hard reject | 4 | 5 | 6 | 3 | revert (BASE -1) |
| 14 | validity binary penalty 0.05/fail | 4 | 5 | 6 | 5 | revert (BASE -1) |
| 15 | validity continuous penalty | killed early | | | | (Dan flagged) |
| 16 | removed BASE PER_SETUP_CONSTRAINTS | 5 | 5 | 6 | 3 | revert (only +1 in_output) |
| 17 | COMBINED_TOP_N 2000 → 50000 (diagnostic) | 5 | 5 | 6 | 3 | DIAGNOSTIC: cap not bottleneck |
| 18 | BASE post-filter disabled (diagnostic) | 4 | 5 | 6 | 3 | DIAGNOSTIC: in_output 14 → 23 (post-filter rejects 9 curated) |

Round 3 (correlative methods, fast iterations):

| iter | change | mode | BASE | wall | notes |
|------|--------|------|------|------|-------|
| 19 | Pearson on (close-EMA21)/ADR full window | single | 3 | 36m | bug: stale cache, coverage collapse |
| 20 | per-phase length-normalized Pearson log-close | single | 4 | 16m | +1 over single-bank baseline (3) |
| 21 | iter4 method baseline | single | 3 | 16m | single-bank floor |
| 22 | multivariate per-phase Pearson (log-close, log-vol, range/ADR) | single | 3 | 21m | added series didn't help |
| 23 | per-phase Pearson on log-returns | single | 4 | 16m | same as iter20 |
| 25 | 15-d Mahalanobis at asof | single | 3 | 19m | universal at-asof feature vector |
| 26 | 15-d Mahalanobis multi-bank | multi | **5** | 21m | matches iter4 ceiling |
| 27 | bank-averaged template + Pearson | multi | 3 | 17m | template loses signal |
| 28 | per-phase Pearson log-close BF+DTSS multi-bank, --universe-cap 2000 | multi | — | — | running at session end |

Plateau analysis recorded verbatim — two stacked structural causes:

**1. Pre-scoring cliff (drops 12–13 of 27 BASE before scoring).** Post-filter rules R1, R5, R7, R8 of `base_check_candidate` reject curated examples. Plus eligibility/DP-INVALID rejects ~3–4. iter18 confirmed by disabling post-filter: `in_output` went from 14 → 23 (+9 freed). Specific rules firing on curated examples (from iter4 stdout):

- R1 (close > shallowest H- line): SIMO, IONQ-WS, RGTI, SNDK
- R8 (close vs SMA330 < 1.0 ADR): DRN, PTON
- R5 (close vs SMA50 > 3.0 ADR): IREN
- R7 (close vs W-EMA21 > 4.0 ADR): FIX
- R2 (close vs W-SMA50 < -1.0 ADR): QXO

**2. Scoring noise ceiling on the 14 that DO reach `in_output`.** Only ~5 reach top 30 of ~3000 post-filter survivors. Curated examples ranked at the ~95th percentile of post-filter survivors but lost to ~25 noise candidates per scan.

Plateau ratio = `in_output / 3` to `in_output / 2`. To break through, the doc states: "BOTH — free curated from post-filter (currently locked) + find metric strong enough to put basically every in-pool curated in top 30 of ~3000 post-filter survivors. None of the ~25 correlative variants tested comes close."

Untried high-EV directions listed in the continuation prompt:

- DTW per-phase or full-window (handles small alignment offsets)
- knn density across all curated examples (k=1, LOO self-match exclusion)
- Pivot-based structural pattern detection (algorithmic chart pattern detection)
- Volume-prominent scoring (volume as primary, not auxiliary signal)
- Multi-timeframe alignment (weekly + daily concatenated correlation)
- Wavelet decomposition + DTW
- Compression-based dissimilarity (NCD)

Per-iteration timings (single-bank --setup base, ~16 min wall clock; multi-bank --setup base ~17–21 min; multi-bank all-setups ~40 min; with `--universe-cap 2000` expected ~5× faster, untested fully).

The pivot context recorded at session end: Dan asked late "what if we pivot to BF and DTSS" — the suspicion that BASE may not be clearly enough defined. iter28 was the first BF+DTSS test of per-phase Pearson on log-close. iter4 baseline on BF/DTSS: BF 5/33 (15%), DTSS 6/50 (12%). Same plateau ratio as BASE (5/27 = 18%). Setup-pivot alone didn't break the plateau, but the cleaner phase definitions on BF/DTSS were hypothesised to give new methods more lift.

### Era 5 detailed catalogue — Avatar 33-iteration timeline (the full Round 1/2/3 record from `iter_results/run_log.txt`)

The bank-curator worktree's `research/iter_results/run_log.txt` is 1,925 lines and records every iteration of the autonomous metric search. Round 1 ran iters 0–9 (8h budget). Round 2 ran iters 10–17 with iter15 killed mid-run. Round 3 ran iters 18–33 broken into a phase signature plateau (18–28), a post-filter-disabled run (iter29), and a knn-over-curated batch (iter30–33). Best result of all rounds = iter4 (BASE 5/27, BF 5/33, DTSS 6/50, HTF 3/18). The full per-iteration record:

| iter | hypothesis | code change | BASE | BF | DTSS | HTF | decision |
|---|---|---|---|---|---|---|---|
| iter1 (R1) | adding asof MA-state to mean rank percentile combine helps BASE? | rp_anchor = mean(rp_phase, rp_asof). Added 7-dim ADR-relative MA-state vector at signal bar (D1 SMA200/50/EMA8/21, weekly EMA8/21/SMA50). Wall: 43.1m | 3/27 | 4/33 | 7/50 | 2/18 | revert: BF -1 (AXON 5→96) |
| iter0 | pre-iter1 baseline measurement | restored `bak_iter1_start` (mean rank percentile only, no rp_asof). Wall: 31.6m | 1/27 | 5/33 | 4/50 | 1/18 | baseline. best-so-far file |
| iter2 | per-phase 6-dim signature wrong primitive? does cosine on anchored-log curves work? | `score_mode="cosine"`. v[i]=log(close[start+i])−log(close[start]), unit-normalised. fit_boundaries fallback to bank's. Wall: 32.4m | 1/27 | 3/33 | 3/50 | 3/18 | revert: BF -2, DTSS -1. Coverage collapsed (in_output BF 14→8, DTSS 8→3, HTF 8→4) but matches were tight (DTSS mean 5.7, HTF 27.0) |
| iter3 | curve shape contributing once asof MA-state is primary signal? | per_anchor_score = rp_asof only (drop rp_phase). Wall: 45.5m | 4/27 | 2/33 | 6/50 | 3/18 | revert: BF -3 (AXON 5→175, HUT 2→missing, BE 24→missing). BASE wins were dramatic (FIX 143→2, SKY 160→10) |
| iter4 | OR-gate combine: each candidate scored by stronger feature against each anchor | per_anchor_score = min(rp_phase, rp_asof). Wall: 42.5m | **5/27** | **5/33** | **6/50** | **3/18** | **KEPT**. New best-so-far. BASE +4 (PAG 60→15, SKY 160→16, FIX 143→4, EAT 87→16, EOSE 131→28); DTSS +2 (WING 44→4, LABU 36→11); HTF +2 (RHP, MOD) |
| iter5 | MA-state Euclidean dominated by high-variance dims? | dist_asof: per-dim z-score using anchor's candidate distribution. Wall: 41.5m | 4/27 | 4/33 | 7/50 | 4/18 | revert: BASE -1, BF -1. Mean ranks improved across all 4 (BASE 171.6→122.5) but cutoff hits lost (EAT 16→46, EOSE 28→115) |
| iter6 | mean-across-anchors right combine, or do bad outlier anchors pollute? | across-anchor combine: median instead of mean. Wall: 41.4m | 4/27 | 5/33 | 6/50 | 6/18 | revert: BASE -1 (EOSE 28→35). HTF +3 (OSCR, FTDR, LMND); but median dropped good outlier anchor that helped EOSE |
| iter7 | asymmetric: drop only worst per-anchor score, mean rest | drop-max trim across-anchor. Wall: 41.5m | 5/27 | 4/33 | 6/50 | 6/18 | revert: BF -1 (BE 17→31). Drop-max universally lowers all scores |
| iter8 | close_in_stack universal pre-filter: drop candidates >1 ADR below any MA at asof | pre-filter loop in run_one_setup combine. Wall: 39.2m | 5/27 | 5/33 | 6/50 | 4/18 | revert: BASE held but didn't STRICTLY advance. Strict rule = held ≠ exceeded. KTOS 426→324, ATI 532→416 surfaced but not enough |
| iter9 | Pearson on log-close (mean-centered, unit-normed). Last in 8h budget | `score_mode="pearson"` added to avatar_phase_probe.py via parallel_pearson_score. Wall: 41.7m | 4/27 | 6/33 | 2/50 | 2/18 | revert: BASE -1, DTSS -4, HTF -1. Mean-centering removes slope info DTSS/HTF need |
| iter10 (R2) | Q1: bar-by-bar phase-anchored ADR curve tighter than 6-dim summary | `score_mode="adr_bar"` via parallel_phase_l2. Wall: 61.2m | 4/27 | 2/33 | 3/50 | 1/18 | revert (multiple violations). Tight when matched (HTF mean rank 4.0!) but in_output collapsed BF 12→6, DTSS 8→3, HTF 8→1 |
| iter11 | Q3: per-phase cosine — cosine within each phase, mean across phases | `score_mode="per_phase_cosine"` via parallel_per_phase_cosine. Wall: 60.3m | 4/27 | 2/33 | 3/50 | 1/18 | revert. Same coverage collapse as iter10/iter2 |
| iter12 | Q5: phase-length-ratio constraint at 0.20 absolute deviation. Trim "structural pretenders" | DP fitted phase proportions must be within 0.20 of bank's. Wall: 52.5m | 4/27 | 1/33 | 5/50 | 5/18 | revert: BF -4. STRL 204→51, ATI 532→387, AXTI 451→255 surfaced; but threshold too tight for BF/DTSS curated |
| iter13 | Q5b: phase-type DIRECTIONAL validity (uptrend rises >0.3 ADR, pullback falls < -0.3 ADR, range <2.0 ADR) | _phase_directional_valid hard reject. Wall: 49.8m | 4/27 | 5/33 | 6/50 | 3/18 | revert: BASE -1 narrow (only EOSE 28→87 lost) |
| iter14 | soft penalty: 0.05 per failed phase (not hard reject) | per_anchor_score += 0.05*n_failures. Wall: 57.6m | 4/27 | 5/33 | 6/50 | 5/18 | revert: BASE -1 (EAT 16→64). EOSE recovered; HTF +2 |
| iter15 | continuous penalty: 0.05*total_violation (smooth, not binary) | _phase_directional_violation returns float | killed | | | | KILLED MID-RUN. Dan flagged: "something fundamental missing, should land 75%+." 13 BASE never reach top 2000 |
| iter16 | diagnostic: PER_SETUP_CONSTRAINTS["base"] excluding ATH-breakout BASE? | base constraint changed [("uptrend","ge_asof")] → []. Wall: 55.0m | 5/27 | 5/33 | 6/50 | 3/18 | revert. in_output 14→15 (only +1!). Hypothesis WRONG — constraint not the cut |
| iter17 | diagnostic: raise COMBINED_TOP_N_LOCAL 2000 → 50000 | iter_measure_ranks.py only. Wall: 56.5m | 5/27 | 5/33 | 6/50 | 3/18 | IDENTICAL to iter4. Missing curated NOT capped — they're filtered upstream (post-filter) |
| iter18 (R3) | diagnostic: post-filter rejecting BASE curated? | base check_fn = None temporarily. Wall: 31.3m | 4/27 (in_output **23/27!**) | 5/33 | 6/50 | 3/18 | CONFIRMED: post-filter rejects 9 of 13 missing BASE (R1 H-line, R5/R7 MA ceilings, R8 SMA330 floor — fired on SIMO, IONQ-WS, RGTI, SNDK, DRN, PTON, IREN, FIX, QXO) |
| iter19 | Pearson on (close-EMA21)/ADR(30) series, MA-convergence path | _series_pearson_dist. Wall: 36.6m | 3/27 (in_output 5/27) | 1/33 | 0/50 | 2/18 | revert: catastrophic. Bug: _MA_DIST_SERIES_CACHE keyed on ticker only, stale cached series |
| iter20 | per-phase length-normalized Pearson on log-close (variable phase durations via DP fit + resample to L=50) | _phase_resampled_pearson_dist. Killed for slowness; speedup added (--setup, --single-bank flags) | killed | | | | iteration loop too slow. Added SINGLE_BANK_MODE and --setup CLI args |
| iter21–28 (BASE-only, single-bank dev mode) | various phase recognition + multivariate variants — log details collapsed in run_log | 15-dim universal at-asof feature vector added (`_compute_asof_features_15d`); _phase_multivar_pearson_dist; bank features 15d slot; etc. | wall 945–1250s each | | | | All within iter4 family, no advance |
| iter29 (KILLED → re-purposed) | Dan: iter4 family exhausted. Need fundamentally NEW measurement | scope change: BASE descoped, only BF/DTSS/HTF | | | | | iter29 kept: post-filters OFF for bf/dtss/htf, full 10k universe, multi-bank |
| iter30 (R3 wave) | knn-over-curated: pool = bank + non-bank fixture, score = min Pearson distance to nearest curated, LOO | `KNN_MODE="pearson"`, KNN_LOOKBACK_BARS=100, KNN_RESAMPLE_L=50. Wall: 7.4m | descoped | 3/33 (29/33 in_output, mean 501.9) | 1/50 (46/50, 655.7) | 0/18 (16/18, 853.7) | underperforms iter4 |
| iter31 | knn DTW (Sakoe-Chiba band 10, numba-jit) | KNN_MODE="dtw". Wall: 7.2m | | 5/33 (29/33, 347.8) | 1/50 (46/50, 559.3) | 0/18 (16/18, 776.8) | best of the 4 — only TIES iter4 BF |
| iter32 | knn NCD (zlib compression on z-bucketed log-return symbols) | KNN_MODE="ncd", n_bins=8. Wall: 13.3m | | 4/33 (24/33, 781.1) | 0/50 (37/50, 1047.6) | 2/18 (13/18, 821.2) | underperforms |
| iter33 | knn Pivot Levenshtein (ZigZag pivots, dir+magnitude bins) | KNN_MODE="pivot", min_swing_pct=0.03. Wall: 14.6m | | 4/33 (28/33, 1024.0) | 0/50 (32/50, 1063.2) | 0/18 (13/18, 867.8) | underperforms |

End of all rounds. iter4 remains best. Conclusion logged: "knn-over-curated with min-distance is the wrong paradigm. The curated set is heterogeneous in shape space; min over a heterogeneous pool gets dominated by chance similarities."

### Era 5 detailed catalogue — Bank discovery probes (HTF and DTSS)

Each bank-discovery probe loads bank entries from `data/scanperfect.db` table `bank_entries`, replays phase boundaries, and tests whether a particular structural relation holds across all curated banks. Findings became candidate filter logic for the post-filter (or got rejected). Files in `swing-screener-bank-curator/research/`:

**HTF probes:**

- `bank_lookback_probe.py` — Tests Rule 3 (any close in flag phase above ignite_high) and Rule 4 (count of consecutive bars before ignite phase whose high stays below ignite_high). Output: per-bank lookback_bars + breaker_date. Used to set HTF lookback ceiling.
- `bank_chop_zone_probe.py` — Tests if at the close of last ignite bar, ext_avgc50_adr14 is above the chop zone. Reads main repo's expression cache. Used to validate "extension is past chop" structural condition.
- `bank_sma200_probe.py` — Tests Rule 4 sanity (walking back 30 bars from ig_first − 1, count any close ≥ ignite_high; should be 0, CRCL exempt as IPO) AND Rule 5 (close[last_ig] > SMA50, banks <50 bars NaN). Both became HTF post-filter rules.
- `bank_weekly_ema_probe.py` — Tests if daily close[last_ignite] > weekly EMA(8) AND > weekly EMA(21). Method: resample daily through last_ignite to W-FRI, compute EMAs. Became weekly-MA post-filter.
- `bank_flag_dip_below_avwap.py` — For each HTF bank, finds bar in ignite phase whose AVWAP-anchored-from-that-bar yields HIGHEST AVWAP at asof. Tracks that AVWAP through flag phase, finds flag bar with lowest low. Reports avwap-low-at-bar in ADR + max excursion. Establishes ceiling for flag dip below the argmax-anchored AVWAP.
- `bank_flag_dip_below_ignite_high.py` — Replaces AVWAP version with ignite_high reference. flag_dip_adr = (ignite_high − min_low_flag) / ADR(30). tightness_ratio = flag_dip_adr / ignite_move_adr.
- `bank_flag_tightness_pct.py` — Same tightness ratio in % terms (no ADR): flag_pullback_pct / ignite_move_pct.
- `bank_ignite_open_close_adr.py` — Per HTF bank computes (a) ignite open-to-close move in ADR(30) and (b) argmax-AVWAP distance from asof close. Diagnostic for ignite size + AVWAP geometry.

**DTSS probes (7 files):**

- `bank_dtss_pullback_probe.py` — Pullback magnitude from leftside_peak (max high in phase 0) to bowl_bottom (low at phase 2 first bar) in ADR(30). Reports per-bank values + smallest pullback as the bank floor.
- `bank_dtss_leftside_rightside_probe.py` — Tests Rule #1: max(high in rightside, the last phase) ≤ max(high in leftside, phase 0). Validates phase taxonomy + rule structurally.
- `bank_dtss_pullback_start_peak_probe.py` — Verifies high at pullback-start bar (= first bar of phase 1) equals max(high) over leftside (phases 0+1). Sanity check on phase boundary semantics.
- `bank_dtss_bowl_depth_probe.py` — Bowl depth: high at pullback-start to low at retrace-start in ADR(30).
- `bank_dtss_signal_bar_probe.py` — Signal bar (asof = E-1) close measured in ADR(30) from leftside_peak (d1, neg = below peak) and from bowl_bottom (d2, pos = above bowl). Used to find threshold for when DTSS signal fires — close should sit somewhere in retrace, not too close to bowl (too early) or above peak (too late).
- `bank_dtss_sma_probe.py` — close[asof] vs D1 SMA(N) for various N (CLI arg, default 200). Tests which SMA period structurally separates banks.
- `bank_dtss_weekly_probe.py` — close[asof] vs weekly EMA(8), EMA(21), SMA(50). W-FRI resample with partial trailing week's close = daily close[asof].

### Era 5 detailed catalogue — DTSS filter pipeline (the only fully working setup)

Pipeline files in `swing-screener-bank-curator/research/`: `dtss_filter_discovery.py` → `dtss_filter_overfit_guard.py` → `dtss_filter_rank_test.py` → `dtss_filter_greedy_stack.py` → `dtss_stack_pareto.py` → `dtss_stack_nonbank_validate.py`. Each writes JSON to `research/avatar_viability_bank/`.

**Stage 1 — discovery** (`dtss_filter_discovery.py`, 73 candidate invariants found). Enumerates structural invariants on D1/Weekly/Monthly SMAs (10/20/50/100/200) and EMAs (8/21). Categories: (1) MA floors per phase: min(close−MA) > 0 in phase k for all 10 banks; (2) MA ceilings per phase: max(close−MA) < 0 in phase k for all 10 banks; (3) MA-bar floor at boundary: close at boundary > MA at boundary; (4) Geometric invariants: phase length / value orderings; (5) Volume orderings: phase volume relations. Filter qualifies only if 0 violations across all 10 bank entries.

**Stage 2 — overfit guard** (`dtss_filter_overfit_guard.py`, 73 → 136 survivors at threshold 0.80). Each candidate evaluated on 50 non-bank curated DTSS examples across a 5-day formation window (chart_date − 4 to chart_date). Operational pass = passes on ANY of the 5 days. Threshold: drop candidates where non-bank operational pass rate < 80%. Sample survivors with non-bank pass rates: "close > D1_SMA200 for every bar in phase 1": 0 violations, nb pass 0.84; "close > D1_SMA200 for every bar in phase 2": 0 violations, nb pass 0.86; "close > W_SMA50 for every bar in phase 1": 0 violations, nb pass 0.90.

**Stage 3 — greedy stack** (`dtss_filter_greedy_stack.py`). Greedy algorithm. Start empty. At each step, simulate adding each candidate; pick the one that gives biggest improvement in sum-of-best-ranks (with stop conditions: improvement ≤ 0 OR adds drop any bank entry entirely). The final 15-step stack:

| Step | Filter added | Bank-sum | Top-30 |
|---|---|---|---|
| 0 baseline | (per-entry: SMCI=960, AAOI=89, CELH=27, ZIM=15, TRNS=132, KYMR=223, DNLI=376, ACHR=16, PSIX=397, PACB=524) | 2759 | 3 |
| 1 | "close > M_SMA10 for every bar in phase 2" | 1412 | 3 |
| 2 | "phase 0 max high ≥ phase 2 max high" | 511 | 5 |
| 3 | "close ≥ open at boundary 3 (green/doji)" | 296 | 7 |
| 4 | "phase 1 min low ≤ phase 2 min low" | 181 | 8 |
| 5 | "close at boundary 0 ≤ close at boundary 2" | 130 | 9 |
| 6 | "phase 0 max high ≥ phase 1 max high" | 93 | 9 |
| 7 | "close at boundary 1 > D1_SMA10" | 72 | **10/10 ALL banks in top 30** |
| 8 | "close at boundary 3 > D1_SMA50" | 60 | 10 |
| 9 | "close at boundary 2 < D1_SMA20" | 53 | 10 |
| 10 | "close > M_SMA20 for every bar in phase 1" | 49 | 10 |
| 11 | "phase 0 min low ≤ phase 1 min low" | 46 | 10 |
| 12 | "close at boundary 1 > D1_SMA20" | 44 | 10 |
| 13 | "close at boundary 1 > D1_SMA50" | 42 | 10 |
| 14 | "close at boundary 3 > D1_EMA8" | 41 | 10 |
| 15 | "close at boundary 2 > W_SMA50" | 40 | 10 |

**Stage 4 — Pareto** (`dtss_stack_pareto.py`). Enumerated all 2^11 = 2048 subsets of an 11-filter version of the stack. Pareto frontier: empty set (0 filters) bank_sum=609, top30=5, nb_rate=1.0; 1 filter ("close ≥ open at boundary 3 green/doji") bank_sum=351, top30=6, nb_rate=0.9792; 2 filters bank_sum=327, top30=6, nb_rate=0.9583. Project memory `project_avatar_methodology_locked` records "DTSS proof: 5/10 → 10/10 in top 30, sum 609 → 72" — matches this Pareto curve.

**Stage 5 — non-bank validation** (`dtss_stack_nonbank_validate.py`). The 11-filter stack from stack_pareto applied operationally (each non-bank curated example, walk T-4..T-0; stack passes day iff ALL 11 filters pass; operational pass = passes any of 5 days). Result: n_eval=50, n_pass_any=17, **pass_rate=0.34**.

Per-filter rejection counts (which filter killed the most non-bank): "close ≥ open at boundary 3 (green/doji)" 24 rejections; "close at boundary 2 < D1_SMA10" 16; "phase 1 max high ≥ phase 2 max high" 18; "phase 1 min low ≤ phase 2 min low" 13; "close at boundary 0 ≤ close at boundary 2" 10; "close at boundary 3 > D1_EMA8" 7; "close at boundary 3 > W_SMA10" 4; "close at boundary 1 > D1_SMA10" 4; "close > M_SMA20 for every bar in phase 1" 4; "close at boundary 1 > D1_SMA50" 3; "phase 0 min low ≤ phase 1 min low" 2.

The locked stack (10/10 banks in top 30, sum=72 at step 7) drops non-bank operational pass rate to 34%. The first filter alone (green/doji at boundary 3) accounts for 24 of the 50 non-bank rejections — the single highest-leverage rule of the stack.

### Era 5 detailed catalogue — BASE post-filter investigation

BASE post-filter (`base_check_candidate` in `avatar_probe_from_db.py`) implements rules R1–R8 of §2.14. Investigation files in `swing-screener-bank-curator/research/`:

- `base_postfilter_pass_count.py` — For each of 38 BASE examples (11 bank + 27 non-bank), runs §2.14 at entry_date − 1 against each of 11 bank anchors, counts passes against ≥1 anchor's window plus per-rule failure breakdown. Direct invocation of `base_check_candidate`.
- `base_close_in_stack_validate.py` — Tests asof-anchored metrics on all 38 BASE examples plus 2000 null baseline windows. Wants distribution to move bank > non-bank > null on close_in_stack at asof.
- `base_range_compression_explore.py` — Tests battery of self-normalising candidate metrics on the bank's range-phase windows (range_start..asof) plus null baseline (50 random matched-length windows per bank). Goal: which metric most cleanly characterises "late-stage range contraction" shared by all 11 BASE bank range phases.
- `base_range_homogeneity_explore.py` — Wider net, 80 null per bank. Tests MA-stack, MA-catchup, time-in-band, and volume metrics for HOMOGENEITY across 11 banks AND DIFFERENCE from null.
- `base_rerank_with_stack_dim.py` — Re-ranks `BASE_historical_top99999.json` (73,812 candidates) by adding close_in_stack penalty to each row's dist_raw, recomputing per-anchor z-score, taking min z over anchors. Tests whether close_in_stack as new dim surfaces 38 curated higher.
- `base_singleday_test.py` — 5 specific (ticker, asof) test cases (SKY, IONQ-WS, STRL, FIX, CLS). Disables PNG + §2.14 post-filter, measures raw scoring rank.
- `base_scan_no_postfilter.py` — Full BASE historical scan with `base_check_candidate` monkey-patched to return (True, None), to isolate "29/36 pass" → "4/38 saved" cliff. If output jumps to ~29/38, §2.14 IS the cut.

The diagnostic finding from iter18 (in run_log): post-filter R1 (H-line), R5/R7 (MA ceilings), R8 (SMA330 floor) fired on specific curated examples — SIMO, IONQ-WS, RGTI, SNDK, DRN, PTON, IREN, FIX, QXO. Disabling §2.14 raised in_output 14/27 → 23/27, confirming R1/R5/R7/R8 reject 9 of 13 missing BASE curated.

### Era 5 detailed catalogue — Code progression iter1 → iter4 → iter29 → current

Tracked via `bak_iter1_start` (1249 lines, pre-iter1 baseline), `bak_iter4_kept` (1357 lines, +108 lines), `bak_pre_iter29` (1528 lines, +171 lines), and current `avatar_probe_from_db.py` (1925 lines, +397 lines).

**iter1_start → iter4_kept (+108 lines, 7 new helpers):** `_ASOF_MA_STATE_CACHE` global dict, `_compute_asof_ma_state(df, end_idx)` returning 7-dim numpy array of (close[asof] − MA) / ADR(30) for D1 SMA200/50/EMA8/21 and weekly EMA8/21/SMA50. `_get_asof_ma_state(cache, ticker, end_idx)` memoised accessor. `_ma_state_distance(bank_vec, cand_vec)` NaN-exempt Euclidean distance over dims valid in BOTH vectors, normalised by sqrt(n_used). In `run_one_setup`'s combine block: pre-compute each bank entry's bank_asof_ma_state once. For each anchor's valid candidates: compute dist_asof per candidate, rank-percentile twice (rp_phase from existing dist_raw, rp_asof from MA-state), per_anchor_score = min(rp_phase, rp_asof). Combine across anchors = mean. The diff against `bak_pre_meanrank` (an even earlier base, 1229 lines) shows the predecessor used PER-ANCHOR Z-SCORE COMBINE (mu, sd from candidate distances, then min across anchors), which iter1's commentary calls out as the broken default later replaced by mean rank percentile — matches Avatar_Correlator.md §6.7's combine-fix history.

**iter4_kept → pre_iter29 (+171 lines, additional helpers for iter19–28 explorations):** `SINGLE_BANK_MODE = False` flag (dev-mode using only first bank entry per setup). `_ASOF_FEATURES_15D_CACHE`, `_compute_asof_features_15d(df, end_idx)` — 15-dim universal at-asof feature vector. Dims 1–7 = 7 MA distances. Dim 8 close_in_stack = max(|dims 1–7|). Dim 9 20-bar mean range / ADR(30). Dim 10 5-bar mean range / ADR(30). Dim 11 ADR(5)/ADR(30). Dim 12 close/max(high last 30 bars). Dim 13 close/max(high last 252 bars). Dim 14 vol[asof]/mean(vol last 20 bars). Dim 15 (close − SMA330)/ADR. `_LOG_VOL_SERIES_CACHE`, `_RANGE_ADR_SERIES_CACHE` — universal precomputed series cached per ticker. `_phase_multivar_pearson_dist(bank_series_list, cand_series_list, bank_full_bdry, cand_full_bdry, L=50)` — per-phase length-normalised multivariate Pearson. iter28 logic in `run_one_setup` combine: per-phase length-normalised Pearson on log-close. Drop printing silenced for iteration speed.

**pre_iter29 → current (+397 lines, knn paradigm):** `KNN_MODE = "off"` (also "pearson", "dtw", "ncd", "pivot"), `KNN_LOOKBACK_BARS=100`, `KNN_RESAMPLE_L=50`. `_KNN_POOL_PRECOMPUTED` dict (setup → pool entries built once against FULL un-sliced cache). `_knn_build_pool_from_full_cache(setup, full_cache)` loads bank entries + non-bank fixture (research/iter_results/nonbank_fixture.json). `_resample_log_close(closes, L=50)` linear interp. `_discretize_returns(closes, n_bins=8)` log-return z-bucketed into bytes for compression-based distance. `_extract_pivots(closes, min_swing_pct=0.03)` ZigZag pivots. Plus `_pivot_levenshtein_distance`, `_dtw_sakoe_chiba_distance`, `_ncd_distance` helpers and `run_knn_over_curated` dispatch path that bypasses entire per-anchor + combine pipeline when KNN_MODE != "off".

### Era 5 detailed catalogue — `metric_research/` formal harness

`swing-screener-bank-curator/research/metric_research/` is a formal harness for systematic seed evaluation. Files: `RULES.md`, `SEED_CATALOG.md` — governance + ~85 atomic candidate seeds across 6 axes (A: distance functions on phase signature, A': distance on bar trajectory, B: template/avatar form, C: combine layer, D: feature representation, plus filter + metric meta-tests). `harness/` contains `data.py`, `metric_base.py`, `representations.py`, `templates.py`, `combines.py`, `phase_metrics.py`, `seed_queue.py`, `test_harness.py`, `seed_tier2.py`. `archive_pre_LOO_fix/` and `archive_pre_combine_fix/` contain T0 baseline (`T0_baseline_zdist`) plus per-axis seed results — T1A (metrics: A01–A04, A06–A10, EUC, L1), T1B (templates: B01–B12), T1C (combines: C01–C09), T1D (representations: D01–D09). Plus `T1X_cloud_rrf_d06_euc.json`. `discovery_findings/` contains `bioinformatics.md`, `audio_mir.md`, `biomedical.md`, `ts_classification.md` — survey notes on potential cross-domain methods.

The harness goal per RULES.md: "Find a similarity / scoring method that, baseline (no hard filters), lands every bank entry of HTF, BF, DTSS, BASE in top-10 of the full 11,500-ticker universe scan, and lands the majority of curated non-bank examples in top-30." Pass bars: every bank top-30 (quick), every bank top-10 + majority non-bank top-30 (full). Hard constraints: setup-agnostic, no symlinks, ticker-count gate, 24 GB RSS cap. Pre-existing rule-outs documented: raw distance sort (1/4), median/IQR/max/log normalize (0–2/4), bank-sumsq-normalize (0/4), within-anchor-rank, RRF alone with 6-d sig (1/4), cosine on signature vector (0/4), length_frac (1/4), universal drawdown_from_window_high_adr extra dim (2/4), adr_bar bar-by-bar relative-L2 (0–1/4). The harness archive directory contains pre-LOO-fix and pre-combine-fix state, suggesting the harness was rebuilt twice when those bugs were discovered.

### Era 5 detailed catalogue — `novel_approaches/` queue and results

Per-script standalone alternatives in `swing-screener-bank-curator/research/novel_approaches/`. No shared harness; each ~150–300 lines complete in itself, writes JSON to `results/`. Wave queue:

- Wave 1 (dispatched): pearson_logclose, chart_image_similarity, pivot_sequence_match, path_signatures, audio_fingerprint, self_similarity_matrix, compression_distance.
- Wave 2: dtw_barycenter, hmm_profile, catch22_features, minirocket_features, wavelet_decomposition, frequency_domain, sax_vsm_tfidf, recurrence_quantification.
- Wave 3 (rank-aggregation): lda_cross_setup_discriminative, triplet_closed_form, nca_projection, ledoit_wolf_mahalanobis, markov_chain_rank_agg, stuart_aerts_rra, birra_bayesian_rank, kemeny_borda_seeded.
- Wave 4: derivative_dtw, weighted_dtw, shape_dtw_hog1d, cross_recurrence_qmax, sbd_shift_invariant, kernel_density_anomaly, convex_hull_membership, topological_persistent_homology.
- Wave 5: tsfresh_minimal, hsmm_phase_duration, foldseek_3di_alphabet, cosine_local_correlation, multiscale_entropy, fractal_box_counting, hilbert_envelope, phase_locking_value.
- Wave 6: lcs_pattern_matching, linear_predictive_coding, arma_model_match, chebyshev_polynomial_fit, bezier_control_points, topological_recurrence_features, permutation_entropy, gradient_pattern_match.

Results from `results/*_summary.json`:

**pearson_logclose** (LOO cloud, mean rank percentile — Dan-authored simple Pearson on log-close):
- DTSS: 0/10 in top-30 (best ranks 476, 836, 990; median 1287.5; max 2281)
- HTF: 1/9 in top-30 (best rank 27; max 2697; CRCL skipped)
- BF: 0/12 in top-30 (best 37; median 541.5; max 5381)
- BASE: 0/11 in top-30 (best 506; median 1939; max 5010)

Result: pure Pearson on log-close fails the bank top-30 bar across all 4 setups. This is the cleanest single datapoint matching Dan's "simple and elegant means pure correlation" hypothesis: tested directly, it does not surface bank examples on this implementation.

**dtw_barycenter** (DBA centroid, LOO):
- DTSS: 1/10 top-30 (PSIX rank 1, ZIM 75; median 198.5; max 485)
- HTF: 0/8 top-30 (best 56; median 320; max 727; XPEV+CRCL skipped)
- BF: **5/12 top-30** — best of all wave-1/2 approaches on BF (GDXU 8, IONQ 9, IZEA 17, UAMY 19, EOSE 26; median 41.5; max 1171)
- BASE: 2/11 top-30 (APP 2, AMN 23; median 429; max 1472)

elapsed 21–44s per setup. DTW-barycenter is the strongest novel approach on BF, matching iter4's 5/33 ceiling.

**lda_cross_setup_discriminative** (LDA with setup-A positives, B/C/D negatives, LOO; centroid + nn_cloud variants):
- DTSS centroid: 2/10 top-30 (TRNS 5, PACB 6, CELH 44; max 449)
- DTSS nn_cloud: 1/10 top-30
- HTF centroid: 0/8 top-30 (best 182; max 1473); nn_cloud: 0/8 (best 76)
- BF centroid: 2/12 top-30 (RKLB 19, GDXU 21, IZEA 35; max 1123); nn_cloud: 1/12 (IZEA 9; max 1193)
- BASE centroid: 0/11 top-30 (best 105; max 2435); nn_cloud: 0/11

**pivot_sequence_match** (BF only): 2/12 top-30 (IONQ 13, HYMC 14; median 394; max 4300; sum 12005). Worse than DTW-barycenter.

The `ensemble_winners.py` file suggests an attempt to combine the best results across approaches, but none individually displaced iter4. Across all wave-1 attempts, dtw_barycenter on BF was the standout (5/12 top-30), and lda's nn_cloud variant was the strongest on HTF (76 best rank). For DTSS and BASE, no novel approach reached 3/N or better in top 30.

The verdict from this work was that no off-the-shelf time-series-similarity primitive (Pearson, DTW, NCD, pivot Levenshtein, Mahalanobis, LDA, DTW-barycenter, audio fingerprinting, self-similarity matrix, path signatures, wavelet decomposition, recurrence quantification, etc.) — applied as a single-distance-metric replacement for the iter4 phase template — produced a bank top-30 ranking on most setups. The implication recorded in the work log: "knn-over-curated with min-distance is the wrong paradigm. The curated set is heterogeneous in shape space; min over a heterogeneous pool gets dominated by chance similarities."

---

## Era 5 — The avatar correlator (2026-04-29 → 2026-05-09)

Era 5 is the project's pivot from "find conditions that fence in the example bank" to "rank candidates by similarity to the example bank." It also marks the project's first systematic use of explicit phase decompositions per setup — the architectural element shared with the outside reference implementation Dan flagged in this audit.

The shift was structural. Where Style 1 had searched a 16k-expression library for boolean conditions, Style 2 would: (1) define **phases** on each setup (uptrend → pullback → retrace for DTSS; analogous decompositions for HTF/BF/BASE/3-4DB), (2) compute a per-phase **signature** for every bank example (length, magnitude, direction, range), (3) build a **template** as the mean and standard deviation of those per-phase signatures across the bank, and (4) score every candidate window in the universe by z-distance from the template, after dynamic-programming-finding the boundary tuple that minimised the candidate's distance.

The recipe was setup-agnostic by construction — the same algorithm runs on DTSS, HTF, BF, BASE, with per-setup phase counts and bank examples picking the per-setup template. The scorer ran with **zero rules baked in**: hard filters, where used, were per-setup and discovered through a separate workflow (discovery → overfit-guard → greedy stack → Pareto selection) gated by the rule "minimum-size stack achieving 10/10 bank entries in top 30; whatever non-bank pass rate that produces is acceptable."

The era opened with diagnostic work. The **bank-curator worktree** built a bank-marking UI in `scanperfect.py` that let Dan annotate phase boundaries on each example and store them in `bank_entries (ticker, asof_date, n_bars, phases_json)` in `data/scanperfect.db`. With this in place, the avatar correlator's per-setup phase template could be computed objectively from human-marked data rather than auto-detected.

The first hard run was on **2026-05-04** in the `swing-screener-bank-curator` worktree — an autonomous 3-hour iteration session on the HTF setup. The session log (`research/work_log.md`) catalogued **30+ combine strategies × 4 score modes × multiple signature variants** plus cap-value sweeps, ADR-window sweeps, Pearson and cosine variants, at-high bonus, log compression, winsorisation, multi-resolution combines. The settled config produced 3/4 of the named target tickers (TRT #7, OGN #5, XE #11) in the top 30; REMX did not surface. After Dan confirmed REMX was a typo for RMAX, the same config with `ADR_LOOKBACK=30` produced 4/4 in the HTF top 11 — RMAX at #1 with `ADR_LOOKBACK=45`. **For HTF, the avatar correlator was working.**

DTSS was confirmed setup-agnostic on the same day. With nine hand-discovered hard filters (the original `Avatar_Correlator.md` spec block), DTSS achieved **10/10 bank entries in the top 30** with sum-of-best-ranks = 130, non-bank pass rate 50%. The spec's headline DTSS result. Cross-setup, the avatar correlator was producing usable rankings on DTSS and HTF.

Then BF, BASE, and HTF (without bank curation tightening) were run, and the result was the audit's closing problem. From the same `Avatar_Correlator.md` table:

| Setup | Bank | Universe | Best top-30 | Filters | NB% | Sum-of-best-ranks |
|---|---|---|---|---|---|---|
| BF | 12 | ~3000 | **3/12** | 20 | 70% | 1385 |
| BASE | 11 | ~2700 | **1/11** | 19 | 65% | 4079 |
| HTF | 9 (K=3, K=2 IPO entry dropped) | ~2200 | **7/9** | 15 | 72% | 210 |

The spec's recorded common failure mode is exact: "many bank entries score with high z-distance to mean-template (e.g., BASE bank phase 0 pct = +293% ± 444% — std bigger than mean), which puts them deep in the universe ranking. Filters can only prune ~80% of competitors at best; with bank already at rank 1500+, that's not enough to reach top 30."

This is Era 5's load-bearing finding, in the project's own words: when the bank's per-phase magnitude variance is large (standard deviation greater than the mean), the z-distance template loses discriminative power — every candidate is "within a couple of standard deviations" of the template, so candidates pile up at the head of the ranking with the bank examples mixed in among them. The hard filters discovered through the discovery workflow can prune candidates, but they cannot promote a bank example from rank 1500 to rank 30.

The deeper diagnostic ran in the same worktree under the `bank-curator` extended session continued through **2026-05-08**. The `research/continuation_prompt.md` snapshot captured the plateau: **5/27 BASE in_top30 across ~25 method variations**. Two stacked causes were identified and measured:

1. **Pre-scoring cliff.** The BASE post-filter rules R1, R5, R7, R8 were rejecting 9–13 of the 27 curated examples *before* scoring. Specific rules firing on curated examples: R1 (close > shallowest H- line) on SIMO, IONQ-WS, RGTI, SNDK; R8 (close vs SMA330 < 1.0 ADR) on DRN, PTON; R5 (close vs SMA50 > 3.0 ADR) on IREN; R7 (close vs W-EMA21 > 4.0 ADR) on FIX; R2 (close vs W-SMA50 < -1.0 ADR) on QXO. Disabling the post-filter (iter18) freed 9 curated examples — `in_output` went from 14 → 23.

2. **Scoring noise ceiling on the 14 that DO reach `in_output`.** Of those 14, only ~5 reached the top 30 of ~3000 post-filter survivors. Curated examples ranked at the ~95th percentile of post-filter survivors but lost to ~25 noise candidates per scan.

The 25 method variations tried during the BASE plateau included: per-phase Pearson on log-close, on log-returns, on (close-EMA21)/ADR; multivariate per-phase Pearson with log-volume and range/ADR; per-phase cosine; phase-length-ratio constraints; phase-type directional validity; iter4's six-dim signature (length_frac, end/max/min level, directness, argmax_position) with multi-bank min(rp_phase, rp_asof) combine and mean-rank-percentile across-anchor combine; rp_asof as a 7-dim ADR-relative MA-state distance at asof; per-dim z-score; across-anchor median; drop-max trim; close_in_stack pre-filter; bank-averaged template + Pearson; 15-dim Mahalanobis at asof. iter4 (the kept best) produced BASE 5/27, BF 5/33, DTSS 6/50, HTF 3/18. None of the 25 variants meaningfully exceeded that ceiling.

The **2026-05-05** finding (`project_avatar_combine_layer_broken` memory) sharpened the diagnosis: **single-day per-phase scoring puts curated examples at top 0.8% to 16.6% of the universe — per-phase scoring has signal**. None reach top 30 even on a clean single-day scan, but the scores are not noise. **The multi-anchor combine layer (per-anchor z-score → min-z dedup) is what buries the curated examples deeper.** Two structural issues: per-anchor z-score is apples-to-oranges across anchors with different distance distributions, and `min-z dedup` favours single-anchor outliers (the QUBT distribution had heavy right tail; QUBT-anchored candidates dominated the multi-anchor combine, even when the QUBT example itself was fine).

The proposed fix in `Avatar_Correlator.md` §6.7 is **mean rank percentile combine**: convert each anchor's distance to a within-anchor percentile, then average across anchors. Rank percentiles are unit-free and comparable across anchors, and averaging punishes candidates that don't rank well from at least one anchor. This was proposed but not validated end-to-end at the audit's close — the spec records it as Pending build.

The **2026-05-04** finding (`project_avatar_metric_inversion_finding` memory) added another data point: on HTF, higher raw distance correlated with **better-looking** charts — the metric was running backwards. The single-anchor CR-only test was named as the clean isolation test; if it worked, multi-anchor combine would be confirmed as the bug.

Three other findings from the same window:

- **`project_avatar_metric_ranking_failure` (2026-05-04)**: a diagnostic confirmed flat-z is BASE-specific. HTF and BF rank examples competitively; BASE alone has flat-z collapse (z range −0.030 to ~0) and buries curated examples at rank 26,284+. Only 4/38 curated BASE examples appeared in saved historical scan output. The original interpretation was that BASE phase taxonomy was too loose — BASE's defining feature is the MA stack tightness at asof rather than directional phase movement, and the generic phase signature didn't capture that. Later (2026-05-05) this was reinterpreted: the combine layer was the actual root cause, with BASE merely the most exposed setup because its z-spread is flattest.
- **`project_avatar_methodology_locked` (2026-05-08)**: the recipe was locked. New filters: 10/10 bank gate + ≥80% non-bank gate + greedy stack by aggregate-rank improvement. DTSS: 5/10 → 10/10 in top 30, sum-of-best-ranks 609 → 72.
- **`project_avatar_is_candidate_generator_not_scanner` (2026-05-07, DTSS-confirmed)**: even with the working DTSS recipe, shape alone does not predict trade outcome. The bank's curated entries are selection-biased winners; what they share is winning-shape, not outcome-causing structure. A classifier (in fade scope) on top of the avatar's candidate output is needed for trade selection. The avatar is a candidate generator, not the full scanner.

By **2026-05-09** Era 5 had produced one fully working setup (DTSS, 10/10 in top 30 with 9 hand-derived filters, hybrid Style 1 + Style 2), one partially working setup (HTF, 4/4 of named targets in top 11 with `ADR_LOOKBACK=30`), and three setups that did not cross the 10-of-N-in-top-30 bar (BF 3/12, BASE 1/11, 3-4DB has zero bank entries marked so the pipeline cannot run). The plateau on the breakouts had been characterised — pre-scoring cliff + combine layer noise — and a fix proposed (mean rank percentile combine) but not validated end-to-end.

The era's open question, as recorded in the spec and the work log: are the failures structural (the mean-template z-distance approach can't represent breakout setups whose bank examples vary by orders of magnitude in pct_move) or implementation-level (the combine layer can be replaced with a properly normalised scheme that surfaces curated examples at competitive ranks)? The outside-trader reference Dan flagged is consistent with either reading — they use phase decomposition on each setup with at least distance and time as phase descriptors, but the scoring/admission mechanism on top is unknown, and could be Style 1, Style 2, or something simpler than either. What the reference does establish is that the *problem class* is solvable; the §6.7 mean rank percentile proposal is one specific implementation-fix candidate within that space. The audit closes with the structural-vs-implementation question unresolved and with the project's only working production scan being DTSS.

---

## Synthesis — What the project's own measurements showed, and what they did not

### The shape of the problem, restated

Three months of work, ~700 hours, fifty-plus method variations, four full architectural rebuilds. The downstream stack — win/loss/no-enter classification, MFE measurement, market-regime EV scoring, exit-condition optimisation, profit-grinder management — produced working code that the project's specs describe as functional and that Dan described in this audit as good. The upstream stack — the scan that produces setup candidates the downstream stack would consume — produced numbers, on every variation tried, that fell roughly an order of magnitude short of the operational target on at least one of three axes:

- **Admission rate.** Target was the level implied by 2–7 setups/day across ~11,500 tickers (somewhere in the 0.001–0.01% range of universe bars on signal-firing days). Observed: either ~0% (compound-probability collapse on strict-AND filters) or 8–26% (after overfit protection or LOO-robustness loosening) — three to four orders of magnitude wide of the target on the loose end.
- **Held-out / forward / leave-one-out generalisation.** Target was 100% held-out coverage and ≥80% LOO admit. Observed: 0/15 BF held-outs admitted by pyramid signals (2026-04-27); 0/44 compose LOO on the V33 chunked stack and 1/44 on V47 even at the operational admission tightness (2026-04-29); rolling-hold-out admit rates of 0–5% at calibrated alpha across 24 anchor dates (2026-04-26). Multiple measurements at zero on a 0–1 scale.
- **Curated example surfacing.** Target was 10/10 (or N/N) bank examples in the top 30 of a universe scan. Observed under the avatar correlator: DTSS 10/10 (the only setup at target, after 9 hand-derived hard filters); HTF 7/9 with the spec recipe and 4/4 of named targets in top 11 with `ADR_LOOKBACK=30`; BF 3/12; BASE 1/11; 3-4DB unrunnable (no bank). On the BASE plateau across 25+ method variants, the ceiling sat at 5/27.

These three axes are not independent metrics measured against the same approach — they are the natural metric for whichever approach was being run at the time. Style 1's natural metric was admission and forward generalisation; Style 2's natural metric was example surfacing rank. The pattern is that every variation, on its own natural metric, fell ~10× short.

### What the data the project produced does and does not say

The data says:

- The 16k-expression cache, search-by-bounding-box approach, when paired with the overfit-protection regimes the team applied (alpha=0.01 BH-FDR-corrected dual gate; 30/15, 25/20, 35/10 chronological walk-forward; per-bar/per-chunk strict-AND with per-chunk null-lift gates), produced one or two single-condition rules per setup whose hold-out lift was 0.35–2.11 percentage points above baseline win rate and whose universe admission rate was high enough that they functionally do not filter.
- The same 16k-expression cache, paired with a chunked Mahalanobis strict-AND mechanism on Z-norm trajectories, produced the *opposite* failure — admission tight enough to deploy (0.0013–0.0026% on multi-cut walk-forward) but LOO compose admit at 0/44, indicating the system memorises example bar positions in a way that does not survive the removal of any individual example.
- The phase-template z-distance approach with DP-found candidate boundaries produced rankings on which the bank examples sit at the 0.8–16.6% percentile of the post-filter universe under single-day per-phase scoring — i.e., scoring carries real signal — but the multi-anchor combine layer (per-anchor z-score → min-z dedup) reorders the output in a way that buries curated examples deeper than top 30 on most setups.
- Each of these findings was produced by a specific experimental configuration, run by either Dan or an autonomous overnight agent, and validated within the bounds of the validation regime active at that moment.

The data does not say:

- That the bounding-box-via-beam-search approach is fundamentally incapable of producing a usable scan. It says the configurations tried produced rules that do not generalise under the protection mechanism applied. Whether a different protection mechanism (different alpha, different walk-forward split, different significance test, different example labelling, additional features) would produce different output was not exhaustively tested. The dual-gate protection itself has a calibration choice (alpha=0.01) that an analyst makes — at alpha=0.05, three additional cases on BF would have admitted (BF/365 d*=1 +2.1pp, BF/540 d*=9 +10.1pp, BF+BASE/270 d*=1 +0.3pp). The +10.1pp BF/540 case was kicking out 64% of held-out winners, which is why it was rejected. But "this calibration rejected it" and "the approach cannot work" are different claims.
- That the phase-template z-distance approach is fundamentally incapable of producing a usable ranker. It says the 25+ variants the team tried hit a 5/27 BASE ceiling. The combine-layer hypothesis (mean rank percentile combine, §6.7) is a specific implementation fix that has been proposed and not yet validated end-to-end. The outside-trader reference establishes that some simple per-phase decomposition with distance and time as descriptors solves the problem class — it does not specify whether that is a per-phase z-distance template, a per-phase bounding-box stack, or some other simple composition.
- That the example sets are clean. The OHLCV cache distribution-adjustment bug (2026-04-23) invalidated months of in-sample tuning evidence. The L14 labeler is "as forgiving as the weakest example" by construction; the OSCR LOO sensitivity (T 2.376 → 4.216, +77%, admission 38.6% → 26.0%) showed how a single mislabeled or atypical example can swing the calibration. The MFE_CAPTURE caveat — capture metrics measured on winners-only curated examples are in-sample on a biased subset — applies equally to every other measurement made on the curated bank.
- That the expression library is feature-complete. The long-lookback structural probe (2026-03-07) found that proposed new features (range_position_252/504, pullback_252/504) had median |Pearson| of 0.93–0.98 with existing pool expressions — the library is correlation-saturated in some directions. But the absence of certain features (e.g., the explicitly noted gap that no `pctrank_ext_*` expressions exist in the cache) was discovered late and not back-filled. The "complete and not being rebuilt" assertion in `EXPRESSION_ENGINE_V2.md` may have closed off feature additions that would have changed the available signal.

### Cross-cutting failure-mode synthesis (consolidated from spec ledgers)

Six structural patterns recur across every component's pending-research and known-bugs sections, and across every worktree's runlog. None of them is a verdict; each is an observation made by the project's own analysts in the project's own writing.

**Pattern 1 — Compound-probability collapse vs. no-discrimination tradeoff.** Strict-AND across many features with each individually tight admits zero universe bars; relaxing the same features individually admits most of the universe. The DARTBOARD test on 2026-03-10 captured this most cleanly: at one threshold, 304 signals and 1/66 examples passing; at another, 53,447 signals and 66/66 examples. No threshold sweet spot. PRESIGNAL §3.1 (zero hits when strict-AND'ing thousands of features), §3.2 (zero hits with two-stage composition), §3.4 (~500× too loose at 287–831 cells with effective rank 2–7), the chunked vs. scalar tradeoff in V47 vs. V48 (admission target requires LOO 0/44; admission 30–45× target buys LOO 47.7%), and the BASE flat-z collapse all share this shape.

**Pattern 2 — In-sample vs. forward-walk-forward gap.** Repeatedly, an approach that looked discriminative on the training portion of the example set lost its lift when held-out examples were tested. PRESIGNAL §3.6 (carve-greedy weekly chain capped at length 4 on BF — anchor-pinning incompatible with breakout setups). PRESIGNAL §3.7 (sign-coherence walk-forward FR far above target despite 100% in-sample agreement — high-dim cell pool overfits regardless of ensemble approach). PRESIGNAL §3.8 (pyramid 0/15 BF held-outs admitted). PRESIGNAL §4 (bbox walk-forward FR 73–90% across setups). The MFE_CAPTURE late caveat explicitly named: "all mean capture 0.76–0.84 numbers are in-sample on winners-only." `exp_walk_forward.py` measured the gap directly: at p_train=0.5 on BF, 47/76 hold-out winners dropped (62% loss). The L14 dual-gate at calibrated alpha=0.01: 0/15 admit under greedy-by-IS-burn AND 0/15 admit under greedy-by-HOLDOUT-burn.

**Pattern 3 — Single-example band-edge sensitivity.** A surprisingly large fraction of mechanisms had calibrations that turned on a single example. SIGNAL_EXIT_GRINDER L14 OSCR LOO: T jumps from 2.376 → 4.216 (+77%) on dropping one example, admission collapses 38.6% → 26.0%. PRESIGNAL §3.5 σ-cloud blowout from AXTI alone (on BF). SIGNAL_FILTER's BRKO universal 0.60-ADR loser-stop driven by PTON. ENTRY_GRINDER's 0/10,000 monotonic ratchet survivors interpreted as "the example set itself contains edge cases preventing any tight stop." LOO-self-distance threshold on the chunked stack: held-out test coverage drops to 0/20. The example bank's robustness to its own composition was systematically lower than the project's algorithms assumed.

**Pattern 4 — Post-rank combine breaks single-day signal.** Avatar §7.17 (2026-05-05) and the mean-rank-percentile-combine proposal in §6.7 are the cleanest expression. Per-anchor scoring has signal — curated examples sit at the 0.8–16.6% percentile of post-filter survivors under single-day per-phase scoring — but the multi-anchor aggregation reorders the output and buries them. Three structural issues with the current min-z combine: (a) per-anchor z-score is apples-to-oranges across anchors with different distance distributions (QUBT's heavy right tail dominates); (b) min-z dedup favours single-anchor outliers (a candidate scoring extreme from one anchor wins over a candidate scoring well from all anchors); (c) dedup discards information (relative consistency across anchors, the strongest signal of structural similarity, is collapsed to a single number). The implication: the failure may be in the aggregation, not the per-anchor metric.

**Pattern 5 — Survivorship and curation bias.** OHLCV_CACHE delisted-ticker policy: three orphaned curated examples (EXAS HTF 2021-01-19, PSTG BF 2024-03-22, NGD BASE 2026-01-05) and survivorship bias in the training pool. Every numerical result the project has produced was measured on a universe that systematically excludes losers. CLASSIFIER_SPEC §17.3 raised the possibility that the ~1100-cluster deduped breakout pool is "all real setups," making structural filtering a poorly-defined exercise. The bank itself is a hand-picked subset of historical winners; what the bank shares may be winning-shape, not setup-causing-shape (the Avatar 2026-05-07 finding made this explicit for DTSS — "shape alone does not predict trade outcome").

**Pattern 6 — Grinder-as-classifier confusion.** TODO_REWRITE captured the original observation: "the signal grinder finds vaguely extended stocks near highs, not stocks approaching their LSP and failing." REFINEMENT_GRIND_FIX captured the same shape on the refinement layer (the old `--blackout` mode re-ran the same examples-vs-universe grind with bars masked). REFINEMENT_GRINDER explicitly acknowledged: "Winners and losers may not be distinguishable by expression conditions… Winning might depend on things the 15,805 expressions don't capture — news, earnings timing, sector rotation, pure luck." Multiple components performed structural work that, when audited, did not in fact discriminate the thing the spec said it would.

**Pattern 7 — Estimation overconfidence.** UNIVERSE_EXPANSION Phase 2: previous-session 30-minute estimate, actual runtime 11 hours. CPU utilisation 8% on i5-12600K (10 cores idle). Multiple optimisation rollbacks recorded: precompute-all on daily data 5× slower than the previous version; daily arith two-phase dispatch slower in production cold-worker case; high worker counts caused contention rather than throughput. The estimation failures and the optimisation rollbacks share a shape — implementation choices made on theoretical-runtime arguments that did not survive measurement.

**Pattern 8 — Operational drift between architecture doc and code.** LOCALIZE.md says "local-first"; the code reveals five active scripts (`matrix_builder`, `exit_grinder`, `cycle_health`, `signal_filter`, `signal_exit_grinder`) still calling Railway. NIGHTLY_REFRESH Step 7 (earnings) has been silently broken since "the endpoints were never built" — earnings refresh has been silently going stale, and any feature that depends on earnings dates (the EV grinder uses earnings as a market-regime feature; the entry candle scorer's example dataset is filtered by earnings windows) has been operating against stale data the audit cannot quantify. FORWARD_PROP_SPEC's best-effort warning: "Bars appended via forward-prop may have NaN cells the same bar would NOT have under full rebuild — downstream consumers that treat NaN as fail silently exclude bars; consumers that treat NaN as pass silently over-count." This silent-failure hazard is documented but not architecturally fixed.

The eight patterns together describe an environment where: (a) the algorithm space being explored has a sharp tradeoff between admission tightness and bank-composition robustness that no variant has split favorably; (b) the example bank exhibits single-point sensitivities that destabilise calibration regardless of algorithm; (c) the universe being measured against carries survivorship bias that biases every metric upward; (d) the validation regime rejects most configurations at calibrated alpha, leaving only marginal-lift expressions to ship; (e) the upstream substrate has silent failure modes (forward-prop NaN handling, broken earnings, Railway drift) that contaminate downstream evidence in ways that are characterised but not fixed; (f) the post-rank aggregation may be the actual failure mode while the per-anchor scoring is genuinely producing signal. The patterns interact — fixing any single one in isolation may not move the operational metric meaningfully, while fixing multiple simultaneously may.

### Cross-cutting patterns the project's own writing called out

Six patterns recur in the spec ledgers, the worktree research logs, and the dated memory entries. None of them is a verdict; each is an observation the project's own analysts surfaced.

1. **Compound-probability collapse vs. no-discrimination tradeoff.** Strict-AND across many features with each individually tight admits zero universe bars; relaxing the same features individually admits most of the universe. The DARTBOARD test on 2026-03-10 captured this as cleanly as any single experiment: at one threshold, 304 signals and 1/66 examples passing; at another, 53,447 signals and 66/66 examples. No threshold sweet spot existed for that approach on those examples. PRESIGNAL §3.1, §3.2, §3.4, the chunked vs. scalar tradeoff in V47 vs. V48, and the BASE flat-z collapse all share this shape.

2. **In-sample / forward-walk-forward gap.** Repeatedly, an approach that looked discriminative on the training portion of the example set lost its lift when held-out examples were tested. PRESIGNAL §3.6 (carve-greedy weekly chain capped at length 4 on BF), §3.7 (sign-coherence walk-forward FR far above target despite 100% in-sample agreement), §3.8 (pyramid 0/15 BF held-outs), §4 (bbox walk-forward FR 73–90%). The MFE_CAPTURE late caveat — "all mean capture 0.76–0.84 numbers are in-sample on winners-only" — is the same shape applied to the exit grinder.

3. **Single-example band-edge sensitivity.** A surprisingly large fraction of mechanisms had calibrations that turned on a single example. SIGNAL_EXIT_GRINDER L14 OSCR LOO (T jumps +77% on dropping one example). PRESIGNAL §3.5 σ-cloud blowout from AXTI alone. SIGNAL_FILTER's BRKO universal 0.60-ADR loser-stop driven by PTON. ENTRY_GRINDER's 0/10,000 monotonic ratchet survivors interpreted as "the example set itself contains edge cases preventing any tight stop." The example bank's robustness to its own composition was lower than the project assumed.

4. **Post-rank combine breaks single-day signal.** Avatar §7.17 (2026-05-05) and the mean-rank-percentile-combine proposal in §6.7 are the cleanest expression of this. Per-anchor scoring has signal — curated examples sit at the 0.8–16.6% percentile of post-filter survivors under single-day scoring — but the multi-anchor aggregation reorders the output and buries them. The implication is that the failure may be in the aggregation, not the per-anchor metric. Whether that's the whole story has not been validated.

5. **Survivorship and curation bias.** OHLCV_CACHE delisted-ticker policy results in three orphaned curated examples (EXAS HTF 2021-01-19, PSTG BF 2024-03-22, NGD BASE 2026-01-05) and survivorship bias in the training pool. CLASSIFIER_SPEC §17.3 raised the possibility that the ~1100-cluster deduped breakout pool is "all real setups," making structural filtering a poorly-defined exercise. The bank itself is a hand-picked subset of historical winners; what the bank shares may be winning-shape, not setup-causing-shape (the Avatar 2026-05-07 finding made this explicit for DTSS).

6. **Grinder-as-classifier confusion.** TODO_REWRITE captured the original observation: "the signal grinder finds vaguely extended stocks near highs, not stocks approaching their LSP and failing." REFINEMENT_GRIND_FIX captured the same shape on the refinement layer (the old `--blackout` mode re-ran the same examples-vs-universe grind with bars masked, which added nothing). REFINEMENT_GRINDER explicitly acknowledged: "Winners and losers may not be distinguishable by expression conditions… Winning might depend on things the 15,805 expressions don't capture — news, earnings timing, sector rotation, pure luck." Multiple components performed structural work that, when audited, did not in fact discriminate the thing the spec said it would.

### Where the project ended on 2026-05-09

DTSS has a working scan that puts 10/10 bank entries in the top 30, with sum-of-best-ranks = 130, non-bank pass rate 50%, under the avatar correlator with 9 hand-derived hard filters. This is the project's only fully working production scan.

HTF has a partially working scan: 4/4 named target tickers in the top 11 under the avatar correlator with `ADR_LOOKBACK=30`, derived in the autonomous 2026-05-04 session, validated against the bank's marked phase boundaries.

BF, BASE, and 3-4DB do not have working scans. BF and BASE are blocked by the avatar plateau (3/12 and 1/11 respectively, with 25+ method variants tested at the BASE 5/27 ceiling). 3-4DB is unblocked structurally — no bank entries have been marked.

The pyramid grinder, the consensus pipeline, the presignal grinder (in any variant), the L14 refinement grinder, and the chunked-Mahalanobis presignal stack all exist as code and as numerical findings; none currently feed the production scan path. The nightly was stripped on 2026-05-03 to infrastructure-only — OHLCV append, intermediate cache rebuild, market cache, fundamentals, seed vault. No scanning runs on cron. The avatar correlator's DTSS path runs on demand.

### What this audit is not

This audit is past-tense narrative of what was tried and what the project's own measurements returned. It does not propose fixes. It does not pronounce any approach dead. It does not rule out that any of the failures recorded are calibration choices, labeling bugs, build-quality regressions, or user-error misalignments between what was tested and what should have been tested. The outside-trader reference point is the strongest single piece of evidence in this audit's possession that this *problem class* is solvable using simple per-phase decomposition — but the reference does not specify which scoring/admission mechanism that trader uses on top of the phases, so it does not by itself favour Style 1 or Style 2.

The closing observation of the audit window is that of all the work catalogued in this document, the components that produced the cleanest numbers — DTSS 10/10, HTF 4/4, the per-anchor scoring sitting at 0.8–16.6% percentile of post-filter survivors — are all parts of Era 5's avatar correlator, the youngest era and the only one operating in Style 2. The components that produced the muddiest numbers are spread across all four prior eras of Style 1. Whether that pattern reflects the relative tractability of the two approaches, or whether it reflects which approach received the most recent attention before this audit was written, is a question this document does not adjudicate.

---

## Appendix A — Worktree inventory

The project carried five active worktrees at the audit's close. Each was a parallel attempt at the same scan-quality problem from a different angle. None had been merged back into v2.

### `swing-screener-bank-curator` (branch: `bank-curator-ui`, HEAD `039aa17` 2026-04-26)

The principal active worktree. Contains the entire avatar correlator era as uncommitted work: `AVATAR_CORRELATOR.md` (the per-component spec), 80+ research scripts (`avatar_phase_probe.py`, `avatar_pipeline.py`, `avatar_probe_from_db.py` plus seven `.bak_*` snapshots from successive iterations including iter1, iter4, iter29, pre-meanrank, run-start), bank-discovery probes for DTSS / HTF / BF / BASE structural rules (`bank_dtss_*_probe.py`, `bank_chop_zone_probe.py`, `bank_lookback_probe.py`, etc.), filter-discovery and overfit-guard scripts (`dtss_filter_discovery.py`, `dtss_filter_greedy_stack.py`, `dtss_filter_overfit_guard.py`, `dtss_filter_rank_test.py`), iteration tooling (`iter_measure_ranks.py`, `iter_compare.py`, `iter_compare_top30.py`, `iter_baselines/`, `iter_results/`, `metric_research/`), and the `novel_approaches/` directory of late-stage experiments. Modified files: `DATA_CONTRACT.md`, `DEPENDENCY_MAP.md`, `SWING_SCREENER_PROJECT.md`, `scanperfect.py` (bank-marking UI). Session narratives: `research/work_log.md` (the autonomous HTF iteration log), `research/continuation_prompt.md` (the BASE plateau snapshot).

### `swing-screener-refinement-research` (branch: `refinement-overfit-research`, HEAD `039aa17`)

The refinement-grinder overfit-characterisation worktree. Contains `research/refinement_overfit_research/` with `FINDINGS_2026-04-27.txt` (the L14 dual-gate findings), `NEXT_SESSION_PROMPT.txt` (the WIN-retention-floor decision handoff), `dual_gate_runner.py`, `method_families.py`, 13 numbered experiment scripts (`exp_01_stability_n500.py` through `exp_13_margin_sweep.py`), and corresponding `results/*.json` per setup × experiment. The cached feature matrices in `cache/` were retained for re-use. The findings landed but the L14 refinement build itself was not started.

### `.claude/worktrees/presignal-quality-research` (branch: `worktree-presignal-quality-research`, HEAD `941682a`)

The 8-hour autonomous overnight session worktree (2026-04-28 / 2026-04-29) that built ~50 engines targeting 0.01% admission with 100% held-out coverage. Contains `research/_KICKOFF.md` (the original mission brief), `research/_audit.md` (the per-V audit checklist with 11 PASS-required items), `research/_runlog.md` (the V47 chunked-Mahalanobis stack runlog including the V33 → V47 → V49 trade-off table). Engine files: `bf_chain_beam.py`, `bf_chain_carve_greedy_weekly.py`, `bf_chain_state_machine.py`, `bf_chain_triangulate.py`, `bf_event_chain.py` (and v2), `bf_expr_trajectory_consensus.py` / `_filter.py` / `_walkforward.py`, `bf_chain_universe_mp.py`, `bf_data_driven_structure.py`, `bf_behavioral_sequence.py`, plus `protected_compose.py` / `verify_compose.py` / `bank_stability_check.py` / `loo_validate.py` / `protected_strict.py` / `protected_super_strict.py` / `protected_slack.py` / `loo_robust_compose.py` / `loo_robust_htf.py` / `compose_engines.py` / `_clock.py`. The session validated chunked-bbox stacks but exposed the LOO 0/44 limitation.

### `swing-screener-fade-detector` (branch: `fade-entry-detector`, HEAD `039aa17`)

Minimal contents: `research/fade_resistance_stats.py` and `research/fade_resistance_stats.json`. Reserved for fade-class entry-detector work (DTSS, 3-4DB) that was scoped but never got past initial probes.

### `.claude/worktrees/agent-a94f1789` (branch: `worktree-agent-a94f1789`, HEAD `941682a` 2026-03-07)

The earliest worktree, frozen in time at the long-lookback-feature exploration phase. Contents: `scripts/probe_long_lookback_structural.py` and `research/long_lookback_probe_results.md`. The probe found that proposed 252-day and 504-day structural features (`range_position_252`, `pullback_252`, `retracement_level_252`, `range_width_252` plus 504-day variants) had median |Pearson| of 0.85–0.95 with existing pool expressions like `pctrank_close_252`. This finding fed the decision (recorded in `EXPRESSION_ENGINE_V2.md`) that the cache was correlation-saturated and adding longer-lookback structural features would mostly add redundancy.

### Branches without worktrees but with diverged history

`v2-consensus` (2026-03-23 → 2026-03-26) holds the consensus pipeline build (Inc 1–11) before merge. `claude/sleepy-haslett` and `worktree-expr-cache-opt` carry expression-cache vectorisation and parallelisation experiments. `worktree-consensus-s2` is empty relative to v2.

---

## Appendix B — Archive / shelved artefacts

The `archive/` tree captures three categories of shelved work.

### `archive/shelved_docs/` (12 specs)

| Archived | File | Reason |
|---|---|---|
| 2026-03-14 | `DARTBOARD_DESIGN.md` | Dartboard scoring architecture. Tested 2026-03-10, rejected. Threshold sweep at peak=5 → 304 signals + 1/66 example pass; threshold at min-example-score → 53,447 signals + 66/66 example pass. Additive scoring washed out discrimination. |
| 2026-03-14 | `EXPRESSION_ENGINE_V2.md` | Earlier incomplete plan. Superseded by current root `EXPRESSION_ENGINE_V2.md`. |
| 2026-03-14 | `MULTISTAGE_EXIT_GRINDER.md` | Folded into profit grinder design. |
| 2026-03-14 | `REFINEMENT_GRIND_FIX.md` | Obsolete fix doc — old `--blackout` mode bug (re-running examples-vs-universe with bars masked) resolved by 2026-03-12 redesign. |
| 2026-03-14 | `TODO_REWRITE.md` | Feb 2026 LSP rewrite plan. Architecture abandoned when grinder went 100% generic on 2026-02-23. |
| 2026-04-11 | `ANALYSIS_SYSTEM.md` | V1 conceptual overview. Superseded by `PIPELINE_V2.md`. |
| 2026-04-11 | `EV_GRINDER.md` | Deferred to a future "live EV ranked watchlist" build. |
| 2026-04-11 | `HANDOFF_PARALLELIZATION.md` | Same content as `UNIVERSE_EXPANSION.md` Phase 2 (the 11-hours-instead-of-30-minutes vectorised-builder failure). |
| 2026-04-13 | `ENTRY_GRINDER.md` | Per-setup-class brute-force stop placement. v1 ratchet search returned 0 monotonic survivors out of 10,000 paths. Static stop only `fw_lowest_low_low` survived. |
| 2026-04-25 | `MFE_CAPTURE_PROJECT.md` | Multi-exit OR-set grinder. Hit 70% capture target across all setups (HTF 0.762, BF 0.768, BASE 0.836, DTSS 0.795) but parked when late caveats showed (a) measurements were on winners-only, in-sample on a biased subset, (b) optimisation objective mismatched downstream consumer's job, and (c) the indicator-lag-ceiling premise was wrong (53–79% of exits fired before MFE bar). |
| 2026-04-25 | `PROFIT_GRINDER.md` | Same shelving wave as EV grinder. Inc 1–4 complete. Reference design only. |
| n/a | `index_old.html`, `pipeline_old.html`, `vetting_old.html` | Pre-PySide6 HTML UI artefacts. |

### `archive/shelved_scripts/`

Per `SHELVED.md`: `dartboard_grinder.py`, `hybrid_grinder.py` (correlated booleans don't filter effectively), `proximity_grinder.py` (replaced by refinement path), `setup_refiner.py` (folded into refinement grinder), `market_grinder.py` (replaced by EV grinder; historical result kept at `regime_dtss_20260313_095056.json`), `setup_grinder.py` (replaced by EV grinder; historical result `setup_dtss_20260313_135931.json`).

### `archive/shelved_data/` and `archive/v1/`

Pre-v2 artefacts retained for reference. The v1 vetting endpoints, classified-signals files, and the original Railway-centric architecture all live here.

### Detailed shelved-docs narrative

**`DARTBOARD_DESIGN.md`** (shelved 2026-03-14 after failed test 2026-03-10). The dartboard concept was to replace the bounding-box+beam-search grinder with a continuous-score scheme — Cohen's d weighting per expression, composite score per bar, ranked output instead of a binary admit/reject gate. The intuition was that beam search produces brittle ANDed boolean conditions while a weighted score would degrade gracefully. Two operational tests on 2026-03-10 against 69 DTSS examples × 500 expressions: Run 1 set the threshold at 0.9158 to target peak=5 signals/day — produced 304 signals total, 1/66 examples passing (98.5% example loss). Run 2 set the threshold at 0.5948 (the minimum example score, which is the looser-end calibration that should preserve all examples) — produced 53,447 signals, 66/66 examples passing (53k signals/day across history vs 2-7/day target = 4 orders of magnitude off). No threshold sweet spot existed for that approach on those examples. Root causes identified: (a) additive scoring — a bar can be mediocre on most expressions and average to passable; (b) 500 expressions too many weak contributors dilute strong ones; (c) example distribution and universe distribution overlap too much for any threshold to discriminate. The doc proposed a hybrid (Cohen's d for expression selection + pyramid-style multiplicative filtering); the hybrid_grinder.py was built on 2026-03-10 (commit `8aee3d6`) and is itself shelved per `SHELVED.md` ("Correlated booleans don't filter effectively"). The dartboard test produced a clean datapoint that shapes the project's Style 1 understanding: the loose-vs-tight tradeoff has no sweet spot for a flat additive composition on this expression library against this example bank.

**`EXPRESSION_ENGINE_V2.md` (shelved older version)** (shelved 2026-03-14 after V2 build tasks A–G complete on 2026-03-02). The original V2 build plan added LSP detection, multi-timeframe OHLCV, and contextual AVWAPs to the expression cache. All seven tasks (A–G) completed 2026-03-02 with 12,421 expressions in cache (4,017 daily + 80 LSP + 44 algo + 246 generic exit + 4,017 weekly + 4,017 monthly). Multi-pass pyramid grinder working. Cache went from 21 GB → ~255 GB. Per-ticker compute time went from ~3-4s → ~8.5s. Full build ~84 min, nightly append ~15-20 min. "Highest all-time AVWAP" was excluded because only 5yr data was available at the time (deferred to full history). Subsequent code change on 2026-04-02 (commit `b2bd914`) removed all AVWAP code from the project except for `ta_knowledge.md` reference — Dan handles AVWAP manually at trade entry, the V2 build's AVWAP investment did not pay off operationally. The current root `EXPRESSION_ENGINE_V2.md` is the live spec; this archived version is the historical record of the multi-week build that produced the 12k → 16k expansion.

**`MULTISTAGE_EXIT_GRINDER.md`** (shelved 2026-03-14, last touched 2026-03-01 v4). The multi-stage exit concept was conditional exit strategies — partial trims, staged exits, gated conditions — implemented as 8 brute-force passes (single, early-trim, MFE-gated, protect+trail, bar-gated, cross-cond, 3stg-cross, refine). v4 was the final form: full parallelization, removed S99 backstop, fixed sort order to floor-primary, enforced 100% fire rule. The doc's design principle was "consistent reliable exits beat max-extraction-on-some-loss-on-others" — sort by floor (worst-case capture) primary, not mean capture. Folded into the planned profit grinder design.

**`REFINEMENT_GRIND_FIX.md`** (shelved 2026-03-14 as obsolete after redesign on 2026-03-12). Documents an old refinement grinder bug: `--blackout` mode ran the same examples-vs-universe grind as step 1, just with post-entry bars masked. Result: refinement added almost nothing (1,338 vs 1,218 step-1 signals — refinement was finding 120 new conditions on a 1,218-signal pool, which the doc identified as "the refinement grind is not actually grinding winners-vs-losers"). Refinement grinder redesigned 2026-03-12 to phase-1 cluster gathering + phase-2 cluster-aware beam search. The historical context is the same shape as the audit's central question: a component named "refinement" that wasn't actually doing refinement work — semantically misaligned with its spec until the bug was caught.

**`TODO_REWRITE.md`** (shelved 2026-03-14 as historical record). The 2026-02-26 LSP rewrite plan addressing the "Strip Bespoke System" decision on 2026-02-23 that ripped out all setup-specific LSP integration from grinders. The doc recorded the founding insight of the project: "the signal grinder finds vaguely extended stocks near highs, not stocks approaching their LSP and failing." All grinders had computation parity issues (different ADX implementations, different reference points). The LSP AVWAP — described as "single strongest DTSS filter" (100% of DTSS winners break below LSP AVWAP) — was never integrated. LSP detector accuracy was 78% with 4 misses (AAOI, BRK-B, SMMT, VUZI). The exit grinder bug recorded: CELH had exit triggering on bar 3 (2024-05-16) with entry not until 2024-05-22 — exit fired during formation period; 19/20 examples failed when measured from earliest signal bar. The 6 ordered tasks were: rewrite LSP detector with simple "highest unbroken pivot high" definition, integrate into signal grinder, validate LSP AVWAP as DTSS condition, re-run signal grinder, re-run exit/outcome grinders with formation-period validation, redo steps 8-9 with corrected inputs. The plan was abandoned when LSP integration was deemed not worth the regression risk vs the strip-bespoke decision; the project committed to setup-agnostic code as the binding constraint.

**`ANALYSIS_SYSTEM.md`** (shelved 2026-04-11). V1 conceptual overview — "Best setups × Best markets × Best management = Highest EV." Re-runnable pipeline as example library grows. Refers to retired components: market_grinder + setup_grinder (replaced by ev_grinder); refinement details predate Session 5 bar-count fix. The doc's pattern of failures became the pattern of the project as a whole — many grinders tried, many shelved (dartboard, hybrid, proximity, setup_refiner, outcome, multistage_exit, market, setup grinders all listed).

**`EV_GRINDER.md` (shelved version)** (shelved 2026-04-11). The earlier complete EV grinder spec at the time of archive — Phase 3 correlative scoring, replaced market_grinder + setup_grinder + the planned combined optimizer. Inc 1–6 + tree A/B complete. Open TODOs at archive time: generic setup feature validation (DTSS-only validator); signal conditions count was hardcoded then fixed. The Railway dependency on the validator script was fragile. Reference design only — current root `EV_GRINDER.md` is the live spec.

**`HANDOFF_PARALLELIZATION.md`** (shelved 2026-04-11 as superseded). Handoff doc for the vectorized expression cache builder parallelization task. Filename misleading — refers to the expression cache builder, not grinder workers. Same work documented in `UNIVERSE_EXPANSION.md` Phase 2. The Phase 2 outcome recorded in that doc: vectorized builder produces correct output but takes ~11 hours single-threaded vs 4.5 hours for the old per-ticker pandas builder. CPU utilization 8% on i5-12600K (10 cores idle). Root cause: 6.7M sequential Python `compute_expr_2d()` calls × ~18ms interpreter overhead. Numpy math is trivial. Multiple parallelization strategies considered (SharedMemory, chunked single-process, multiprocessing+pickle, ThreadPoolExecutor) — none implemented; Windows fork unavailable. The previous-session estimate had been 30 minutes; the actual runtime was 11 hours. The doc explicitly captured this estimation failure and added the rule "BENCHMARK AT SCALE before claiming performance improvements."

**`ENTRY_GRINDER.md`** (shelved 2026-04-13 as deferred). Per-setup-class brute-force search for the most aggressive stop placement and ratchet path that all examples survive. Outputs static stop, ratcheting stop path, breakeven window. v1 built 2026-03-27 but ratchet search returned 0 monotonic survivors out of 10,000 candidate paths. Static: only `fw_lowest_low_low` achieved 100% survival (1/8 attempted). BE window: 5 bars. The 0/10,000 result was interpreted as either "no monotonic stop-tightening path holds all examples" or "the example set itself contains edge cases preventing any tight stop." Banner on the doc: "Don't wire into pipeline without planning conversation." Pending issues recorded: seed vault doesn't back up `local_runner/cache/` JSON files or `signal_exit_grind/` for non-DTSS setups; signal condition validation needed (must verify signal grinder's conditions actually fire on the scan bar = entry_idx − 1 for every example, otherwise examples were matched by proximity logic, not actual signal firing); ratchet 0 survivors may be legitimate or may indicate edge-case examples preventing any tight stop; forward window source ambiguity (cluster file vs setups table).

**`MFE_CAPTURE_PROJECT.md`** (parked 2026-04-17, shelved 2026-04-25). Worktree (`swing-screener-dual-exit`, since deleted) project doc. Mission: break 70% mean MFE capture across HTF/BF/BASE/DTSS. Multi-exit OR-set grinder built and validated 2026-04-16. Headline result post-earnings-aware-scoring-fix: HTF 0.762, BF 0.768, BASE 0.836, DTSS 0.795 — target hit on every live setup. Then two late caveats came into focus that change the interpretation entirely. **Caveat 1: example-set overfit vs noise overfit.** The defenses protect against noise-fitting WITHIN the curated examples. They do NOT protect against the example set itself being a biased sample. Curated examples are handpicked winners; the full pyramid signal universe includes scratches, losses, no-entries. "All mean capture 0.76-0.84 numbers here are in-sample on winners-only." **Caveat 2: objective mismatch with downstream consumers.** The grinder optimised `capture_eff = realized_move / per-example-MFE`. signal_filter's actual job is winner/loser classification + mean-ADR-on-winners for the full signal population. Different optimization problems will select different rules. The diagnostic flips that landed late: fire-bar-vs-MFE diagnostic *inverted* the project's premise — exits fire PREEMPTIVELY, not late. 53–79% of exits fire BEFORE the MFE bar. The "indicator lag ceiling" narrative that had motivated multi-stage exit work was wrong. Take-profit grid: pure ADR target 6–15pp BELOW rule-based grinder picks — 16K-expression rule selection is doing real timing work pure price target cannot replicate. Gated TP, parallel rule-OR-TP: worse than rule-close on most setups. Per-ticker ext-rank exit prototype failed — signals enter ALREADY at the 86–92% percentile of ticker history, T=0.90 fires at bar 1; capture collapses to 0.15–0.49. Confirmed gap: no `pctrank_ext_*` expressions exist in the cache. The MFE_CAPTURE story is the audit's clearest example of how a project can pursue a wrong premise (indicator-lag ceiling) for months and only catch it via late diagnostic work.

**`PROFIT_GRINDER.md`** (shelved 2026-04-25). Phase 4 exit optimization — TA-expression-based exit conditions (not ADR price levels) for maximizing trade profit. `entry_candle_score` weighting, no trigger gate on unvetted winners (1-ADR-loss penalty), multi-stage trim search. Inc 1–4 complete. Replaces an earlier profit_grinder.py (2026-03-19) flagged as wrong (price-level brute force). Notes 8 dual-exit worktree experiments parked with the project: exit-lag diagnostic (earnings cap not respected — bug noted); fill-assumption rescore (peak-vs-close gap is upper bound); ADR-multiple TP grid; gated dual-exit (rule fires near peak, retraces, target rarely hits); parallel dual-exit (net near 0 vs rule-close — rule timing already good); per-ticker ext bimodality (BC scores 0.39–0.43 — visual bimodality weaker than statistical); per-ticker ext-rank exit (failed, see MFE_CAPTURE caveats); example chart renderer.

### `archive/shelved_scripts/` — script-level narrative

Per `SHELVED.md`, six scripts retired with reasons:

- `dartboard_grinder.py` — Additive scoring washes out discrimination. Tested 2026-03-10, see `DARTBOARD_DESIGN.md` above.
- `hybrid_grinder.py` — Correlated booleans don't filter effectively. Built 2026-03-10 (commit `8aee3d6`) as the proposed dartboard fix, tested, shelved when correlated boolean expressions in the cache prevented the multiplicative filtering from achieving the targeted carve.
- `proximity_grinder.py` — Replaced by the refinement grinder in `pyramid_grinder.py --blackout`. The proximity concept (loser-elimination via expression bounds) was folded into the cluster-aware refinement search.
- `setup_refiner.py` — Functionality folded into the refinement path of `pyramid_grinder.py`. Spec note: "Any surviving comments in `pyramid_grinder.py` referencing `setup_refiner` are dead — leave the comments for future cleanup but do not re-import."
- `market_grinder.py` — Replaced by `ev_grinder.py`. Historical result kept at `regime_dtss_20260313_095056.json`.
- `setup_grinder.py` — Replaced by `ev_grinder.py`. Historical result kept at `setup_dtss_20260313_135931.json`.

The pattern: the substrate (expression cache + pyramid + refinement) was kept; specialised grinders (dartboard, hybrid, proximity, setup_refiner, market, setup) were folded back into the substrate or replaced by the EV grinder. The architectural simplification was real — by mid-April the grinder layer had collapsed from 8+ separate scripts to pyramid_grinder + signal_exit_grinder + ev_grinder + profit_grinder, with the rest folded in or shelved.

---

## Appendix C — Key dated findings (for cross-reference)

| Date | Finding | Source |
|---|---|---|
| 2026-02-23 | Spiderweb single-tier grinder built; pyramid (D1→W→M→Q→6mo→1yr) added same day | git log; `ANALYSIS_SYSTEM.md` (archived) |
| 2026-03-06 | BUG-001: D1 over-locking, 62 examples → 168 signals vs 68 examples → 1,691 signals | `BUGS.md` |
| 2026-03-10 | Dartboard test: 1/66 or 53,447/66 — no threshold sweet spot | `archive/shelved_docs/DARTBOARD_DESIGN.md` |
| 2026-03-21 | Beam search instability flagged in TODO; consensus_engine.py committed same day | git log |
| 2026-03-25 | First overnight consensus run; expr cache 65GB; profit grinder memmap fix | git log |
| 2026-04-07 | Forward-prop engine landed (~19 min vs 124 min); CLAUDE.md added | git log |
| 2026-04-11 | Second shelving wave: EV grinder, profit grinder, ANALYSIS_SYSTEM, HANDOFF_PARALLELIZATION | `archive/shelved_docs/` |
| 2026-04-15 | MFE capture target hit (HTF 0.762 / BF 0.768 / BASE 0.836 / DTSS 0.795) | `archive/shelved_docs/MFE_CAPTURE_PROJECT.md` |
| 2026-04-17 | MFE_CAPTURE parked: in-sample-on-winners-only caveat; indicator-lag premise inverted | same |
| 2026-04-21 | Classifier rebuild mandate; presignal grinder spec begins | git log |
| 2026-04-23 | OHLCV cache distribution-adjustment bug fixed; downstream numerics required re-derivation | `OHLCV_CACHE.md` |
| 2026-04-25 | Rescue commit: presignal grinder + classifier infra + ext50 artefacts merged into v2 | git log |
| 2026-04-26 | L14 labeler shipped (`mfe_during_life ≥ T_setup`, T = min over examples) | `SIGNAL_EXIT_GRINDER.md` |
| 2026-04-27 | Pyramid walk-forward 0/15 BF held-outs; L14 dual-gate 0/15 admit at alpha=0.01 | `refinement-overfit-research` worktree FINDINGS |
| 2026-04-28/29 | V47 chunked-Mahalanobis stack: 0.0013% admission, LOO compose 1/44 (V47), 0/44 (V33) | `presignal-quality-research` worktree _runlog |
| 2026-05-03 | Nightly stripped to infrastructure-only; auto-scan retired | git log |
| 2026-05-04 | Avatar HTF autonomous session: 30+ combine variants, 3/4 (then 4/4 with RMAX/ADR=30) | `bank-curator/research/work_log.md` |
| 2026-05-04 | BASE flat-z collapse identified; phase taxonomy too loose for BASE | memory `project_avatar_metric_ranking_failure` |
| 2026-05-05 | Combine layer identified as primary ranking failure mode (per-anchor scoring puts curated at 0.8–16.6% percentile; combine reorders to deeper than top 30) | `Avatar_Correlator.md` §7.17 |
| 2026-05-07 | DTSS confirms shape alone doesn't predict trade outcome; avatar = candidate generator, not full scanner | memory `project_avatar_is_candidate_generator_not_scanner` |
| 2026-05-08 | Avatar methodology locked: 10/10 bank gate + ≥80% non-bank gate + greedy stack; DTSS 5/10 → 10/10 | memory `project_avatar_methodology_locked` |
| 2026-05-08 | BASE plateau: 5/27 across 25+ method variants over a 2-day session | `bank-curator/research/continuation_prompt.md` |
| 2026-05-09 | Audit window closes. DTSS production scan working; BF/BASE/3-4DB unsolved | this document |

---

## Appendix D — Active spec status as of 2026-05-09

This appendix walks each currently-active root spec, recording what was built, what is in pending research, and what known bugs were carried at the audit's close. The specs are the project's authoritative description of what is in production; their pending sections are the most direct enumeration of what was not solved.

### `CLASSIFIER_SPEC.md` — per-setup classifier rebuild

The successor doc to four deleted spec files. Defines the per-setup classifier producing a 5-pile classification (later collapsed in §15, 2026-04-21, to {WIN, LOSS, NO_ENTRY} — BE moved to profit_grinder scope, AMBIGUOUS dissolved by aggregate-profit exit). `_gather_raw_signal_clusters()` in `pyramid_grinder.py` is the runtime classifier. The `raw_signal_clusters` JSON exists for DTSS only; other setups have not had the classifier run end-to-end. The locked entry detector (§16, 2026-04-22) is argmax AVWAP resistance + foothold support MA + AND-gate; the locked classifier tag mechanic (§17.7, 2026-04-23) is ENTRY/REDUNDANT/NOENTRY/MISSING tags via resistance-AVWAP AND-gate alone (foothold + support-MA dropped after testing). The L14 labeler (§15.9, 2026-04-26) is the per-setup `mfe_during_life ≥ T_setup` rule with T = min over examples.

Pending research items recorded in the spec: the **DTSS classifier is too lenient** — 71.1% WR vs Dan's 40-60% expectation (§9.1). The proposed fix is to narrow the ceiling to signal-bar high only; not yet tried. **HTF and 3-4DB signal_exit top conditions have `floor_adr < 0`** — the "winning" exit rule loses money on at least one example (XPEV on HTF; the 3-4DB mechanism is not investigated). Fifteen-plus B-series and A-series scripts shelved (§7) with documented reasons — most either evaluated rather than derived rules, or used a fixed race kit conflicting with §2 constraints. **§17.2 (2026-04-23): single-bar classifier-stage filter on signal-bar features deprioritized** — single-bar features at the signal bar do not discriminate wild from examples once pyramid + presignal pre-filtered. Mahalanobis envelopes either over-cut (HTF 10.1%) or zero-cut (BF/BASE). **§17.3 reframing**: possible the ~1100-cluster deduped breakout pool is "all real setups" and the classifier task is outcome prediction, not structural filtering. **§17.4 next directions**: (A) temporal/multi-bar coiling features, (D) forward-outcome labeling. **Pyramid + presignal regrind needed** after 2026-04-22 DB cleanup (PTON, LMND, HTT changes) — has not been run.

The §17.3 reframing is consequential. If the deduped breakout pool is in fact all real setups (i.e. the upstream stack already does the structural filtering and downstream classification is a noisy-outcome problem), then the entire architecture's emphasis on tighter and tighter upstream filters is misallocated. The audit's central question — "why doesn't the scan surface the examples and similar charts" — has a different shape under this reframing: it surfaces them, but it also surfaces a comparable number of look-alikes whose forward outcomes are stochastic, and the discriminating layer is forward-outcome prediction, not structural matching. The reframing was raised but not resolved.

### `SIGNAL_GRINDER.md` — pyramid + consensus

Single-grind path stable. Multi-pass D1 + LSP + algo, then weekly HTF, then monthly HTF, with locked conditions across passes. Multi-run consensus through Increment 10. The NaN-asymmetry rule documented: search treats NaN as pass; locked conditions and validation treat NaN as fail. Per-bar tradable filter (close ≥ $1, dvol_20d ≥ $4M, ADRP ≥ 1.8%).

Pending research: **overfit-protected pyramid on §6-prefiltered universe** — the §6 stack admits ~14.5% of universe and is forward-stable but too loose to deploy alone; open question whether §6 + stability-selection produces forward-stable conditions. **Rejected approach (2026-04-28): pyramid example-subsample consensus.** Ten runs at 80% subsamples, only 2 of ~40 conditions per run survived 10/10 strict intersection (the broadest two). Walk-forward 14/15 coverage looked acceptable on the surface but was **misleading because the 2-condition union-band filter admitted ~26% of wild universe** — lift only ~3.6× over random. The root cause recorded: stability selection is the right tool for pyramid beam-search non-determinism, not for example-subsample variance. The 26%/3.6×-lift combination is precisely Dan's verbal description in this audit: the surviving conditions melted to two and admitted most of the universe. The decision was to not deploy this approach as-is.

### `REFINEMENT_GRINDER.md` — cluster-aware loser-elimination

`run_refinement()` in `pyramid_grinder.py` with three phases (gather, load, beam search). Cluster membership matmul for vectorized scoring. ProcessPoolExecutor was removed (RAM crash on large pools). Step 4-5 of consensus pipeline planned: 10× refinement runs with 50% loser cluster subsampling per run, then consensus engine with two-test validation (Meinshausen stability ∈ [0.6, 0.9] AND per-condition binomial p < 0.01 against universe baseline).

Pending: `--skip-gather`, `--subsample-losers`, `--seed`, `--conditions-file` flags for consensus refinement (Increment 6 + 9 not yet validated end-to-end). Open implementation questions: consensus threshold within 0.6–0.9 (start at 0.7?), binomial test memory strategy at scale, matrix-size adjustment if signal population grows, can `_load_refinement_piles` skip OHLCV cache load entirely. The legacy "WHY REFINEMENT CANNOT USE PERMUTATION TESTING" section (winner/loser data shapes asymmetric) is invalid for the L14 single-bar labeler — should be retired when L14 design lands. If zero conditions survive both tests, refinement is skipped; pipeline continues with the unrefined population. The doc explicitly acknowledges the failure-mode possibility verbatim: "Winners and losers may not be distinguishable by expression conditions… Winning might depend on things the 15,805 expressions don't capture — news, earnings timing, sector rotation, pure luck."

The verbatim acknowledgment is significant. The refinement grinder spec — written by Dan and the team across multiple revisions — names the possibility that the expression library does not contain the discriminating features, regardless of how the search is configured. That possibility was not investigated separately; it remains a hypothesis sitting in the spec.

### `SIGNAL_FILTER.md` — classification logic

Two paths (fade DTSS/3-4DB; breakout HTF/BF/BASE). Strict entry-1 example matching (the signal bar is exactly entry_idx − 1; clustering splits at example signal bars so entry-1 is always the rightmost of its own cluster). Intraday-touch fade stop. Unified ADR computation. Forward-window derivation fixed (no ×1.1). DTSS baseline 71.1% WR; BRKO baseline 48.9%.

Open issues recorded in the spec: **DTSS classifier too lenient** (71.1% above Dan's 40–60% expectation). Likely fix: narrow ceiling to signal-bar high only. **BRKO universal 0.60-ADR loser threshold driven by one example (PTON)**; proposed per-signal FW-low stop. **BRKO winner `move_adr` floor of −4.83 is a measurement artifact** from `entry_high`-vs-`signal_close` divergence. **Loser threshold stalls for high-ADR stocks** (RGTU 0.60 × $22.67 = ~$14 stop — too wide to be operational). **Tradable filter exemption for examples may distort scan statistics** — examples may pass the filter even when they would not normally pass the per-bar filter, inflating the scan's example-pass rate. **`breach_bar` semantics differ by path** (fade min FW+1; breakout min 1) but use the same field name — semantic ambiguity downstream consumers may stumble on.

The PTON-driven 0.60-ADR threshold is the same single-example sensitivity pattern that broke the σ-cloud presignal in §3.5 and the L14 labeler with OSCR. A pattern that recurs across three independent components is structural to the example bank, not to the algorithm.

### `SIGNAL_EXIT_GRINDER.md` — L14 labeler

L14 labeler shipped 2026-04-26: `mfe_during_life ≥ T_setup`, T = `min(example mfe_during_life)`. Setup-specific data-derived T values: HTF 2.376, BF 2.376, BASE 2.886. Lock holds by construction; wild WIN rates 38.6% / 42.4% / 42.3% across the three live setups. Legacy per-example exit-rule fit retained until pipeline reorder.

The OSCR LOO sensitivity is the headline pending issue: dropping OSCR shifts T from 2.376 to 4.216 (+77% jump in T value, drops admission 38.6% → 26.0%). The doc records this verbatim: "Same single-example-band-edge phenomenon flagged in PRESIGNAL_GRINDER.md §3.5." Accepted as ship-state. Fade implementation OUT OF SCOPE — DTSS/3-4DB have structurally different entry mechanics. Pipeline reorder pending: EV-after-Profit (`labeler → refinement → entry_candle_scorer → profit_grinder → ev_grinder`).

### `EV_GRINDER.md` — correlative scoring

Built and producing results (Inc 1–6 + tree A/B). Standalone via `python scripts/ev_grinder.py --setup <type>`. Uses 256 instruments × ~16,051 expressions for market features + 10 setup features (6 OHLCV + 4 fundamentals). Decile-spread screening (≥10pp WR or ≥1.0 ADR MFE), 3-pass dedup, category-balanced 50/50 weighting. **Not currently wired into the consensus pipeline orchestrator** — separate integration task. **DTSS-only validator with Railway dependency** — fails offline; needs setup-agnostic generalization. **Per-instrument 200-cap** on screening survivors is arbitrary. **No example pass-through guarantee in screening** — relies on a post-scoring hard-fail check.

### `MANAGEMENT_GRINDER.md` — deferred banner

The doc carries a deferral banner: "cannot be built until signal filter classification (Problem 1) is solved." Two scripts exist standalone but are not on the active call graph: `entry_grinder.py` v1 (needs rewrite — ratchet returned 0 monotonic survivors out of 10,000 paths, see archived `ENTRY_GRINDER.md`) and `profit_grinder.py` (Inc 1–4 complete). Rolling ratchet abandoned 2026-04-13 — tightens too fast, kills winners on pullbacks. Wide-indicator results are signal-relative artifact; entry-relative analysis "should find much tighter stops" but not yet built. The deferral itself names the audit's central problem: management cannot be optimised until classification is correct, classification depends on the upstream scan being correct, the scan is Problem 1.

### `EXPRESSION_ENGINE_V2.md` — cache architecture

Cache built and operational. HTF look-ahead bias fixed 2026-04-01 (partial candle engine, since reverted in favor of the simpler approach). Ten shipped optimisations (SLOW_OPS numpy, numpy bools, ext struct vectorisation, HTF dispatch, fast compression, worker-side saves, etc.). Universe rebuild post-2026-04-25 distribution fix: 11,534 tickers × 16,216 expressions (12 levels + 104 trendlines + 61 MOC added). Several optimisation rollbacks documented: precompute-all on daily data 5× slower; daily arith two-phase dispatch slower in production cold-worker case; high worker counts (cpu_count − 1) caused contention on i5-12600K; uncompressed saves exceeded disk budget. The data-quality fix on 2026-04-23 (forward-split-only adjustment, no dividend back-adjustment) required full cache rebuild — months of in-sample tuning evidence against the pre-fix cache had to be re-derived.

Pending: daily arith fallback ops for `dispatch_arith_numpy`; LSP+Algo structural cost (~0.64s/ticker floor); `ext_ceiling_ratio` rolling max. Three new sub-features (Extension Levels, Extension Trendlines, MOC) shipped 2026-04-25 to a worktree branch — should fold into §2 spec on next doc cleanup.

### `FORWARD_PROP_SPEC.md` — incremental append engine

Built and validated on AAPL Gate 1 (zero mismatches after 7 debug rounds). 4-file layout (.npz frozen + .append wider + .lookback + .state). ~1.43s/ticker × 11,200 / 14 workers ≈ 19 minutes (vs 124-minute full rebuild). Hybrid ExpressionEngine + scalar approach (pure scalar caused float16 precision drift). Build steps 7–16 pending (refresh OHLCV, full cache rebuild, one-time setup, Gate 2 all-ticker test, update load_ticker_cache + signal_filter._load_ticker_npz, update build_full() to clear .lookback/.state, push, audit, signal-filter regression, grind regression).

The doc carries a critical operational warning verbatim: forward-prop is best-effort — "Each phase wraps its per-expression computations in try/except pass. Any silently-failing path leaves the corresponding cell as the initial NaN. Bars appended via forward-prop may have NaN cells the same bar would NOT have under full rebuild — downstream consumers that treat NaN as fail silently exclude bars; consumers that treat NaN as pass silently over-count. Rule: Before any consensus pipeline run, run full rebuild." This is an unresolved silent-failure mode in the project's nightly path that has been characterised but not architecturally fixed.

### `OHLCV_CACHE.md` — data layer

Forward-split-adjusted, NOT dividend-adjusted (2026-04-22 policy). EODHD primary + yfinance gap fill / full-history sweep. ~920 MB daily, ~170 MB weekly, ~45 MB monthly. Split detection via per-ticker splits endpoint. Per-bar tradable filter authoritative in `pyramid_grinder.compute_tradable_masks()`.

Pending research: **delisted-ticker retention policy change** (shelved 2026-04-24). Current policy deletes delisted tickers on universe sync, causing two known consequences. (1) **Three orphaned curated examples** — EXAS HTF 2021-01-19, PSTG BF 2024-03-22, NGD BASE 2026-01-05 — are unreadable by the grinder and classifier because their tickers have been removed from the cache. (2) **Survivorship bias in the training pool** — the universe used to compute lift, FPR, and admission metrics excludes companies that subsequently delisted, which biases backtested WR and EV upward versus what would be observed forward on a universe that includes eventually-failing names. A full 6-phase plan was drafted; not committed to. The survivorship bias is an architectural finding sitting in the spec — every numerical result the project has produced was measured on a universe that systematically excludes losers.

### `NIGHTLY_REFRESH.md` — pipeline orchestration

Pipeline localised — Railway removed from OHLCV (2026-03-28) and market cache (2026-04-01). Only earnings (broken) and seed vault (intentional) touch Railway. Total runtime ~2–2.5 hr. Step 5 was rebuilt as intermediate-cache (`.im` files, ~1.7 min) — nightly does NOT update grinder's `.npz` expression cache. **Step 7 (earnings) BROKEN** — Railway endpoints don't exist; needs a local Yahoo Finance scraper. Earnings refresh has been silently going stale ("data has been going stale every night since these endpoints were never built"). **Step 9 (fundamentals) has dead Railway mirror call.** Expression cache append bottleneck — was 91 min single-threaded; HTF skip on non-rebalance days reduced to 30–35 min Tue–Fri (Mondays still full). Forward-prop engine projected to drop step 5 to ~19 min.

Earnings going stale silently is an operational hazard for any feature that depends on earnings dates (the EV grinder uses earnings as a market-regime feature; the entry candle scorer's example dataset is filtered by earnings windows). The audit cannot quantify how much downstream evidence was contaminated by stale earnings dates; the operational hazard exists.

### `LOCALIZE.md` — Railway-still-referenced scripts

Phases 1–4 + 6 complete. PySide6 desktop app drives pipeline. Daily OHLCV pickle loaded into memory at startup. Seed vault backs up 13 SQLite tables + 8 JSON file patterns nightly via step 10. Phase 5 (slim Railway server.py) low priority. Several active scripts still reference Railway examples API (`matrix_builder`, `exit_grinder`, `cycle_health`, `signal_filter`, `signal_exit_grinder`) — future cleanup. The architecture doc says "local-first"; the actual code says "still calling Railway in five places." Drift between architecture doc and code that has not been resolved.
