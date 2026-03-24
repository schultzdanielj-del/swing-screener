# Setup Analysis System

**The repeatable process for building any setup type into a fully optimized trading playbook.**

**The formula:**

> Best setups × Best markets for those setups × Best management = Highest EV possible

---

## Design Principle: Re-Runnable Pipeline

**Every phase is designed to be re-run as the example library grows.** New examples come from vetting — signals that turn out to be legitimate setups get added to the example library, and the pipeline re-runs from Phase 2 forward.

**Why this matters:** With 48 examples, you can trust floor and median metrics but not the tails. At 80 examples, you start trusting more aggressive extraction. At 150+, you can squeeze hard because the distribution is well-characterized. The system's output quality scales directly with example count.

**Rule: never hard-code example counts or tune to a specific example set.** All thresholds are relative (percentiles, ratios, floor/median) so they adapt automatically as examples grow.

---

## Pipeline Overview

```
Phase 1 — Sample Gathering
  a) Vetting System

Phase 2 — Causative Filtering
  a) Signal Grind
  b) Exit Signal Grind
  c) Refinement Grind

Phase 3 — Correlative Scoring (EV Grinder)
  a) Source external data (market cap, float, sector, fundamentals)
  b) EV Grinder — unified scoring of all market + setup-specific features

Phase 4 — Profit Optimization
  a) Profit Grinder — TA-expression-based exit optimization

Phase 5 — Live Watchlist
  a) Dynamic EV Scoring
  b) Live Nightly Workflow
```

---

## Phase 1 — Sample Gathering

### a) Vetting System

The vetting system is where setup examples are defined and collected. You start with a setup description and a baseline set of example trades. The more you vet, the better the entire system gets.

Vetting is a standalone workbench outside the pipeline loop. You open it when you want to vet, do your work, and the pipeline just sees a bigger example library next time you trigger a regrind.

**Two vetting modes:**

**Signal Grind vet** -- after signal grind, before refinement. Raw signals sorted by move_adr only.

**Post-Refinement vet** -- after refinement grind produces the winner pile. The entry candle scorer produces a combined_score per signal (entry candle similarity x move_adr, both as percentile ranks). Signals with both a big move and a tradable entry candle float to the top.

**Entry Candle Scorer** (scripts/entry_candle_scorer.py):
- Builds centroid from example entry candle expression vectors (16,051 dims)
- Computes per-expression discrimination weights (entry candle stdev vs forward window bar stdev, capped at 95th percentile)
- For each winner cluster: scans leftmost bar through rightmost bar + forward_window
- Scores each bar via weighted cosine similarity to centroid, keeps best match
- Combined score = percentile_rank(entry_candle_score) x percentile_rank(move_adr)
- Output: entry_scores_{setup}.json, mirrored to Railway (backup)
- Self-improving: more examples = tighter centroid = better scoring next session
- **Also consumed by profit grinder:** the raw `entry_candle_score` (not combined_score) is used as the tradability weight for exit optimization

**Vetting flow:**
1. Click Update Scores (entry candle scorer runs, ~10 seconds)
2. UI shows winners sorted by combined_score
3. Vet top-down: 1=YES, 2=NO, 3=SKIP
4. YES picks go to AI second-pass (pending_examples)
5. AI reviews against example library, GREEN_LIGHT or FLAG
6. One-click approve adds to examples table
7. When enough examples banked, trigger regrind

### CRITICAL: Scan Timing — The #1 Rule

**The scan runs AFTER market close the night BEFORE the entry.** The entry happens the next morning at the open. This means:

- **The scan candle = 1 trading day BEFORE the entry date.** If the entry date is Tuesday, the scan ran Monday night using Monday's completed bar.
- **ZERO entry candle data can be used in scan conditions.** The entry candle hasn't happened yet when the scan runs.
- **When analyzing examples:** if the example has `entry_date = 2024-05-22`, all conditions must be tested against the bar for `2024-05-21`. The scan is looking for charts that look like the setup 1-2 days before the entry, not on the entry day itself.

---

## Phase 2 — Causative Filtering

These steps find the mathematical conditions that separate setup bars from the universe. They are "causative" — they describe what the chart looks like when the setup is present.

### a) Signal Grind

Examples vs full universe. The pyramid grinder beam-searches 15,805 expressions across 4,167 tickers to find conditions where 100% of examples pass but most of the universe fails.

- Engine: Pyramid grinder, D1 cap=15
- Input: Example library + expression cache + 5yr OHLCV
- Output: Condition set + raw signal list across 5yr history
- Script: `pyramid_grinder.py`

### b) Exit Signal Grind

Finds the optimal exit condition — the expression threshold that best captures when the setup resolves (move is over).

- Input: Example entry bars + expression cache
- Output: Exit expression + direction + threshold
- Script: `signal_exit_grinder.py`

### c) Refinement Grind

Scans the full universe with signal grind conditions, clusters consecutive signal bars, classifies winners/losers via a ceiling+exit race, then beam-searches winners vs losers to find additional conditions that eliminate losing clusters.

Classification uses a ceiling + exit race (no ADR floor):
1. Forward window derived from examples: max distance from leftmost signal bar to entry bar across all examples, +10%.
2. Ceiling = max high across all cluster bars + forward window bars after rightmost bar.
3. Race: close above ceiling before exit fires → AUTO_LOSS. Exit fires first or price never breaches ceiling → AUTO_WIN.
4. Example clusters → AUTO_WIN regardless of race outcome.

Cluster-aware scoring: a losing cluster is only eliminated when ALL its bars are dead. No overcounting partial kills.

No re-scan, no re-classify after the beam search. Phase 1 classification (ceiling+exit race) is truth.

- Input: Signal conditions + exit condition + example library + expression cache + 5yr OHLCV
- Output: Combined conditions (signal + refinement) + filtered winner/loser signal lists with move_adr data
- Script: `pyramid_grinder.py --blackout`

### d) Consensus Pipeline (replaces single-run Phase 2)

Multi-run stability selection + permutation testing. Runs the signal grind 15 times on 50% universe subsamples with randomized pass ordering and 0% margin, plus 15 permuted runs (fake examples = noise floor). Bootstrap z-score gates the pattern at z > 3 (99.7% confidence). Consensus conditions locked with 5% margin. Then: deterministic scan → exit re-grind → 10 refinement runs with loser subsampling → two-test refinement validation (consensus stability + binomial significance per condition).

- Orchestrator: `scripts/run_consensus_pipeline.py --setup dtss`
- Test runner: `scripts/test_consensus_pipeline.py --setup dtss`
- Signal consensus: `scripts/consensus_engine.py --stage signal`
- Refinement consensus: `scripts/consensus_engine.py --stage refinement`
- Full spec: `SIGNAL_GRINDER.md` and `REFINEMENT_GRINDER.md`

The existing single-run pipeline (`pyramid_grinder.py --setup dtss`) still works unchanged. Both paths can coexist.

### move_adr measurement

Every cluster with an exit_bar gets `move_adr` (entry_high to exit_close, in ADR units), `adr_at_signal`, and `entry_high`.

Entry high: examples use entry candle high. Non-examples use forward window max high (worst-case entry — conservative).

Exit price = close of the bar where the exit condition fired.

`move_adr = (entry_high - exit_close) / adr_at_signal` for shorts.

---

## Phase 3 — Correlative Scoring (EV Grinder)

Phase 3 does not filter signals. Every signal that passes Phase 2 makes the watchlist. Phase 3 scores each signal with an accurate historical EV estimate so the watchlist can rank them.

### What the EV Grinder produces

Three numbers per signal:
- **Estimated win rate** — based on how signals with similar characteristics performed historically
- **Estimated median winner move (MFE)** — same basis
- **EV** — (WR × MFE) − ((1−WR) × 1.0 ADR assumed stop)

### Feature universe

**Market regime features** (~4M): 256 instruments × 15,805 expressions. Each instrument's expression value on the signal date.

**Setup-specific features (OHLCV-derived, 6):** price, ADR, dollar volume (20d avg), days since IPO, RS vs SPY (D1), RS vs SPY (W1).

**Setup-specific features (external data, 10):** market cap, float, volume/float ratio, sector mapping, RS vs sector, sector RS vs SPY.

### Architecture

1. Feature matrix: for each signal, look up every feature value on that date
2. Univariate WR screening: decile bucketing, keep features with D10-D1 spread > 10pp
3. Univariate MFE screening: same for winner move_adr, keep features with D10-D1 spread > 1.0 ADR
4. Per-instrument cap (top 200 by strength) + Union survivors
5. Cross-instrument deduplication: greedy dedup by inter-feature correlation (< 0.95)
6. Percentile scoring: continuous percentile rank per signal per feature (scipy.stats.rankdata), direction-flipped so higher = better
7. Category-balanced scoring: market features (50%) and setup features (50%) weighted equally, then weighted average → quality_score (0-100) + interpolated WR/MFE from decile curves → predicted EV
8. Validation: decile calibration (actual WR per quality_score decile), example coverage check

- Input: Refinement output + market cache + 5yr OHLCV cache + external data cache
- Output: Scoring equation + per-signal scores (WR, MFE, EV) + validation stats
- Script: `scripts/ev_grinder.py`

### Replaces

The EV grinder replaces `market_grinder.py` + `setup_grinder.py` + the planned combined optimizer. Results from both are preserved for reference.

---

## Phase 4 — Profit Optimization

### a) Profit Grinder

Finds the optimal TA-expression-based exit conditions for maximizing trade profit across the winner signal set. Brute-forces the expression cache (same 15,805 expressions) testing every expression × threshold × direction against forward price paths.

This is distinct from the exit grinder (Phase 2b), which found one expression condition for **classification** (separating winners from losers). The profit grinder finds expression conditions for **profit-taking** — the optimal time to close the trade once you're in it.

**Weighting:** Each signal's influence is determined by its tradability. Examples and vetted YES signals get weight 1.0 (hard gate — exit must trigger on all of them). Vetted NO signals are excluded. Unvetted winners are weighted by their raw `entry_candle_score` (cosine similarity to the example centroid). This measures "does this chart look like an entry I'd take" — information available at trade time. Move size is explicitly NOT part of the weight because it's the outcome, not the entry decision.

**No trigger gate on unvetted winners.** If the exit expression doesn't trigger on an unvetted winner, that signal is scored as a 1-ADR loss at its `entry_candle_score` weight. The weighted scoring naturally penalizes candidates that miss tradable charts (heavy penalty) while tolerating misses on non-tradable charts (negligible penalty). No bins, no hardcoded thresholds — fully continuous, self-referencing.

**Multi-stage trim:** The grinder searches for up to 3 exit stages (trim at first target, trim at second, exit remainder). Cascading search: 1-stage narrows candidate set, 2-stage builds on those, 3-stage on 2-stage.

**All stats weighted by `entry_candle_score`.** SQN, expectancy, equity curve, drawdown — all reflect performance on signals you'd actually trade.

- Input: EV-scored signals + entry candle scores + vetting decisions (SQLite) + expression cache + 5yr OHLCV
- Output: Exit expression candidates + weighted stats + equity curves + per-trade detail
- Scripts: `scripts/profit_grinder.py` + `scripts/profit_grinder_2stage.py` (Inc 1-4 COMPLETE)
- Saves to `local_runner/cache/profit_{setup}_{timestamp}.json`, mirrors to Railway

---

## Phase 5 — Live Watchlist

### a) EV Scoring

Each signal that fires tonight gets scored by the EV grinder's equation. Feature lookups + scoring curves = milliseconds per signal.

### b) Live Nightly Workflow

After market close:
1. Run tonight's bars against signal + refinement conditions → signals that fired today
2. Score each signal using the EV equation → estimated WR, MFE, EV
3. Rank order by EV, highest to lowest
4. You take the top N that you have capital for — the bottom ones don't get traded

The watchlist is the end product. Every cycle of the loop makes it more accurate.

---

## Key Design Decisions

- **Pyramid with D1 cap=15 is the official signal grind engine.** Experimental grinders (dartboard, hybrid) failed and are shelved.
- **Beam search instability accepted.** Individual runs produce usable signal sets despite low run-to-run overlap.
- **Cluster-aware refinement scoring.** A losing cluster only counts as eliminated when ALL its bars are dead.
- **No re-scan/re-classify in refinement.** Phase 1 classification is truth.
- **Examples run through full classification race.** Not skipped — they get exit_bar, ceiling, move_adr. Classification overridden to AUTO_WIN.
- **move_adr uses conservative entry price for non-examples.** Forward window max high = worst-case fill.
- **Setup-specific features are NOT from the expression cache.** The signal grind already mined all 15,805 expressions.
- **Phase 3 scores, it does not filter.** Every signal that passes Phase 2 makes the watchlist.
- **EV grinder replaces market_grinder + setup_grinder.** One unified engine, all features compete on equal footing.
- **Additive scoring model for ~893 signals.** Interaction terms deferred until more examples accumulate.
- **Category-balanced weighting (50/50).** Market features and setup features each get 50% of total weight regardless of headcount. Prevents 1,800 market features from drowning out 3 setup features.
- **Continuous percentile scoring (Option C).** quality_score 0-100 per signal, not discrete quartile levels. Slider 2 threshold is continuous.
- **Assumed stop of 1.0 ADR for EV calculation.** Adjustable without re-running.
- **100% example pass rate required.** Any grinder result where an example fails is invalid.
- **Profit grinder uses `entry_candle_score` weighting, not `combined_score`.** Move size is future information — not available at trade time. Entry candle similarity is the right tradability proxy.
- **Profit grinder: no trigger gate on unvetted winners.** Non-triggers scored as 1-ADR loss at their weight. The scoring function self-regulates. No bins, no hardcoded thresholds.
- **Profit grinder: TA-expression-based exits only.** No fixed ADR price targets or stop losses. The chart determines the exit through expression conditions.

---

## Data Flow -- Input/Output Per Step

| Step | Script | Input | Output |
|------|--------|-------|--------|
| Signal Grind | pyramid_grinder.py | Examples (local SQLite) + expr cache + 5yr OHLCV | pyramid_{setup}_*.json |
| Exit Grind | signal_exit_grinder.py | Examples + expr cache | Exit condition in local cache |
| Refinement Grind | pyramid_grinder.py --blackout | Pyramid result + exit cond + expr cache + 5yr OHLCV | raw_signal_clusters_{setup}.json + refinement_{setup}_*.json |
| Consensus Pipeline | run_consensus_pipeline.py | Examples + expr cache + 5yr OHLCV | consensus_signal_{setup}.json + refinement consensus + EV + profit |
| Consensus Engine | consensus_engine.py | Real + permuted grind JSONs | consensus_signal_{setup}.json (signal) or refinement consensus (refinement) |
| Entry Candle Scorer | entry_candle_scorer.py | Examples (local SQLite) + refinement output + raw_signal_clusters + expr cache | entry_scores_{setup}.json |
| EV Grinder | ev_grinder.py (complete, inc 1-6) | Refinement result + raw clusters + market cache + 5yr OHLCV + fundamentals cache | ev_{setup}_inc6_*.json (features, per-signal scores, calibration tables, redundancy analysis) |
| Profit Grinder | profit_grinder.py (rewrite in progress) | EV output + entry candle scores + vetting decisions (SQLite) + expr cache + 5yr OHLCV | profit_{setup}_*.json (exit candidates, weighted stats, equity curves, per-trade detail) |

All grinder outputs are also mirrored to Railway (backup) via file_mirror.py.
The entry candle scorer is not a pipeline step -- it is a standalone vetting utility that also provides tradability weights for the profit grinder.

---

## Infrastructure

- **Repo:** `schultzdanielj-del/swing-screener`, branch `v2` (production), `v2-consensus` (consensus pipeline)
- **Railway:** `https://web-production-e3025.up.railway.app`
- **Expression cache:** 16,051 expressions, ~21 GB
- **5yr OHLCV cache:** ~4,169 tickers
- **File mirror:** All grind results → Railway via `file_mirror.py`
- **Nightly refresh:** 4:30pm ET, 9 steps, fully automated
- **DB schema:** See `DATA_CONTRACT.md` for full Local SQLite schema
- **Pipeline spec:** See `PIPELINE_V2.md` for authoritative architecture
- **Task list:** See `TODO.md` for current work items

---

## Shelved / Legacy

- `dartboard_grinder.py` — additive scoring washes out discrimination
- `hybrid_grinder.py` — correlated booleans don't filter
- `proximity_grinder.py` — replaced by refinement grinder
- `setup_refiner.py` — legacy, unused
- `outcome_grinder.py`, `outcome_engine.py` — legacy
- `multistage_exit_grinder.py` — legacy
- `classify_universe.py`, `condition_pruner.py` — legacy
- `market_grinder.py` — replaced by EV grinder (results preserved)
- `setup_grinder.py` — replaced by EV grinder (results preserved)
- `DARTBOARD_DESIGN.md`, `EXPRESSION_ENGINE_V2.md`, `MULTISTAGE_EXIT_GRINDER.md` — obsolete docs
