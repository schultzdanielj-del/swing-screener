# TODO

## Current State (as of 2026-02-22)

### The Grinder — Desktop Expression Discovery Engine ✅ LIVE

The core analysis engine is now a desktop-based spiderweb search system:
- **Desktop agent** (`local_runner/agent.py`) runs 24/7, polls Railway for jobs
- **Universe matrix** (~4,167 tickers × 1,338 expressions) auto-rebuilds nightly at 4:30pm ET
- **Spiderweb search** explores branching condition combinations via beam search
- **Frontend slider** controls grind depth (5 levels: 30s → 8 hours)
- **Results display** shows pass rate progression + best condition combo

**Architecture:**
- `local_runner/matrix_builder.py` — Precomputes universe matrix (daily, shared) + example matrix (per-setup, fast)
- `local_runner/spiderweb.py` — Beam search tree exploration
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Polling agent with nightly auto-rebuild
- `local_runner/cache_builder.py` — OHLCV cache from Railway DB
- `local_runner/brute_expressions.py` — 1,338 expression generator
- `server.py` — 13 grinder API endpoints (jobs/status/progress/results/agent)

---

## DTSS Pipeline Status

| Step | Status | Notes |
|------|--------|-------|
| 1 Load | ✅ Done | Data + TA knowledge loaded |
| 2 Receive | ✅ Done | 26 examples with LSP data |
| 3 Profile | ✅ Done | **THE GRINDER** — 1,338 expressions, spiderweb combo search, desktop compute |
| 4 Collaborate | **→ NEXT** | Take grinder ceiling, add discretionary/qualitative conditions together |
| 5 Backtest | Not started | |
| 6 Market Context | Not started | |
| 7 EV Optimize | Not started | |

---

## Immediate Next Steps

| # | Task | Description |
|---|------|-------------|
| 1 | **Run DTSS grind to completion** | First universe matrix build (~30 min one-time). Then run grinder at various levels to find the mathematical ceiling. |
| 2 | **Collaborative refinement (Step 4)** | Review grinder results together. Add market regime, AVWAP, algo line, and qualitative conditions that the math can't find. Goal: 0% daily pass rate (scan only fires when setup is present). |
| 3 | **3-4DB through grinder** | Already has 21 examples. Run through same pipeline — universe matrix is shared so no 30-min wait. |
| 4 | **Backtest validated conditions** | Run final conditions across 5 years of history, review signal quality |
| 5 | **Market regime analysis (Step 6)** | Build the "when to trade it" filter. 3-4DB showed 6-7x signal spikes during stage transitions. |
| 6 | **Daily scan automation** | Nightly job: run scan conditions against today's data, surface tomorrow's candidates. |
| 7 | **HTF setup examples** | Third setup type has zero examples. Need to collect and load. |

---

## Expression Library Expansion

Expand `brute_expressions.py` beyond the current 1,338 to cover more of the TA knowledge base. Target: use the 4:05pm–7pm rebuild window more fully (~45-60 min total still acceptable).

**What to add:**
- AVWAP expressions — distance to anchored VWAP from key pivots (requires volume-weighted calc from OHLCV)
- Relative strength vs SPY — price ROC ratio, beta-adjusted extension
- Sector relative strength — requires sector mapping + sector OHLCV
- Extension ceiling proximity — how close is current extension to historical max (needs longer lookback)
- Wave cycle position — where in the 3-5 day bounce cycle (flush → building → peak → fail)
- Volume character — accumulation vs distribution over N days, not just ratio
- Candle sequence patterns — e.g. inside bars, NR7, consecutive up/down closes over various windows
- Gap analysis — gap up/down size, fill status, vs ATR

**Constraint:** Rebuild must finish before 7pm ET. Monitor actual rebuild time as expressions grow.

---

## Other Priorities

| # | Task | Description |
|---|------|-------------|
| 1 | **3-4DB backtest → optimizer** | Run 800+ backtest signals through outcome precomputation + management optimizer. Get real EV numbers. |
| 2 | **EV optimization pipeline** | Build Step 7 — brute force management testing against MFE/MAE matrices. |
