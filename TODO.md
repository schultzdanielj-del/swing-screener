# TODO — Swing Screener (2026-03-04)

## Current State — DTSS SIGNAL GRIND DONE, MFE CAPTURE NEXT

Latest pyramid grind (grind #4): **86 conditions, 168 signals, peak 4/day (weekly pass) / 11/day (5yr), 59/59 examples pass, 14.1 min runtime.** 62 examples in Railway DB (59 resolved in grinder — BRK-B, SMMT, VUZI not in 5yr cache).

AI vetting pipeline operational — Claude Code reviews chart images, sentiment-based verdict parsing. 8/19 approved in first batch.

Exit grinder: `slope_xavgc21_off7_adr14 <= -1.123253` — median 5.8 ADR capture, floor 1.9 ADR, avg 21 bars to exit.

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works (6 steps) |
| 1 | Optimal Samples | optimal_samples | 62 DTSS examples |
| 2 | Signal Brute Forcing | signal_brute | Complete — grind #4 done |
| 3 | Sample Expansion | sample_expansion | 168 signals to vet |
| 3b | AI Sample Review | sample_review | Working — Claude Code + sentiment parsing |
| 4 | MFE Capture | mfe_capture | **NEXT: Entry bar grind + MFE capture grind** |
| 5 | Market Grinder | market_grind | After MFE capture |

### Vetting Flow

Vet signals (YES/MAYBE/NO) in Sample Expansion
  -> YES picks go to pending_examples table
Agent auto-reviews pending every 15s via Claude Code CLI
  -> AI verdict (APPROVE/REJECT/UNKNOWN) + reasoning stored in DB
  -> Sentiment analysis extracts verdict from essay-style output
Check Optimal Samples > Pending tab
  -> User APPROVE or REJECT each pick
  -> Approved = example created | Rejected = removed

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

## NEXT — Priority Order

### 1. Entry Bar Grinder (NEW — Part of MFE Capture)

**The missing piece.** Signal fires on some bar. We need to find the ACTUAL ENTRY BAR — the bar where you pull the trigger.

**Architecture:**
- Input: 62 examples with known entry dates + 168 grinder signals
- Grind target: the ENTRY BAR ITSELF across all examples
- Find conditions that are true ON the entry bar that distinguish it from surrounding signal bars
- Apply as additional filter on the 168 signals
- Result: signals that pass = "entry is TODAY" (not "setup exists somewhere nearby")

**Why this matters:** Can't have 50 potentials across 10 setups all waiting 5 days to fire. Too much data. Need "scan fires today, enter tomorrow morning."

**This is a brand new grind** — same architecture as pyramid grinder but different target. Not the bar before entry, not a window. THE bar.

### 2. MFE Capture Grind (Exit Optimization)

Current exit: `slope_xavgc21_off7_adr14 <= -1.123253` (median 5.8 ADR, floor 1.9 ADR)
Optimize entry bar high to exit for maximum capture.

### 3. Market Grinder (Step 5)

After entry bar + MFE capture are done:
- Correlate signal outcomes with market regime
- Find which conditions produce highest win rate
- Even 50% win rate with losers under 1 ADR and winners at 5-6+ ADR = massive profit factor
- 20 quality setups/year on a rare setup is a great result
- Target: 2.5%/month compounding, mid 8 figures in 20 years

### 4. Next Setup: 3-4DB

21 examples already loaded. Run through same pipeline after DTSS is fully complete.

---

## AI Vetting System (Built 2026-03-04)

### Architecture
- Agent polls pending_examples table every 15s
- Downloads chart PNG via `/api/chart/{setup}/{ticker}/{date}`
- Charts saved to `cache/review_charts/` (project dir, not temp)
- Calls `claude -p` from chart directory with `--allowedTools Read`
- `--system-prompt` forces review context
- Parses essay-style output via sentiment analysis (positive/negative signal counting)
- Posts verdict + reasoning to Railway

### Prompt
Checks 3 things the grinder CAN'T do visually:
1. Is there a visible double top?
2. Is the LSP (left side pivot / prior high) clean?
3. Did the stock actually break down after entry?

Grades A/B/C/F. Approves A+B, rejects C+F.

### Anti-Discretion-Drift
AI review prevents the user from loosening criteria during long vetting sessions. The AI applies the same criteria consistently regardless of how many charts have been reviewed.

### Known Issues (Resolved)
- Claude Code ignores strict output format -> fixed with sentiment-based parsing
- `--image` flag doesn't exist in claude CLI -> reference file by path in prompt
- Windows `shell=True` needed for both detection AND call
- Temp directory permissions -> use project `cache/review_charts/` dir
- Essay responses defaulting to REJECT -> fixed fallback logic

### Endpoints
- `GET /api/pending/{setup}` — list pending items
- `POST /api/pending/{setup}/{id}/review` — store AI verdict
- `POST /api/pending/{setup}/{id}/approve` — user approves, creates example
- `POST /api/pending/{setup}/{id}/reject` — user rejects, removes
- `POST /api/pending/{setup}/reset-reviews` — clear all reviews for retry
- `POST /api/pending/{setup}/backfill` — recover lost YES picks from vetting file
- `GET /api/chart/{setup}/{ticker}/{date}` — chart PNG for any ticker+date

---

## The Math

**Why this works even with low win rate:**
- Losers: solidly under 1 ADR (entry method gives tight stops)
- Winners: median 5.8 ADR capture
- Even 36% win rate -> positive expectancy
- 50% win rate (after market grinder filtering) -> massive profit factor
- 10% position size, single 25% net winner/month = 2.5% compounding
- 20 years of 2.5%/month = mid 8 figures

**The pipeline delivers:**
1. Entry bar grind -> tells you WHEN to enter (today, not "sometime this week")
2. MFE capture grind -> tells you WHERE to exit
3. Market grinder -> tells you WHICH signals to take (juices win rate)

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
