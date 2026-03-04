# TODO — Swing Screener (2026-03-04)

## Current State

Latest pyramid grind (grind #4): **86 conditions, 168 signals, peak 4/day (weekly pass) / 11/day (5yr), 59/59 examples pass, 14.1 min runtime.** 62 examples in Railway DB (59 resolved in grinder — BRK-B, SMMT, VUZI not in 5yr cache).

AI vetting pipeline operational — Claude Code reviews chart images, sentiment-based verdict parsing.

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works |
| 1 | Optimal Samples | optimal_samples | 62 DTSS examples |
| 2 | Signal Brute Forcing | signal_brute | Complete — grind #4 done |
| 3 | Sample Expansion | sample_expansion | 168 signals to vet |
| 3b | AI Sample Review | sample_review | Working |
| **4a** | **Exit Grinder** | setup_grinder_a | **Scripts built — READY TO RUN** |
| **4b** | **Setup Grinder** | setup_grinder_b | Locked until 4a choice made |
| 5 | Market Grinder | market_grind | After 4b |

### Step 4a: Exit Grinder — READY TO RUN

Runs two exit grinders sequentially. User reviews results in UI and chooses single or multi-stage before 4b unlocks.

| Script | Output |
|--------|--------|
| `scripts/profit_grinder.py` | `data/profit_grind/profit_{setup}.json` |
| `scripts/multistage_exit_grinder.py` | `data/multistage_exit/ms_exit_{setup}.json` |

Agent step: `setup_grinder_a`

### Step 4b: Setup Grinder — locked until 4a choice

| Script | Output |
|--------|--------|
| `pyramid_grinder.py --blackout` | `cache/pyramid_results_{setup}_blackout.json` |
| `scripts/setup_refiner.py` | `data/setup_refiner/refined_{setup}.json` |

`setup_refiner.py` reads exit choice from Railway → routes to single or multi exit condition.
Agent step: `setup_grinder_b`

---

## Grind History (DTSS)

| Grind | Date | Examples | Conditions | Signals | Peak/day | Notes |
|-------|------|----------|------------|---------|----------|-------|
| 1 | ~2026-02-24 | 20 | 41 | 264 | 3 | First production grind |
| 2 | ~2026-03-01 | 35 | 53 | 91 | 3 | After first vetting pass |
| 3 | ~2026-03-02 | 48 | ~76 | ~200 | ~3 | After second vetting pass |
| 4 | 2026-03-03 | 62 (59 resolved) | 86 | 168 | 4 (1mo) / 11 (5yr) | After AI vetting |

Pass breakdown (grind #4):
- Pass 1 (Daily+LSP+Algo): 69 conditions [379s]
- Pass 2 (Weekly): 12 conditions [267s]
- Pass 3 (Monthly): 5 conditions [200s]
- Total: 86 conditions, 847s (14.1 min)

---

## NEXT — Step 4a: Exit Grinder

Run `setup_grinder_a` in the UI (agent must be online). Runs profit_grinder.py then multistage_exit_grinder.py sequentially.

When complete: review results in ExitGrinderPage (single vs multi side by side), click "Use Single-Stage" or "Use Multi-Stage".

That choice unlocks Step 4b.

## After 4a — Step 4b: Setup Grinder

Run `setup_grinder_b` in the UI. Runs blackout pyramid re-grind + setup_refiner sequentially.

setup_refiner.py reads exit choice from Railway and routes to the correct exit condition.

Output: pruned condition set + filtered signals uploaded to Railway for next vetting pass.

---

## AI Vetting System

### Architecture
- Agent polls pending_examples table every 15s
- Downloads chart PNG via `/api/chart/{setup}/{ticker}/{date}`
- Calls `claude -p` with `--allowedTools Read`
- Parses essay-style output via sentiment analysis
- Posts verdict + reasoning to Railway

### Endpoints
- `GET /api/pending/{setup}` — list pending items
- `POST /api/pending/{setup}/{id}/review` — store AI verdict
- `POST /api/pending/{setup}/{id}/approve` — user approves, creates example
- `POST /api/pending/{setup}/{id}/reject` — user rejects, removes
- `POST /api/pending/{setup}/reset-reviews` — clear all reviews for retry
- `POST /api/pending/{setup}/backfill` — recover lost YES picks
- `GET /api/chart/{setup}/{ticker}/{date}` — chart PNG

---

## The Math

- Losers: solidly under 1 ADR
- Winners: median 5.8 ADR capture (from signal_exit_grinder)
- Even 36% win rate -> positive expectancy
- 50% win rate (after market grinder) -> massive profit factor
- 10% position size, 25% net winner/month = 2.5% compounding
- 20 years of 2.5%/month = mid 8 figures

---

## Data

- Expression cache: 4,119 tickers x 12,421 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- Earnings dates: batch scraped nightly
- 62 DTSS examples (59 in cache, 3 excluded: BRK-B, SMMT, VUZI)

---

## Grinder Rules (NON-NEGOTIABLE)

1. All grinders must use identical computation methods
2. All grinders must be optimized for maximum speed
3. All grinders must use full CPU cores
4. All results must pass 100% of setup examples - no exceptions
5. Expression cache is the single computation path

---

## Rules

1. NEVER proceed without explicit go-ahead
2. All grinders: 100% example pass. No exceptions.
3. Push all work to GitHub before ending chat
4. Break work into small tasks
5. All OHLCV from Railway DB or local caches
6. Read ta_knowledge.md before any TA work
7. NEVER dump large data into context
8. Expression cache = single computation path
