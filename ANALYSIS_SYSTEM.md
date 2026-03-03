# Setup Analysis System

**The repeatable process for building any setup type into a fully optimized trading playbook.**

**The formula:**

> Best setups × Best markets for those setups × Best management = Highest EV possible

---

## Design Principle: Re-Runnable Pipeline

**Every step (3-9) is designed to be re-run as the example library grows.** New examples come from Step 4 backtest review — signals that turn out to be legitimate setups get added to the example library, and the pipeline re-runs from Step 3 forward.

**Why this matters:** With 48 examples, you can trust floor and median metrics but not the tails. At 80 examples, you start trusting more aggressive extraction. At 150+, you can squeeze hard because the distribution is well-characterized. The system's output quality scales directly with example count.

**Re-run flow:** Add examples via chart vetting → re-run signal grinder (tightens with more data points) → re-run signal filter → vet new signals → repeat until convergence. Then re-run exit grinder (more examples = more confident exits) → run market grinder. Each step's scripts accept the current example set and produce fresh results. No manual state to manage.

**Rule: never hard-code example counts or tune to a specific example set.** All thresholds are relative (percentiles, ratios, floor/median) so they adapt automatically as examples grow.

---

## Step 1: Load Data & Knowledge

All OHLCV data already exists in the system. The `universe_ohlcv` table has 5 years of daily data for ~11,000 tickers, updated nightly via `POST /api/universe/append-daily` (incremental — fetches only missing days). The `tradable_universe` subset (~4,100 tickers meeting minimum price and liquidity requirements) is what we scan against. The full nightly pipeline (`python local_runner/nightly.py`) chains: Railway append → daily cache → 5yr cache → expression cache append → matrix rebuild. Runs after 4:30pm ET, ~15-20 min total. Agent auto-triggers if running.

Before starting any setup analysis, load `ta_knowledge.md` for full TA context — extension structures, channel behavior, AVWAP mechanics, market stages, wave cycles. This informs what to look for and how to think about the data. Load `pcf.md` for the complete PCF language reference — every function, operator, and syntax pattern available in TC2000.

**Verify before proceeding:**
- Universe OHLCV is current (most recent trading day present)
- Tradable universe is built and populated
- `ta_knowledge.md` is read and understood
- `pcf.md` is read and understood

---

## Step 2: Receive Setup Examples & Context

The user presents:
- **Setup description** — what the pattern is, what it looks like, why it works
- **Validated examples** — tickers with confirmed entry dates. **Remember: entry date = the day the trade is entered at the open. The scan candle is the trading day BEFORE the entry date.**
- **Any additional TA context** — LSP levels, channel structure, key relationships specific to this setup type

This gives a general starting point to understand what the setup looks like numerically. Ask questions if anything is unclear about the pattern mechanics or what distinguishes a good example from a bad one.

### ⚠️ CRITICAL: Scan Timing — The #1 Rule

**The scan runs AFTER market close the night BEFORE the entry.** The entry happens the next morning at the open. This means:

- **The scan candle = 1 trading day BEFORE the entry date.** If the entry date is Tuesday, the scan ran Monday night using Monday's completed bar.
- **ZERO entry candle data can be used in scan conditions.** The entry candle hasn't happened yet when the scan runs. Its Open, High, Low, Close, Volume — none of it exists at scan time.
- **When analyzing examples:** if the example has `entry_date = 2024-05-22`, all conditions must be tested against the bar for `2024-05-21` (or the prior trading day). The scan is looking for charts that look like the setup **1-2 days before the entry**, not on the entry day itself.

**Every time you write analysis code, verify you are using index `entry_idx - 1` (or the equivalent prior trading day) for all condition checks. Using `entry_idx` is WRONG and will produce conditions that can't work in real-time scanning.**

---

## Step 3: THE GRINDER — Signal Discovery (Pre-Signal Conditions)

**Goal:** Find which expression combinations best discriminate examples from the tradable universe. Get from 100% down to the mathematical ceiling — the tightest pass rate achievable through pure brute-force computation. Then eliminate historical noise until signals/day is manageable.

**This step is fully automated via THE GRINDER, a two-phase desktop search system.**

**Output: SIGNALS** — the set of all ticker-date pairs across 5yr history that pass all conditions. These are the raw signals before any outcome filtering.

**Note:** Step 8 (Pre-Signal Refinement) may add additional conditions back into this step after outcome analysis reveals pre-signal tells that were invisible when comparing examples vs the full universe. When that happens, the signal set is re-run and becomes TOTAL SIGNALS (tighter than the original, but all outcome signals still pass by definition).

### Phase 1: Spiderweb Search (single-day ceiling)

The grinder precomputes two matrices:
1. **Universe matrix** — every tradable ticker (~4,167 after filtering) × every expression (4,141 daily+LSP+algo), evaluated at the most recent bar. Shared across all setups. Auto-rebuilds nightly at 4:30pm ET. Parallelized across 8 CPU cores (~2.8 min on i5-12600K).
2. **Example matrix** — every setup example × every expression, evaluated at the scan candle (day before entry). Per-setup, fast (~5s per example).

The spiderweb search then explores branching combinations of conditions:
- Each node = a set of conditions applied together (AND logic)
- Thresholds derived from example ranges (all examples must pass)
- Score = % of universe that passes (lower = tighter = better)
- Beam search prunes weak paths, explores promising ones deeper

**The ceiling problem:** Phase 1 finds conditions that filter tightly on TODAY (e.g., 0.07% = 3 tickers) but when backtested across 5 years: 340 signals/day. The conditions describe "bull market uptrend" not the specific setup. The spiderweb stops at the single-day ceiling but there are still expressions that could eliminate massive historical noise without losing any examples.

### Phase 2: Pyramidal Grinder (nested time horizon noise elimination)

**✅ COMPLETE — PRODUCTION READY**

`local_runner/pyramid_grinder.py`

**How to run:**
```bash
# Standard run — sweep peak targets 2-10, take best result (~50 min total)
for /L %p in (2,1,10) do python local_runner/pyramid_grinder.py --setup dtss --peak-target %p --beam 10000 --depth 100

# Quick single run (~5 min)
python local_runner/pyramid_grinder.py --setup dtss --peak-target 3 --beam 10000 --depth 100
```

**Best result (2026-03-03, peak-target=3, beam=10000, 35 examples):**
- 53 conditions, peak 3/day, 91 signals (409 raw in 5yr scan), avg 1.4/day
- Runtime: 13.4 min with expression cache
- Previous best (2026-02-24, 20 examples): 41 conditions, 264 signals, peak 3/day

**Key parameters:**
- **beam=10000** — explores 10K paths per level (exhaustive — search terminates when no path improves)
- **depth=100** — up to 100 conditions per tier (search self-terminates when exhausted, typically ~25-30)
- **peak-target=N** — target ≤N signals on any single day. Different values lock different conditions at earlier tiers → different 5yr starting points → explore different solution paths. **Always sweep multiple values and take the best.**

**How it works:** The pyramid progressively widens the historical window, each tier grinding until `peak_signals/day < threshold` before advancing:

1. **D1 (today):** Spiderweb grind on today's snapshot → finds ceiling. Lock conditions.
2. **1 week:** Build matrix from last 5 trading days. Grind until peak/day < threshold. Lock.
3. **1 month:** Matrix from ~21 trading days. Same. Lock.
4. **6 months:** Matrix from ~126 trading days. Same. Lock.
5. **1 year:** Matrix from ~252 trading days. Same. Lock.
6. **5 years:** Matrix from all trading days. Same. Lock.

**Why this is fast:** Expression series cache pre-computes all 12,175 expressions for all 4,119 tickers × 5yr on disk (~50 GB). Tier matrix builds load pre-computed arrays instead of calling compute_series() thousands of times. Matmul pre-screening (beam × rows @ rows × candidates) estimates joint signal counts without materializing all combinations — only top candidates get exact peak scoring. OpenBLAS with MAX_THREADS=24 parallelizes matmul across all cores.

**Why peak-based:** Average 7.4/day sounds fine but hides days with 260 signals. Peak-based guarantee means no single day overwhelms manual review.

**Why sweep peak targets:** Different peak-target values lock different conditions at the 6mo/1yr tiers (which have fewer rows to discriminate), creating different starting points into the 5yr tier. The 5yr tier is where most conditions get added and where the expression library's discriminating power is tested. peak-target=1 is usually too aggressive (prunes everything), peak-target=2-5 tend to produce the best results.

**NaN handling:** Expression ranges require 70% of examples to have non-NaN values (minimum 3). This maximizes the candidate pool — a few NaN values (e.g., RSI slope on short-history examples) don't eliminate useful expressions.

**Determinism:** Row ordering is sorted by (date, ticker) to eliminate process-pool scheduling variance. Beam search tiebreakers use (peak, -total_signals, condition_indices) for reproducible results.

**Constraint:** 100% of setup examples must ALWAYS pass all conditions at every tier (zero false negatives).

**DTSS grind progression (peak-target=3, beam=10000):**
- D1: 4 conditions → 0 tickers passing today
- 1wk-1yr: ~3 conditions added across these tiers
- 5yr: ~19 added → peak 6/day (ceiling at level 26)
- Total: 26 conditions, 201 signals, ~5 min

### Legacy Phase 2: Flat Historical Scorer (deprecated)

The original Phase 2 at `local_runner/historical_scorer.py` targeted average signals/day instead of peak — fundamental design flaw that allowed 260 signals/day spikes. Replaced by pyramid grinder.

### Running the Grinder

**Phase 1:**
1. Load examples into Railway DB with entry dates
2. Set grind level via frontend slider (L1: 30s → L5: 8 hours)
3. Click Start Grind — desktop agent picks up job, runs spiderweb search
4. Review ceiling — the progression chart shows where adding conditions stops improving

**Phase 2 (Pyramid):**
```bash
python local_runner/pyramid_grinder.py --setup dtss --peak-target 5 --beam 200 --depth 30
```
Requires 5yr OHLCV cache + expression series cache + Railway API for examples. Runs all 6 tiers sequentially, outputs `pyramid_results_{setup}.json` + `historical_results_{setup}.json` (compatible with `signal_distribution.py`).

### What the Grinder Produces

**Pyramid output** (`pyramid_results_{setup}.json`):
- All locked conditions with tiers, expression names, categories, and ranges
- Per-tier condition counts and peak/avg stats
- Total signal count and peak/day across full history

**Compat output** (`historical_results_{setup}.json`):
- Same conditions in format compatible with `signal_distribution.py`
- Enables signal chart generation and visual verification

### What the Grinder CAN'T Do

The grinder finds the ceiling of mechanical, single-threshold conditions. It cannot capture:
- Market regime / stage transitions
- AVWAP relationships (institutional flow)
- Algo lines from high-volume candles
- Multi-day pattern shape (it sees one bar at a time)
- Net gamma effects
- Qualitative "this chart looks right" patterns

### Architecture

- `local_runner/matrix_builder.py` — Precomputes universe + example matrices. Universe build parallelized via `ProcessPoolExecutor` (8 workers, `MATRIX_WORKERS` env var configurable).
- `local_runner/spiderweb.py` — Phase 1: beam search tree exploration. Inner loop vectorized with numpy broadcasting + float32 matmul.
- `local_runner/pyramid_grinder.py` — Pyramidal grinder: 6 nested tiers (D1 → 1wk → 1mo → 6mo → 1yr → 5yr). D1 uses SpiderwebSearch, historical tiers use PeakSpiderweb (peak-based scoring). Parallel matrix build per tier via ProcessPoolExecutor. Auto-detects expression series cache for 14x speedup.
- `local_runner/historical_scorer.py` — Legacy Phase 2: greedy historical signal elimination (deprecated, kept for reference).
- `local_runner/grinder.py` — CLI interface
- `local_runner/agent.py` — Desktop polling agent with nightly auto-rebuild (triggers at 4:30pm ET)
- `local_runner/nightly.py` — **✅ BUILT:** Single command nightly update pipeline. Chains: Railway append-daily → daily cache → 5yr cache → expression cache append → matrix rebuild. Stops early if DB is already current. Run manually or auto-triggered by agent.
- `local_runner/cache_builder.py` — OHLCV caches: daily (300 bars, 57 MB) + 5yr (1,260 bars, 214 MB)
- `local_runner/expr_cache_builder.py` — **✅ BUILT:** Pre-cached expression series for all tickers × 5yr. 4,119 tickers × 12,175 expressions (4,017 daily + 80 LSP + 44 algo + 4,017 weekly + 4,017 monthly), ~50 GB compressed .npz per ticker. `--build` (first-time, ~37.5 min), `--append` (nightly, ~5-8 min), `--status`. Manifest tracks expression fingerprint for auto-invalidation.
- `local_runner/brute_expressions.py` — Expression generator: 12,175 expressions (4,017 daily + 80 LSP + 44 algo + 4,017 weekly + 4,017 monthly)
- `scripts/expression_engine.py` — Computes expressions against OHLCV
- `scripts/backtest_conditions.py` — Series computation for historical scoring. **88 ops** — full parity with expression_engine.py (excluding 8 LSP ops that require injected context). All generic expressions are available to all pyramid tiers.
- `scripts/signal_distribution.py` — Parallel signal analyzer: runs all conditions across 5yr cache, outputs daily signal counts + per-signal CSV. Used to verify peak/avg before advancing.
- `scripts/backtest_runner.py` — **✅ BUILT:** Parallel signal scan + chart generation for visual verification. Loads conditions from pyramid results, scans 5yr cache, generates dark-theme candlestick charts per signal (magenta entry marker, 8/21 EMA + 50/200 SMA). Charts organized by date folder. **Auto-uploads signals to Railway** via `POST /api/backtest/signals/upload` so frontend Historical tab updates automatically. Modes: full run, scan-only (`--no-charts`), charts-only (`--charts-only`). Outputs: `backtest_signals_{setup}.csv`, `backtest_summary_{setup}.txt`, `backtest_charts_{setup}/`, Railway upload.
- `scripts/lsp_detector.py` — Detects Local Structural Peak (highest structural high before scan bar)
- `scripts/classify_universe.py` — ETF classifier (quarterly, desktop-only, ~150 exclusions)
- `scripts/fetch_universe.py` — Universe OHLCV fetcher: full build + incremental `append_daily()` for nightly updates. Batch 40, 3s delay, INSERT OR REPLACE for dedup.
- `server.py` — Grinder API endpoints (jobs/status/progress/results/agent), nightly append-daily endpoint

### Expression library — 100% generic

The grinder uses one universal expression set for all setups. No setup-specific expressions — the pyramid grinder finds setup-specific discrimination by grinding the generic library against 5yr history.

- **Generic set** (`brute_expressions.json`) — 4,017 daily expressions across 29 categories: near_resistance (203), near_support (133), extension (98), extension_dynamics (91), extension_ceiling (40), extension_adr (6), MA slope (240), MA spread (46), spread_slope (64), slope_ratio (18), MA cross (72), MA stack (7), momentum (138), range (59), range_dynamics (13), retracement (26), swing_structure (48), gap (21), consecutive (4), candle_pattern (39), volume_character (49), volume_continuous (36), bollinger (25), macd (28), aroon (18), efficiency (9), vwap (36), percentile_rank (37), boolean (2,413 from 127 conditions × 19 aggregations each). Plus 80 LSP level expressions (precomputed by lsp_detector_v2), 44 algo line expressions (precomputed by algo_line_detector, daily-only), and 8,034 HTF copies (4,017 weekly + 4,017 monthly). Total: 12,175. Used by all setups and the universe matrix.

**Note:** `expression_engine.py` still has old LSP compute ops (`lsp_distance`, `lsp_bounce_recovery`, etc.) and `set_lsp_context()`. These are dormant — nothing calls them. They are superseded by `scripts/lsp_detector_v2.py` which produces 80 LSP expressions as precomputed series in the expression cache, and `scripts/algo_line_detector.py` which produces 44 algo line expressions (daily-only). See `EXPRESSION_ENGINE_V2.md` for the integration plan (Tasks B-G remaining).

### Expression generation constraints
- Every expression must be valid TC2000 PCF
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- Tradable universe only — `tradable_universe` table (4,167 tickers). Never Universe.txt. Never samples.

---

## Step 4: Signal Filter + Chart Vetting + Example Expansion

**Goal:** Filter grinder signals down to vettable candidates, manually review charts, expand examples, and re-grind until convergence.

**✅ COMPLETE** — Full pipeline built and working (2026-03-02).

### The Pipeline:

1. **Signal grinder** (Step 3) produces raw signals across 5yr history
2. **Signal exit grinder** runs back-to-back with signal grinder (combined into one agent step)
3. **Signal filter** (`scripts/signal_filter.py`):
   - Dedup consecutive signals (rightmost kept)
   - Apply exit condition from signal exit grinder
   - Measure signal close → exit close in ADR
   - Filter: keep only signals ≥ example floor ADR
   - Exclude signals matching existing examples (±5 day window)
   - Rank by ADR descending
   - Output: `data/signal_filter/filtered_dtss.json`

4. **Chart vetting** (unified UI, step 4):
   - Embedded candlestick chart with EMA 8/21, SMA 50/200
   - Earnings overlay (purple E markers from Yahoo Finance)
   - 250-bar lookback + 80-bar forward, all bars visible
   - YES = create example in Railway DB (click entry candle first)
   - NO = create rejected signal in `rejected_signals` table
   - MAYBE = flag for review
   - Keyboard shortcuts: ↑↓ navigate, 1/2/3 for yes/maybe/no
   - Auto-advances to next unvetted signal after verdict

5. **AI review** ("Submit for Audit" — when all signals vetted):
   - Runs `claude -p` via agent for each YES/NO decision
   - Compares against existing examples + setup definition from ta_knowledge.md
   - Flags disagreements for human review
   - Must pass before re-grind (NOT YET BUILT)

6. **Re-grind** with expanded examples:
   - More examples → tighter conditions → fewer signals → less noise
   - Repeat from step 1 (signal grinder)
   - Convergence: when grind produces signals and all good ones already in example set

### First Vetting Pass Results (2026-03-02, DTSS):
- Input: 100 filtered signals from 23-example grind
- Output: 14 new examples (YES), 8 rejected (NO), 1 later removed
- Result: 36 total examples, ready for re-grind

### Deduplication Rules:
- Same ticker, consecutive signal bars (no gap) = one signal, keep rightmost
- Any gap (even 1 bar) = separate signal
- Applied identically to examples and backtest signals

### Exit Distance Measurement:
- Rightmost deduped signal bar close → exit bar close, in ADR units
- Same method for examples and backtest signals
- Example floor ADR = minimum across all example measurements
- Backtest signals must meet or exceed floor

## Step 6: Exit Management Grind — How Do the Examples Resolve?

**Goal:** Find the optimal TA-driven exit conditions on the validated examples' post-entry bars. No fixed bar counts, no arbitrary targets — the TA tells us when the move is done.

**This step MUST come before the outcome grind (Step 7) because it defines what "the move played out" looks like.**

### How It Works

- **Input:** The validated examples (current DTSS: 23) with entry dates
- **Data:** Post-entry OHLCV bars for each example, pulled from the 5yr cache. Open-ended — span enough bars to encompass all behavior (let the data tell us, not a predefined number).
- **Method:** Brute force a comprehensive post-signal expression library (~4,000 expressions) against the forward price paths. The grinder finds which expression states correlate with the bars that captured the most move. The "exit candle" isn't an input — it's the output.
- **Benchmark:** Entry bar high to exit bar close = captured move, in ADR. Simple, consistent, dependable.

### Post-Signal Exit Expression Library (~6,400 expressions)

Every expression is evaluated at each forward bar relative to the signal bar. The grinder tests every bar as a candidate exit.

| Category | Count | What it measures |
|----------|-------|-----------------|
| **extension_dynamics** | 70 | Extension slope (1/3/5 bar), retrace from post-signal peak, acceleration — all 5 MAs × 2 norms |
| **momentum_reversal** | 56 | RSI (5/7/9/14/21) + slopes, ROC (1-20), MACD histogram + slope, stochastic, ADX + DI spread |
| **extension_from_ma** | 50 | Extension from 8/12/21 EMA, 50/200 SMA in ADR/ATR + historical ceiling ratios per ticker |
| **ma_reclaim** | 40 | Close above MAs, bars since reclaim, failed reclaims, distance from MA, sequential reclaim pairs |
| **entry_relative** | 39 | Delta from entry (extension, RSI, ADX, stoch, BB %B, MA dist) + ratio to entry (RVOL, BB bw, LSP/algo/AVWAP dist) |
| **candle_character** | 33 | Bar range/body/wick ratios, gaps, rolling green/red % over 3/5/10/20 bars, streak counts |
| **structural** | 28 | MA touches/closes-through, swing counts, lower-low sequences, higher-low formation |
| **volume_character** | 26 | RVOL vs 10/20/50 avg, up/down volume ratios, OBV slope, volume vs signal bar, volume rank |
| **range_compression** | 25 | ATR ratio vs entry, Bollinger bandwidth + %B + rank, inside bar counts, range contraction |
| **algo_lines** | 20 | Distance/broken/touch_count to H-/L+ algo lines (rank 1-3), shallowest unbroken line dist/slope |
| **lsp_structure** | 17 | Distance to LSP above/below (rank 1-3), broken, congestion, nearest unbroken level |
| **move_captured** | 11 | Distance from entry high to current close/low in ADR/ATR/%, MFE, capture efficiency |
| **retracement** | 10 | Retrace from MFE in ADR/%/ATR, position in post-signal range, bars since MFE, MFE expanding |
| **avwap** | 9 | LSP-anchored AVWAP distance (above/below × rank 1-2), entry AVWAP distance, AVWAP slope, AVWAP crossed |
| **time** | 6 | Bars since signal, move per bar, velocity accelerating/decelerating |
| **relative_strength** | 6 | Stock vs SPY performance + slope over 5/10/20 bars |

**446 unique per-bar expressions** → expanded by:
- **~213 boolean conditions** (59 native + 154 threshold) × 4 aggregations × 7 windows → ~5,964 boolean expressions

**Total: 6,410 post-signal expressions.**

**Entry-relative expressions** are critical: `delta_from_entry` and `ratio_to_entry` ops let the grinder find conditions like "RSI rose 20 from entry" or "extension retraced 1.5 ADR from entry" — conditions that adapt to each stock's starting state rather than requiring one absolute threshold across all examples.

**Structural detection systems (LSP, algo lines, AVWAPs)** are frozen at entry time. Levels are detected once from pre-entry history, then price is evaluated against those fixed levels at each forward bar. This is the same computation path used by the signal grinder's expression cache builder.

### Current Single-Stage Result (2026-03-01)

Best exit: `avg_range_atr_10b above 1.0541` — 71% median capture efficiency, 64% avg, 20/20 examples pass.

**Finding:** Single-condition exit hits a ceiling. Structural expressions compute correctly but cannot beat simple volatility expansion as a standalone universal trigger. Multi-stage exit architecture is needed — see Task 3.7 in TODO.md.

### Scoring System — Reliability Over Max Extraction

**Design for a managed account:** consistent, reliable exits that turn over capital. Not max-profit prayer trades.

**Measurement unit:** Capture efficiency = captured move (ADR) / MFE (ADR) per example. Normalizes across different-sized moves so a 3 ADR capture on a 4 ADR move (75%) scores the same as 9 ADR on 12 ADR (75%).

**Scoring hierarchy for ranking exit conditions:**

1. **Primary: Floor capture efficiency** — the WORST example's capture efficiency across all examples. If the floor is high, the exit works on every trade. This is the "sleep at night" metric.
2. **Secondary: Median capture efficiency** — typical outcome. Tiebreaker between conditions with similar floors.
3. **Hard constraint: every example must capture > 0 ADR.** If any example loses money under an exit condition, that condition is eliminated. Zero tolerance.

**Why not average/max?** With ~23 examples you can't trust the tails. A 15 ADR monster outlier skews the average but its real prevalence is unknown. Floor and median are honest with small samples — they tell you what reliably happens. As the example library grows (50, 100+), the tails become trustworthy and more aggressive extraction becomes statistically justified.

**Plateau detection:** Find regions where many nearby exit conditions all produce similar high-floor scores. Plateaus = robust zones where small parameter changes don't break the system. Same principle as the signal grinder's peak-based plateau detection.

### Process

1. Pull post-entry OHLCV for each example from 5yr cache (open-ended forward window)
2. Compute MFE per example (lowest low for shorts — the theoretical max captured move)
3. At each forward bar, compute all ~4,032 post-signal expressions
4. For each expression + threshold combination, identify which bar would be the exit bar (first bar where condition triggers)
5. Measure captured move (entry high → exit bar close) and capture efficiency (captured / MFE)
6. Score: rank by floor capture efficiency, break ties with median
7. Plateau detection: find robust parameter regions
8. Output: the exit conditions that mark "this move is done"

### Output

**Exit conditions** — the TA expression states that reliably mark "the move is done" with the highest floor capture efficiency across all examples. These become the base filter for Step 7: a Step 3 signal only counts as an outcome signal if these exit conditions eventually triggered on its post-signal bars.

**Exit scoring report** — per-example breakdown showing captured move, MFE, efficiency, and which bar triggered the exit. Visual verification that the exits make TA sense.

### Scales With Examples

This step is designed to be re-run as examples grow. At 23 examples, floor/median scoring finds reliable-but-conservative exits. At 50+, the floor metric has more resolution and can accept tighter conditions. At 150+, tail behavior is well-characterized and the system can discover more aggressive extraction strategies that still maintain high floor scores. The scoring math adapts automatically — no manual tuning needed.

### Script: `scripts/exit_grinder.py` (NEW)

---

## Step 7: Outcome Grind — Which Signals Are Clean, Tradeable Moves?

**Goal:** Split the Step 3 signals into OUTCOME signals (clean, tradeable moves worth being in) and non-outcome signals (junk — didn't go far enough, choppy, untradeable). Uses a base filter followed by a brute force expression matrix grind against the signal-to-exit segment.

**Input requirements:**
- Step 3 signal results (all signals with ticker + signal bar date)
- Step 6 exit conditions + exit grind results
- Validated examples with entry dates

### Phase 0: Example Signal Bars — ✅ HANDLED BY PYRAMID

The pyramid grinder already outputs `example_signals` with signal bar dates for all examples. Phase 0 is no longer a separate step — the signal bar for each example is the bar where all conditions first fire leading up to the entry date. These are included in the pyramid's 576 total signals, tagged with `is_example: true`.

**Key insight from build:** Signal conditions can fire on multiple consecutive bars for the same ticker. The 576 signals are not 576 unique trade opportunities — they're 576 "watchlist appearances" where some tickers appear multiple consecutive days. This is fine for outcome analysis (each signal bar gets its own forward window) and doesn't need deduplication.

### Phase 1: Base Filter — Exit Triggered + Minimum Move — ✅ BUILT

Apply Step 6 exit conditions to all signals' post-signal bars. Two requirements to pass:

1. **Exit condition triggers** on the forward bars
2. **Minimum move threshold:** Exit bar close must be ≥ 1 ADR below signal bar close (shorts) or ≥ 1 ADR above signal bar close (longs). Uses ADR14 at the entry bar for normalization.

Both must be true. This is a simple, fast filter that trims the obvious garbage — signals where the exit technically triggered but the move was negligible, or the stock just chopped around the signal level without going anywhere meaningful.

**Implementation:** `scripts/outcome_grinder.py` — fully local, no API calls. Reads pyramid signal grind + exit grind from `local_runner/grinds/{setup}/` via GrindStorage. Uses local 5yr OHLCV cache. Batch-processes signals grouped by ticker (one ADX computation per ticker). All date handling via positional iloc indexing — never calendar day arithmetic. ProcessPoolExecutor with all CPU cores.

**Initial DTSS results (2026-02-26, needs investigation):**
- 576 total signals → 187 outcomes (32.7%), 373 sub-ADR, 12 no trigger, 4 errors
- 3 examples failed: WING (0.96 ADR, barely missed), ZIM (-1.78 ADR, wrong direction), FTAI (no trigger in 120 bars)
- Outcome stats: median +12% move, median 3.5 ADR, median 0.845 capture efficiency
- ⚠️ Results need investigation — 3 missing examples suggests exit condition or measurement issues

Signals passing both = **candidate outcome signals** for Phase 2.
Signals failing either = eliminated before the expensive grind.

### Phase 2: Outcome Expression Grinder

Brute force expression matrix comparing validated examples vs all signals across their **signal-to-exit segments**. This is the core of Step 7.

- **Positives (examples):** Validated examples, measured from their signal bar (found in Phase 0) forward through exit
- **Universe:** All Step 3 signals, measured from signal bar forward through exit
- **Expression library:** Generic **segment expressions** — characterize the quality of the move from signal to exit. NOT the same library as Step 3 (pre-signal snapshot) or Step 6 (exit detection at each bar). This library analyzes ranges of bars as a segment. Designed to work across all setup types — shorts and longs.
- **Method:** Same pyramid grinder pattern — build matrices, grind for conditions that separate real runners from noise

**What Phase 2 is asking:** "Was this a clean, tradeable move?" A real runner from signal to exit looks fundamentally different from a choppy mess that technically triggered the exit condition and passed the 1 ADR threshold. The grinder finds what separates them.

### Outcome Segment Expression Library

**This is a distinct expression library from Step 3 and Step 6.** It analyzes the entire signal-to-exit segment as a unit, not individual bars.

Expressions are generic, normalized (ADR/ATR/%), and work across any setup type. Categories informed by ta_knowledge.md — to be designed after reading ta_knowledge.md.

**Key design principle:** These expressions characterize *move quality and tradeability* across a range of bars. They answer: "how did price get from signal to exit?" Not "what does the chart look like at one point in time" (Step 3) or "is the move done yet" (Step 6).

Categories TBD — will be built after reading ta_knowledge.md. Must capture move conviction, velocity, structural behavior, and tradeability without being bespoke to any single setup.

### Output

**OUTCOME SIGNALS** — the subset of Step 3 signals where:
1. The exit condition triggered (Phase 1)
2. The move reached at least 1 ADR from signal bar close (Phase 1)
3. The signal-to-exit segment matches example behavior (Phase 2)

These are confirmed clean, tradeable moves.

### Assembly Note

Phase 0, Phase 1, and Phase 2 are built as separate blocks/scripts first, then chained together in the final outcome grinder pipeline. Each block is independently testable and re-runnable.

### Script: `scripts/outcome_grinder.py`

**Usage:** `python scripts/outcome_grinder.py --setup dtss` (auto-finds signal/exit from grinds storage)

**Override:** `python scripts/outcome_grinder.py --setup dtss --pyramid path/to/signal.json --exit-grind path/to/exit.json`

**Output:** Saves to `local_runner/grinds/{setup}/outcome/` via GrindStorage.

---

## Step 8: Pre-Signal Refinement Grind — Did Step 3 Miss Anything?

**Goal:** Check if there are pre-signal (scan bar) conditions that distinguish outcome signals from non-outcome signals — conditions that Step 3 couldn't find because it was comparing examples vs the entire 4,000 ticker universe instead of comparing within the signal set.

### How It Works

- **Universe:** All Step 3 signals (the full signal set)
- **Examples:** OUTCOME signals from Step 7
- **Expressions:** The existing 12,175 pre-signal expression library (same as Step 3)
- **Method:** Standard pyramid grinder. Outcome signals as examples, all signals as universe. Find any conditions that predict winners *before the move even starts*.

### Why This Can Find Things Step 3 Missed

Step 3 compared 23 examples vs ~4,000 tickers. That's a very different discrimination problem than comparing outcome signals vs non-outcome signals within an already-filtered set of ~200 signals. A subtle pre-signal tell that's invisible when comparing against the full market may become obvious when comparing within the signal set.

### What Happens With Results

Any new conditions found get **added to Step 3's condition set**. Rerun Step 3 with the expanded conditions to produce **TOTAL SIGNALS**.

**Critical property: TOTAL SIGNALS ≤ original signals, but OUTCOME SIGNALS don't change.** The new conditions only eliminate signals that were non-outcome anyway — the outcome signals pass by definition because they share those conditions with the examples. So the win rate goes up (fewer losers, same winners) without needing to re-run Steps 6-7.

### Output

- **TOTAL SIGNALS** — the tightened signal set (Step 3 conditions + Step 8 conditions)
- **OUTCOME SIGNALS** — unchanged from Step 7
- These two sets are the inputs to Step 9

### Script: `scripts/presignal_grinder.py` (NEW)

---

## Step 9: Environment Clustering — When Does This Setup Work Best?

**Goal:** Find the market environments where the setup produces the highest win rate on quality moves.

### The Math

- **Win rate** = OUTCOME SIGNALS ÷ TOTAL SIGNALS, computed per market environment
- This is win rate on **quality moves only** — signals that ran like the examples. Small wins and scratches are NOT counted as wins. So the win rate will be lower than a traditional backtest but represents real, tradeable runner probability.
- **EV** = (win rate × average captured distance under optimal exit) − (loss rate × average loss)
  - Win rate comes from this step
  - Average captured distance comes from Step 6 exit management
  - Average loss comes from analyzing non-outcome signals (potential Step 10 refinement)

### Process

1. **Compute market context at each signal** — SPY regime (above/below key MAs, extension, trend), breadth metrics, signal clustering density, VIX level, sector rotation state, etc.
2. **Split signals by environment** — for each market context factor, bucket TOTAL SIGNALS into quantile groups
3. **Compute win rate per bucket** — OUTCOME SIGNALS in bucket ÷ TOTAL SIGNALS in bucket
4. **Find the high-EV environments** — which market conditions produce the highest concentration of outcome signals?
5. **Optionally: check if optimal exit varies by environment** — from Step 6, does the best exit strategy differ in bear vs neutral vs bull markets?

### Output

**Environment scoring model** — given tonight's market context, what's the expected win rate and EV? This feeds into the presentation system for nightly signal ranking.

### Script: `scripts/environment_scorer.py` (NEW)

---

## Future: Loss Reduction (Step 10, optional)

**Goal:** Analyze non-outcome signals for common early post-signal behavior that predicts failure. If there's a consistent tell in the first bar or two after the signal fires, add near-entry management rules to cut losses faster and improve the loss side of EV.

**Input:** Non-outcome signals from Step 7 (signals where the move didn't play out)
**Method:** Profile the first 1-5 bars after each non-outcome signal. Look for common patterns — gap ups, immediate reclaim of a key level, volume failure, etc.
**Output:** Early management rules that reduce average loss size without affecting winners.

This is optional and may not be needed if Steps 6-9 already produce strong EV.

---

## Summary

| Step | What | Output |
|------|------|--------|
| 1 | **Load** | Data & TA knowledge — everything is already in the system |
| 2 | **Receive** | User presents examples, entry dates, and setup context |
| 3 | **Signal Grind** | THE GRINDER — pyramid grinder finds pre-signal conditions. Output: **SIGNALS** (~201/5yr for DTSS) |
| 4 | **Backtest** | Visual verification of signals across history |
| 5 | **Backtest Runner** | Charts + Railway upload + Historical tab |
| 6 | **Exit Management Grind** | ~4,000 post-signal expressions grind against examples' forward paths. Scored by floor capture efficiency (worst example). Output: **EXIT CONDITIONS** |
| 7 | **Outcome Grind** | Phase 0: find example signal bars. Phase 1: exit filter + 1 ADR minimum move. Phase 2: segment expression grind. Output: **OUTCOME SIGNALS** |
| 8 | **Pre-Signal Refinement** | Grind outcome vs non-outcome on pre-signal expressions. New conditions added to Step 3. Output: **TOTAL SIGNALS** (tighter, same outcome signals) |
| 9 | **Environment Clustering** | OUTCOME SIGNALS ÷ TOTAL SIGNALS by market regime. Output: **EV per environment** |

**The output:** For any setup, the system produces: signal conditions (when to watch) × exit conditions (how the move resolves) × environment scoring (when it works best) = **EV**.

**What the system does NOT do:** Entry. That's discretionary TA — the trader's skill and edge.

**Re-run on example growth:** Steps 3-9 re-run as examples are added from Step 4 backtest review. More examples → tighter signal grind, more confident exit conditions (floor metric gains resolution), sharper outcome/environment models. The system's output quality scales directly with example count. All scoring uses relative metrics (floor, median, percentiles) that adapt automatically.

**Presentation:** A separate PRESENTATION_SYSTEM handles nightly data updates, signal detection, and rank-ordered EV presentation. That system consumes the outputs of this analysis system.

---

## Grind Storage System

All grinder outputs are managed by `local_runner/grind_storage.py`. Standardized structure, Windows-compatible (copies, not symlinks).

### Structure
```
local_runner/grinds/{setup}/{step}/{step}_{timestamp}.json
local_runner/grinds/{setup}/{step}/latest.json  (copy of newest)
```

Steps: `signal`, `exit`, `outcome`, `market`

### Usage
```python
from local_runner.grind_storage import GrindStorage
gs = GrindStorage("dtss")

gs.save("signal", data)          # auto-timestamps, updates latest.json
data = gs.load("signal")         # loads latest.json
data = gs.load("signal", "20260226_104240")  # specific run
runs = gs.list_runs("signal")    # newest first
print(gs.summary())              # all steps status
```

### Grinder integration
Each grinder saves via `gs.save()` and loads upstream dependencies via `gs.load()`. CLI flags (e.g. `--pyramid`, `--exit-grind`) override the default latest.json lookup.

### Support files (NOT in grinds/)
These stay in `local_runner/cache/`:
- `brute_expressions.json` — expression library
- `classification.json` — ETF classifier
- `expr_series/` — expression series cache (~21 GB)
- `universe_ohlcv_5yr.pkl` / `universe_ohlcv.pkl` — OHLCV caches

---

## Reference

- **URL:** web-production-e3025.up.railway.app
- **Key API endpoints:**
  - `GET /api/setups` — list setup types
  - `GET /api/examples/{type}` — list examples for a setup
  - `GET /api/conditions/{type}` — get conditions for a setup
  - `GET /api/ohlcv/local/{type}/{id}` — OHLCV data for an example
  - `GET /api/extension-data/{type}/{id}` — extension analysis
  - `GET /api/tradable` — tradable universe
  - `GET /api/universe/status` — universe data status
  - `GET /api/backtest/summary` — backtest results summary
  - `POST /api/backtest/run` — run backtest
  - `POST /api/backtest/signals/upload` — desktop runner uploads signals per setup_type (replaces existing)
  - `GET /api/backtest/signals/{setup_type}` — get signals for Historical tab (per-setup)
  - `GET /api/chart-image/{type}/{id}` — chart image
  - `GET /docs` — full Swagger API docs
  - **Nightly endpoints:**
  - `POST /api/universe/append-daily` — incremental OHLCV update (checks DB max date vs yfinance latest trading day, fetches only missing days, rebuilds tradable_universe)
  - **Grinder endpoints:**
  - `POST /api/grinder/start` — submit a grind job (setup_type, grind_level)
  - `GET /api/grinder/jobs/pending` — pending jobs for agent pickup
  - `POST /api/grinder/status` — agent posts job status updates
  - `POST /api/grinder/progress` — agent posts progress updates
  - `GET /api/grinder/progress/{job_id}` — frontend polls progress
  - `GET /api/grinder/results/{setup_type}` — get latest results
  - `GET /api/grinder/agent/status` — check if desktop agent is online
  - `POST /api/grinder/agent/heartbeat` — agent heartbeat
- **Infrastructure:** SQLite on Railway persistent volume (/app/data)
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status, ticker_classification, universe_exclusions, backtest_signals
