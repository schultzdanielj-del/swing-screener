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
