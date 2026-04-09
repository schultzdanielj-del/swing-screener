# BUGS.md — Known Issues

**Note (2026-03-17):** Bug entries below reference Railway as authoritative store and pipeline_agent.py.
These are historical descriptions of past bugs. The current architecture is local-first — see LOCALIZE.md.
pipeline_agent.py is legacy, replaced by direct subprocess via QProcess in scanperfect.py.

This file documents confirmed bugs and structural problems in the project.
Each entry includes what was observed, what the root cause is, and what needs to be fixed.
Nothing in here gets touched until explicitly approved.

---

## Project Goal

Find 2-7 high-quality short setups per day historically, where the math works: losers
under 1 ADR, winners median ~5-6 ADR, win rate high enough that the expectancy is
strongly positive. Compound at 2.5%/month for 20 years.

The system is a nightly automated watchlist of signals for setups, dynamically rank
ordered by EV. The watchlist shows signal bars for potential setups and ranks them by
how likely they are to produce an entry candle very soon (ideally 1 day away) for a
trade that will run as much as the setup examples did on average/median.

Ranking is driven by historical EV:
- **Win rate** = ratio of signals that produced a winning entry bar like the examples,
  calculated per market condition bucket via the market grinder
- **Exit capture** = median MFE capture on exits from the exit grinder
- **EV** = win rate × median captured move

This goal has direct implications for what the grinder must do:
- Signal count must be low and consistent — 2-7/day. Not 50, not 0, not spiking to
  100 on random days.
- Every signal must be a legitimate instance of the setup — not a false positive that
  happens to pass conditions.
- The conditions must be robust — they need to hold up on new data, not just fit the
  example set.
- The system needs to scale — it has to work the same way with 150 examples as it
  does with 62.

Everything in the system exists to serve this goal. Any fix, change, or addition must
make the nightly watchlist more accurate, more reliable, or higher EV. If it doesn't
serve that goal directly, it doesn't get built.

---

## BUG-001: D1 Tier Over-Locking Destroys Downstream Tiers

**Status:** ✅ FIXED — D1 cap=15 implemented in pyramid_grinder.py
**Discovered:** 2026-03-06 during audit

### What Was Observed

Grind #4 (62 examples) produced ~168 raw signals.
Grind #5 (68 examples, same params) produced 1,691 raw signals — a 10x increase.

The 1wk tier in grind #5 had only **166 surviving rows** across 6 dates.
The same tier in earlier grinds had **1,364 surviving rows**.

The 1mo tier hit a ceiling at 27/day and could not get below it.
Every tier from 1mo onward was working with a decimated universe and could not compensate.

### Root Cause

D1 locks conditions greedily with no awareness of how many historical rows will survive.

With 68 examples, D1 found 29 conditions (vs 7 with 23 examples). Those 29 conditions
filtered the 1wk matrix from ~1,364 rows down to 166 rows — 8x fewer rows.

With only 166 rows across 6 dates, the beam search at 1wk has almost nothing to work
with. It hits a ceiling fast. That ceiling propagates through every subsequent tier.
The grinder produces more conditions but far weaker filtering — hence 10x more signals
despite more examples.

### Why This Violates the Project Goal

Signal count must be low and consistent — 2-7/day — and the system must scale reliably
as examples grow. A grinder that produces 10x more signals when 6 more examples are
added violates both requirements directly.

### Why This Is a Structural Problem

D1 and the historical tiers are coupled but D1 has no awareness of this.
D1 grinds to its ceiling and locks whatever it finds.
There is no constraint preventing D1 from locking so hard it destroys downstream tiers.
The problem silently gets worse as the example count grows.

### What Needs to Be Fixed

D1 must be constrained so that the conditions it locks cannot reduce the 1wk surviving
row count below a meaningful minimum threshold. Options:

1. **Hard row floor constraint on D1:** After each D1 condition is locked, check how many
   rows survive in the 1wk window. Stop locking D1 conditions if 1wk rows would drop
   below e.g. 500 rows.

2. **Remove D1 as a separate locking tier:** Let D1 conditions compete in the historical
   tiers instead of being locked first. D1's job becomes seeding candidates, not locking.

3. **Cap D1 condition count:** Hard limit on how many conditions D1 can lock (e.g. max 5-7),
   regardless of how many it finds.

Option 1 is the most principled fix — it directly addresses the coupling problem.

### What Must NOT Be Done

Do not touch the historical tier logic.
Do not change beam/depth params as a workaround.
Do not change the example range computation as a workaround for this specific bug.

---

## BUG-002: Pipeline Agent Step IDs Don't Match UI Step IDs

**Status:** ✅ FIXED (2026-03-07) — step IDs remapped, updated again 2026-03-14 to remove dead setup_refiner/market_grinder references
**Discovered:** 2026-03-06 during live system audit

### What Was Observed

`pipeline_agent.py` has a `STEP_COMMANDS` dict with these keys:
- `nightly`, `signal_grind`, `exit_grind`, `multistage_exit`, `signal_filter`,
  `profit_grinder`, `outcome_grind`, `backtest`

The Railway UI pipeline exposes these step IDs:
- `nightly`, `optimal_samples`, `signal_brute`, `sample_expansion`, `sample_review`,
  `setup_grinder_a`, `setup_grinder_b`, `market_grind`

The only match is `nightly`. Every other UI step has no corresponding agent command.

When the UI queues `signal_brute`, the agent receives the job, looks it up in
`STEP_COMMANDS`, finds nothing, and posts `error: Unknown step`. Same for
`setup_grinder_a`, `setup_grinder_b`, `market_grind`, `sample_review`.

This is confirmed by live Railway state: `sample_review` and `setup_grinder_b` are
both in `error` status right now. `setup_grinder_a` shows `done` because it was run
manually, not through the agent.

### Root Cause

The UI pipeline step IDs were renamed or redesigned at some point after the agent's
`STEP_COMMANDS` dict was written. The two were never resynchronized.

### Consequence

The agent is effectively non-functional for the current pipeline. Any step triggered
from the UI immediately errors. The only way anything has been run is manually from
the command line, which means no log streaming to Railway, no status tracking, and
no reliable record of what ran with what parameters.

### What Needs to Be Fixed

`pipeline_agent.py` `STEP_COMMANDS` must be updated to match current UI step IDs,
with each key mapping to the correct script and arguments:
- `signal_brute` → pyramid_grinder.py + signal_exit_grinder.py (sequential)
- `sample_expansion` → signal_filter.py
- `sample_review` → review_samples.py
- `setup_grinder_a` → profit_grinder.py + multistage_exit_grinder.py (sequential)
- `setup_grinder_b` → pyramid_grinder.py --blackout + setup_refiner.py (sequential)
- `market_grind` → market_grinder.py (not yet built)

---

## BUG-003: Grinder Results Are Never Uploaded to Railway

**Status:** ✅ FIXED (2026-03-08)
**Severity:** High — Railway has no accurate record of current grind state
**Discovered:** 2026-03-06 during live system audit
**Fixed:** 2026-03-08 — `grind_uploader.py` built into `pyramid_grinder.py`. Every grind writes local JSON + uploads to Railway in the same function call. Transactional upload with 5-point defense (retry, validation, partial upload protection, read-back verification, hash). V1 storage artifacts deleted. See `GRIND_STORAGE.md` for full reference.

### What Was Observed

`GET /api/grinder/results/dtss` returns a stale Phase 1 spiderweb result from
2026-02-23: 5 conditions, 3 passing tickers. This is the old single-day ceiling
result from before the pyramid grinder existed.

The actual pyramid grinder output (86+ conditions, 168 signals, grind #4 from
2026-03-03) exists only as a local JSON file on the desktop machine. It has never
been uploaded to Railway.

The signal count mismatch that triggered the BUG-001 investigation (1,691 signals
from grind #5) also only exists locally.

Railway's `backtest_signals` table for DTSS has **0 rows**.

The vetting UI shows 568 signals loaded (from a manual signal_filter run that did
upload), but the grinder results endpoint that the UI would use to show "what
conditions produced these signals" is stale and wrong.

### Root Cause

Two separate problems:

1. The pyramid grinder has no upload step. It writes results to local JSON files
   only (`local_runner/cache/pyramid_results_{setup}.json`). There is no code
   anywhere that POSTs pyramid results to Railway after a grind completes.

2. The backtest runner (`backtest_runner.py`) does upload signals via
   `POST /api/backtest/signals/upload`, but it has not been run since the
   grinder results changed. The upload and the grind are decoupled — running
   the grinder doesn't trigger the backtest runner.

### Consequence

Railway is operating on stale data throughout. The UI's "current conditions" display,
historical signal counts, and any downstream steps that read grinder output from
Railway are all working from the wrong data. There is no single source of truth —
the desktop has the real data, Railway has old data, and nothing enforces consistency.

### What Needs to Be Fixed

Every grinder that runs locally must upload its results to Railway as part of the
same run. This should not be optional or a separate manual step. Specifically:

- `pyramid_grinder.py` must POST results to a Railway endpoint on completion
- `backtest_runner.py` must be triggered automatically after a grind completes,
  not run separately
- Railway must be the authoritative store for all grinder outputs so the UI
  and downstream steps always read current data

---

---

## SESSION NOTE 2026-03-08 — Audit Reversal

Audit commit d0140ab was fully reverted (commit 32754a0).

**What was wrongly changed:**
- `exit_grind` pipeline step was rewired to `profit_grinder.py` — WRONG. PIPELINE_V2.md explicitly maps `exit_grind → exit_grinder.py` and marks it "Keep as-is"
- `sys.exit` → `RuntimeError` changes across profit_grinder, signal_filter, cycle_health
- Direction format normalization in profit_grinder + setup_refiner
- Stale upload removal from signal_filter

All reverted. Repo back to d9ea5e0.

**Root cause:** Audit was conducted without reading PIPELINE_V2.md first.

**Rule:** Always read PIPELINE_V2.md before touching any grinder or pipeline_agent.py.

---

## SESSION NOTE 2026-03-08 — Full Session Summary

**Commits this session (5479019 → 19211fa):**

### ✅ Done correctly

**`30e6522` + `d9ea5e0` — Pipeline agent v2 step wiring**
- `pipeline_agent.py`: Added missing v2 steps: `scan`, `refinement_grind`, `regime`, `health`
- `refinement_grind` stored as list-of-lists (`[[blackout grind cmd], [setup_refiner cmd]]`)
- `run_step()` updated to detect and execute multi-command steps sequentially, aborting on first failure
- `server.py`: Added `"prerequisites": []` to all 7 PIPELINE_STEPS entries (fixes latent KeyError)
- PIPELINE_V2.md + TODO.md updated to reflect 7-step pipeline and BUG-002 fixed status

**`39de53b` — BUGS.md reversal note**
**`19211fa` — Rule 10 added to TODO.md** (read PIPELINE_V2.md before touching grinders)

### ❌ Wrong and reverted

**`d0140ab` — Grinder audit (REVERTED by `32754a0`)**
- Conducted audit against rules posted in chat without reading PIPELINE_V2.md first
- `exit_grind` step rewired to `profit_grinder.py` — WRONG. V2 spec maps it to `exit_grinder.py`
- `sys.exit` → `RuntimeError` across profit_grinder, signal_filter, cycle_health
- Direction format normalization in profit_grinder + setup_refiner
- Stale upload removal from signal_filter
- All reverted. Root cause: read ANALYSIS_SYSTEM.md (v1 spec) instead of PIPELINE_V2.md.

**Net code state:** `pipeline_agent.py` and `server.py` have the correct v2 step wiring. All other files unchanged from pre-session.

**What's next for DTSS:** Refinement grind (step 4) — pull v2, restart agent, click Refinement Grind in UI.
