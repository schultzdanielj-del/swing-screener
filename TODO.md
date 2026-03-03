# TODO — Swing Screener (2026-03-03)

## Current State — GRIND COMPLETE, SAMPLE EXPANSION ACTIVE

Latest pyramid grind complete: **53 conditions, 91 signals (5yr scan: 409 raw -> 129 filtered for vetting), peak 3/day, 35/35 examples pass, 13.4 min runtime.** Tighter than previous best (264 signals, 41 conditions, 20 examples). Expression cache loads in ~50s (fingerprint bug fixed).

48 DTSS examples in Railway DB (was 51, removed FRT/FITB/WGO as sub-A+ quality).

### Pipeline Steps

| # | Step | Server ID | Status |
|---|------|-----------|--------|
| 0 | Nightly Refresh | nightly | Works (6 steps now - includes earnings refresh) |
| 1 | Optimal Samples | optimal_samples | Works (shows ADR move from exit grinder) |
| 2 | Signal Brute Forcing | signal_brute | Complete - UI has beam/depth/peak params |
| 3 | Sample Expansion | sample_expansion | 129 signals ready to vet |
| 3b | AI Sample Review | sample_review | NEW - automated Claude CLI review pipeline |
| 4 | MFE Capture | mfe_capture | Script exists |
| 5 | Market Grinder | market_grind | Placeholder |

### Vetting Flow (NEW)

Vet signals (YES/MAYBE/NO) in Sample Expansion
  -> YES picks go to pending_reviews table (not directly to examples)
Click SUBMIT FOR AUDIT
  -> triggers sample_review pipeline job
Agent runs review_samples.py -> claude -p reviews each pick
  -> AI verdicts uploaded to Railway
Check Optimal Samples > Pending tab
  -> APPROVE or REJECT each pick
Approved = example created | Rejected = rejected_signals

---

## Latest Grind Results (2026-03-03)

53 conditions (49 daily, 4 weekly, 0 monthly)
35/35 examples pass (BRK-B, SMMT, VUZI excluded - not in 5yr cache)
Pass 1 (Daily+LSP+Algo): 49 conditions
Pass 2 (Weekly): 4 conditions
Pass 3 (Monthly): 0 conditions
Signal filter: 409 raw -> 326 deduped -> 233 with exit -> 129 filtered (1.8 ADR floor)
Runtime: 13.4 min (matrix cached ~50s)

Previous best: 264 signals, 41 conditions, 20 examples, peak 3/day

---

## Built This Session (2026-03-03)

### Features
1. Grinder param UI - beam/depth/peak-target inputs on pipeline Step 2 (defaults: 10000/100/3)
2. Earnings dates DB cache - new earnings_dates table, batch scrape endpoint, nightly step 6
3. ADR move on Optimal Samples - reads exit grinder per-example captured ADR
4. AI Sample Review pipeline - full automated flow:
   - pending_reviews DB table
   - YES verdict -> pending (not direct example creation)
   - sample_review pipeline step wired to SUBMIT FOR AUDIT button
   - scripts/review_samples.py - CLI script calls claude -p per pending pick
   - Server endpoints: GET pending, POST review-results, POST approve, POST reject-pending
   - UI: 3-tab Optimal Samples page (Examples / Pending / Rejected) with approve/reject buttons
5. lxml added to requirements.txt - fixes earnings scraping on Railway

---

## NEXT - Priority Order

### 1. Sample Expansion (NOW)
- Vet the 129 filtered signals (Sample Expansion page)
- Submit YES picks for AI review -> approve/reject in Pending tab
- Target: grow from 48 to 80+ examples
- Re-grind after each batch of new examples

### 2. Condition Pruning (AFTER sample expansion)
- Leave-one-out pruning: remove each condition, measure peak/day impact
- Drop conditions where removal adds <3 signals and peak stays under threshold
- May not be needed if re-grind with 80+ examples naturally drops weak conditions

### 3. Entry Bar Detection/Grinder (NEW - future)
- Build entry grinder that sits between signal and exit
- Signal fires day X -> entry grinder finds optimal entry bar within X+1 to X+5 window
- Same architecture as exit grinder but forward-looking from signal
- Requires 60-80+ examples with accurate entry dates (already have entry dates on all examples)
- Grind expressions on post-signal bars to find conditions that fire on actual entry day

### 4. Market Grinder (Step 5)
- Correlate signal outcomes with market regime (SPY extension, breadth, VIX)
- Needs enough examples to split by regime with statistical meaning
- Best after sample expansion when signal count is higher

### 5. Overfit Mitigation
- Filter power per condition diagnostic
- Leave-one-out condition importance scoring
- Growing example library is the primary defense (48 -> 150 target)

---

## Data

- Expression cache: 4,119 tickers x 12,421 expressions (~50 GB)
- Railway DB: 11M+ OHLCV rows, ~4,167 tickers
- Earnings dates: batch scraped nightly for all tradable tickers
- 48 DTSS examples (45 in cache, 3 excluded: BRK-B, SMMT, VUZI)

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
9. Best grind benchmark: 264 signals, peak 3/day, avg 1.3/day, 41 conditions, 20 examples
