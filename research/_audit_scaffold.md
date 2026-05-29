# Audit scaffolding (workfile, do not commit)

## Project lifespan
- 2026-02-16 → 2026-05-09 (~12 weeks)
- 1,385 commits on v2 branch
- Commits/month: Feb 384, Mar 880, Apr 119, May 2 (active development collapsed sharply in April)

## Branches
- `v2` (default head, f123285) — main working branch
- `main` (567 commits) — pre-v2 ancestor
- `v2-consensus` — consensus pipeline build (Mar 23–26 increment series Inc 1–11)
- `worktree-expr-cache-opt`, `claude/sleepy-haslett` — perf branches
- `bank-curator-ui`, `fade-entry-detector`, `refinement-overfit-research`, `worktree-agent-a94f1789`, `worktree-presignal-quality-research` — sister-dir worktree branches, all at v2 commit `039aa17` (2026-04-26) with active uncommitted research work

## Worktrees and what they hold (uncommitted)
- **swing-screener-bank-curator** — entire avatar correlator era. `AVATAR_CORRELATOR.md`, ~80 research/avatar_*.py + bank_*_probe.py + base_*.py + dtss_*.py + iter_*.py + test_*.py. Multi-iteration `.bak` snapshots of `avatar_probe_from_db.py` (run_start, iter1, iter4, iter29, pre_meanrank, prev). `research/work_log.md`, `research/work_start_time.txt`, `research/continuation_prompt.md`. Modified: DATA_CONTRACT.md, DEPENDENCY_MAP.md, SWING_SCREENER_PROJECT.md, scanperfect.py.
- **swing-screener-fade-detector** — minimal: `research/fade_resistance_stats.{py,json}`. Mostly empty.
- **swing-screener-refinement-research** — `research/refinement_overfit_research/` with FINDINGS_2026-04-27.txt, NEXT_SESSION_PROMPT.txt, exp_01..exp_13 experiments (stability, rolling holdout, synthetic null, shuffle count, search method families, inner sig gate, NaN handling, alpha, margin sweep, etc.), walk-forward + permutation null logs.
- **.claude/worktrees/agent-a94f1789** — old, just `scripts/probe_long_lookback_structural.py` + `research/long_lookback_probe_results.md`. HEAD frozen at 2026-03-07.
- **.claude/worktrees/presignal-quality-research** — large: BF chain-beam algorithms, behavioral sequence, event chains, expression-trajectory consensus, walk-forward, bare-boolean consensus, clustered consensus, sixstack masks. `research/_KICKOFF.md`, `_audit.md`, `_runlog.md`. HEAD frozen at 2026-03-07 but research dir is the most active layer.

## Root .md timeline (last-modified ↓ first-added)
| Last-mod | First-add | File |
|---|---|---|
| 2026-05-03 | 2026-03-29 | DEPENDENCY_MAP.md |
| 2026-05-03 | 2026-03-26 | NIGHTLY_REFRESH.md |
| 2026-05-03 | 2026-03-16 | LOCALIZE.md |
| 2026-05-03 | 2026-03-06 | PIPELINE_V2.md |
| 2026-04-26 | 2026-04-25 | SIGNAL_EXIT_GRINDER.md |
| 2026-04-26 | 2026-04-25 | CLASSIFIER_SPEC.md |
| 2026-04-26 | 2026-03-06 | DATA_CONTRACT.md |
| 2026-04-25 | 2026-04-25 | PRESIGNAL_GRINDER.md |
| 2026-04-25 | 2026-04-13 | SIGNAL_FILTER.md |
| 2026-04-25 | 2026-04-02 | OHLCV_CACHE.md |
| 2026-04-25 | 2026-04-02 | FORWARD_PROP_SPEC.md |
| 2026-04-25 | 2026-03-31 | CONSENSUS.md |
| 2026-04-25 | 2026-03-22 | REFINEMENT_GRINDER.md |
| 2026-04-25 | 2026-03-14 | EV_GRINDER.md |
| 2026-04-25 | 2026-02-27 | EXPRESSION_ENGINE_V2.md |
| 2026-04-25 | 2026-02-19 | ta_knowledge.md |
| 2026-04-25 | 2026-02-18 | SWING_SCREENER_PROJECT.md |
| 2026-04-24 | 2026-04-07 | CLAUDE.md |
| 2026-04-13 | 2026-04-13 | MANAGEMENT_GRINDER.md |
| 2026-04-11 | 2026-03-31 | SHELVED.md |
| 2026-04-11 | 2026-03-29 | Code_Auditor.md |
| 2026-04-11 | 2026-03-27 | UNIVERSE_EXPANSION.md |
| 2026-04-11 | 2026-03-22 | SIGNAL_GRINDER.md |
| 2026-04-11 | 2026-03-16 | UI_FLOW.md |
| 2026-04-11 | 2026-02-16 | README.md |
| 2026-03-17 | 2026-03-08 | GRIND_STORAGE.md |
| 2026-03-17 | 2026-03-06 | BUGS.md |
| 2026-02-21 | 2026-02-21 | pcf.md |
| (untracked) | — | Avatar_Correlator.md |

## Archive / shelved_docs (when shelved)
- 2026-03-14 batch (v1→v2 pivot): DARTBOARD_DESIGN, EXPRESSION_ENGINE_V2 (old), MULTISTAGE_EXIT_GRINDER, REFINEMENT_GRIND_FIX, TODO_REWRITE
- 2026-04-11 batch: ANALYSIS_SYSTEM, EV_GRINDER, HANDOFF_PARALLELIZATION
- 2026-04-13: ENTRY_GRINDER
- 2026-04-25 batch: MFE_CAPTURE_PROJECT, PROFIT_GRINDER

## Key narrative anchors (from memory + initial scan)
- **2026-03-06** — PIPELINE_V2.md added; v1→v2 pivot ("redirect to V2 — no more V1 patching")
- **2026-03-14** — first big shelving: v1 docs archived
- **2026-03-22 to 2026-03-26** — v2-consensus branch built Inc 1–11; first overnight consensus run
- **2026-03-23 to 2026-04-02** — consensus pipeline integration, expr cache 65GB, profit-grinder memmap fix
- **2026-04-07** — CLAUDE.md added with project rules
- **2026-04-11** — second shelving wave: EV grinder, profit grinder, ANALYSIS_SYSTEM all archived
- **2026-04-21** — classifier rebuild mandate; presignal grinder spec begins
- **2026-04-23** — OHLCV cache distribution-adjustment bug fix (downstream numerics had to be re-derived)
- **2026-04-25** — Rescue commit + presignal grinder spec + classifier spec + ext50 trendline rules merged
- **2026-04-26** — labeler change: WIN iff trade ran ≥ weakest example during life
- **2026-04-27** — pyramid walk-forward 100% failure rate (BF, 0/15 held-outs)
- **2026-05-03** — nightly stripped to infrastructure-only (no scan)
- **2026-05-04 → 2026-05-08** — avatar correlator era: combine layer broken, BASE phase taxonomy too loose, methodology locked, then DTSS confirms shape alone doesn't predict outcome
- **2026-05-09** — current

## Setup taxonomy used throughout
- **DTSS** (Double Top Short Sell) — fade class; 73 examples, primary benchmark
- **3-4DB** — fade class; 16 examples, never ground
- **HTF** — breakout class; 32 examples
- **BF** — breakout class; 45 examples
- **BASE** — breakout class; 42 examples
