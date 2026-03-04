# TODO — Swing Screener (2026-03-04)

## Current State

Latest pyramid grind (grind #4): **86 conditions, 168 signals, peak 4/day (weekly pass) / 11/day (5yr), 59/59 examples pass, 14.1 min runtime.** 62 examples in Railway DB (59 resolved in grinder — BRK-B, SMMT, VUZI not in 5yr cache).

AI vetting pipeline operational — Claude Code reviews chart images, sentiment-based verdict parsing.

Profit grinder built (`scripts/profit_grinder.py`) — benchmarks from entry bar high, expression cache only, outputs per-example exit dates for blackout filter. NOT YET RUN.

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works |
| 1 | Optimal Samples | optimal_samples | 62 DTSS examples |
| 2 | Signal Brute Forcing | signal_brute | Complete — grind #4 done |
| 3 | Sample Expansion | sample_expansion | 168 signals to vet |
| 3b | AI Sample Review | sample_review | Working |
| **4** | **Setup Grinder** | setup_grinder | **NEXT — build this** |
| 5 | Market Grinder | market_grind | After Setup Grinder |

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

## NEXT — Step 4: Setup Grinder

Step 4 in the UI is a single step called "Setup Grinder" that runs the full loop internally:

### Sub-steps (run in order, no user intervention between them):

**1. Profit Grinder** (`scripts/profit_grinder.py`)
- Already built. Run exit grind from entry bar HIGH across all examples
- Outputs: exit condition + per-example exit dates
- Exit dates feed the blackout filter

**2. Blackout Filter → Re-grind** (matrix_builder.py change + pyramid_grinder re-run)
- Matrix builder loads profit grinder output
- Masks entry→exit bars per ticker per setup (post-entry bars excluded from universe)
- Re-runs pyramid grinder on clean universe
- Produces new condition set that can't fire on post-entry noise

**3. Condition Pruning** (new script: `scripts/condition_pruner.py`)
- Leave-one-out on every condition in the new condition set
- Measure filter power: how much of the remaining universe each condition eliminates
- Drop conditions below minimum filter power threshold (~10-15% universe reduction)
- Tightens scan, reduces overfitting

**4. Signal Filter + Vetting** (existing `scripts/signal_filter.py` + UI)
- Run signal filter on pruned condition set
- Upload signals to Railway for chart vetting
- User vets in Sample Expansion UI
- New YES picks → examples → loop back to step 1 until convergence

### Convergence condition:
Re-grind produces signals already in the example set → no new examples added → done.

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
