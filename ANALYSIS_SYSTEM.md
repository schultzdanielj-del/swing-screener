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

**Best result (2026-03-03, grind #4, 62 examples, 59 resolved):**
- 86 conditions (69 daily, 12 weekly, 5 monthly), peak 4/day (1mo) / 11/day (5yr), 168 signals
- Runtime: 14.1 min with expression cache
- Previous: grind #3 (48 examples): ~76 conditions, ~200 signals
- Previous: grind #2 (35 examples): 53 conditions, 91 signals
- Previous: grind #1 (20 examples): 41 conditions, 264 signals, peak 3/day

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

- **Extension structure expressions** (`extension_structure`) — **PLANNED, ~3,630 new expressions.** Treats `ext_avgc50_adr14` and `ext_avgc200_adr14` as price-like inputs and runs the full price-structure expression suite against them across daily + weekly + monthly timeframes. Categories: slope, ROC, ROC delta, ROC acceleration, RSI + RSI slope, stochastic, CCI, ADX + ADX slope, range position, pullback, floor ratio, peak/ceiling ratio (HTF only), trendline deviation, channel position, Bollinger %B, smoothed MA, MA cross, boolean aggregations. No volume ops — extension series has no volume structure. Implementation requires: (1) `on_series` op handler in `backtest_conditions.py`, (2) `extension_structure` generator block in `brute_expressions.py`, (3) second-pass computation in `expr_cache_builder.py`. Cache goes 12,421 → ~16,051 (~65 GB). See TODO.md for full breakdown.

**Note:** `expression_engine.py` still has old LSP compute ops (`lsp_distance`, `lsp_bounce_recovery`, etc.) and `set_lsp_context()`. These are dormant — nothing calls them. They are superseded by `scripts/lsp_detector_v2.py` which produces 80 LSP expressions as precomputed series in the expression cache, and `scripts/algo_line_detector.py` which produces 44 algo line expressions (daily-only). See `EXPRESSION_ENGINE_V2.md` for the integration plan (Tasks B-G remaining).

### Expression generation constraints
- Every expression must be valid TC2000 PCF
- ATR = `ATR14` (not AVGT14, AVG14, or AVGT)
- EMA = `XAVGC` (not EAVG or XAVG). e.g. EMA21 = `XAVGC21`
- All thresholds normalized to ATR, ADR, or % — no fixed dollar amounts
- Tradable universe only — `tradable_universe` table (4,167 tickers). Never Universe.txt. Never samples.

---

## Step 4: Sample Expansion (Signal Filter + Chart Vetting + Example Expansion)

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

4. **Chart vetting** (unified UI, step 3 / Sample Expansion):
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

---

## Step 4a: Exit Grinder

**Goal:** Find the optimal TA-driven exit conditions from the ENTRY BAR HIGH across all validated examples. Produces both a single-stage and multi-stage exit. User reviews results and chooses which to carry into 4b.

**UI step:** `setup_grinder_a` — runs both grinders sequentially, then presents results side by side for review and choice.

### Single-Stage Exit (`scripts/profit_grinder.py`)

Brute forces ~12K expressions against validated examples' forward price paths. Benchmarks from entry bar high to exit bar close in ADR. Output: best single exit condition + per-example exit dates (used by blackout filter).

- Writes to: `data/profit_grind/profit_{setup}.json`
- Uploads to Railway via `POST /api/profit-grind/{setup_type}/upload`

### Multi-Stage Exit (`scripts/multistage_exit_grinder.py`)

Tests all structural variants (1–N stage configurations with trim percentages, gates, and exit conditions per stage). All CPU cores, 100% example pass rate required, no backstops.

- Writes to: `data/multistage_exit/ms_exit_{setup}.json`
- Uploads to Railway via `POST /api/exit-grind/{setup_type}/upload-multistage`

### Choosing an Exit

Frontend (`ExitGrinderPage`) displays single vs multi side by side:
- Single: expression, threshold, floor/median capture %, floor/median ADR, avg bars to exit
- Multi: n_stages, capture stats, stage-by-stage breakdown (trim%, gate, condition, max window)

User clicks "Use Single-Stage" or "Use Multi-Stage" → POST to `/api/exit-grind/{setup}/choose` → unlocks 4b.

**Current DTSS result (as of 2026-03-01):**
- Single: `slope_xavgc21_off7_adr14 <= -1.126631`, ~71% median capture
- Multi: see latest run output

### API Endpoints
- `GET /api/exit-grind/{setup}/results` — single + multi results + current choice
- `POST /api/exit-grind/{setup}/choose` — store choice (`"single"` or `"multi"`)
- `GET /api/exit-grind/{setup}/choice` — get current choice (404 if none)
- `POST /api/exit-grind/{setup}/clear-choice` — clear choice, re-locks 4b

---

## Step 4b: Setup Grinder (Blackout Re-Grind + Condition Pruning)

**Goal:** Re-grind the signal pyramid with post-entry bars masked (blackout filter), then prune low-power conditions and produce a clean signal set for the next vetting pass.

**UI step:** `setup_grinder_b` — locked until a choice is made in 4a. Runs blackout pyramid + setup_refiner sequentially.

### Sub-steps (agent runs in order):

**1. Blackout Re-Grind** (`pyramid_grinder.py --blackout`)

`matrix_builder.py` loads `data/profit_grind/profit_{setup}.json` (per-example exit dates) and masks entry→exit bars per ticker. Universe rows in that window are excluded from the pyramid grinder's universe matrix. This prevents the grinder from "seeing" post-entry price action and building conditions that only fire because a stock already moved.

Re-runs pyramid grinder on the cleaned universe. Same params: beam=10000, depth=100, peak=3.

Output: `cache/pyramid_results_{setup}_blackout.json`

**2. Condition Pruning + Signal Filter** (`scripts/setup_refiner.py`)

- Leave-one-out on every condition in the blackout grind result
- Measures filter power per condition: % of remaining universe eliminated
- Drops conditions below ~10–15% threshold (low-power conditions add overfitting risk without discriminating)
- Runs signal filter on pruned conditions
- Reads exit choice from Railway (`GET /api/exit-grind/{setup}/choice`) → routes to single or multi exit
- For multi-stage: uses first stage as effective exit for signal measurement

Output: `data/setup_refiner/refined_{setup}.json`

Uploads cleaned signal set to Railway for the next vetting pass (Step 3 / Sample Expansion).

### Convergence Condition

When the blackout re-grind produces signals that are all already in the example set, the loop is done. No new examples → no re-grind needed → advance to Step 5.

---

## Step 5: Market Grinder — What Market Conditions Favor This Setup?

**Goal:** Find the market regimes where this setup's signals have the highest win rate. Given tonight's market conditions, output an expected win rate so you know whether to trust tonight's signals.

**Why this matters:** The same setup may fire in both favorable and unfavorable conditions. The market grinder finds which conditions predict winners vs losers, letting you skip signals in bad regimes and size up in good ones.

### Winner / Loser Classification

Every deduped signal from Step 4b gets classified as winner or loser using all available data. Vetting verdicts override auto-classification. The rule is simple:

| Signal | Classification |
|--------|---------------|
| Setup examples (confirmed by human vetting) | **AUTO YES — always winner** |
| Exit-triggered signals (exit condition fired within window) | **AUTO YES — winner** |
| Exit-triggered signals manually marked NO in Step 4 viewer | **NO — loser (override)** |
| No-exit signals (exit never triggered within window) | **AUTO NO — loser** |

**The override mechanism:** If you vet an exit-triggered signal and it's a bad/no-win setup — chop, extended bear trend, untradeable — mark it NO. It moves to the loser pile regardless of the exit trigger. This cleans up false winners in the regime model.

**Key properties:**
- The system works with zero manual vetting — exit triggered / no-exit is a good-enough proxy at any scale
- Every NO verdict on an exit-triggered signal improves the model — more vetting = cleaner populations = better regime signal
- Partial vetting is automatically better than no vetting
- Works identically whether a setup has 132 signals or 500+ signals

**Why this scales:** A setup with 500 signals across 5yr doesn't need full manual vetting. Auto-classification handles all 500 and produces a valid regime model. For large setups, the manual vetting pass is optional quality improvement, not a prerequisite.

**Alignment with the sample expansion loop:** Steps 2-4 can be re-run as many times as needed to grow the example library. Each re-grind tightens conditions and produces a new signal set. The market grinder always operates on the latest signal set — re-running it after a re-grind automatically picks up improved classifications from expanded examples.

### Step 4 Signal Viewer (prerequisite UI)

Before running the market grinder, the vetting UI needs a source toggle so all Step 4b signals can be browsed and optionally vetted.

**The Sample Expansion page (VettingPage) gets a Step 2 / Step 4 toggle:**

- **Step 2 mode** (default): reads from `data/signal_filter/filtered_{setup}.json` — the ADR-filtered signals. Full YES/NO/MAYBE vetting enabled.
- **Step 4 mode**: reads from `data/setup_refiner/refined_{setup}.json` — all deduped signals including those with no exit trigger. YES/NO verdicts enabled (NO overrides feed Market Grinder classification). MAYBE disabled (not meaningful here).

In Step 4 mode, signals with no exit trigger are visually distinguished in the list (dim color + "no exit" tag). The chart and navigation work identically.

**New server endpoint required:**
- `GET /api/setup-grinder/{setup_type}/signals` — reads `data/setup_refiner/refined_{setup_type}.json`, returns all signals with exit status. Attaches any existing vetting verdicts.

### Market Regime Indicators

At each signal date, compute SPY conditions using the 5yr OHLCV cache and expression cache:

**Price/trend:**
- SPY close vs SMA50, SMA200 (above/below)
- SPY EMA8 vs EMA21 vs SMA50 (MA stack state)
- SPY extension from SMA50 and SMA200 in ATR
- SPY SMA50 slope (rising/flat/falling)
- SPY SMA200 slope

**Momentum:**
- SPY RSI(14) — overbought/oversold/neutral
- SPY ROC(20) — 1-month momentum
- SPY % from 52-week high

**Volatility:**
- SPY ATR14 relative to its own 50-day average (expanding/contracting)
- SPY 20-day realized volatility

**Regime buckets:** Each indicator is bucketed into 3-5 quantile groups (e.g. SPY RSI: <40 / 40-60 / >60). Win rate is computed per bucket. Buckets with fewer than ~5 signals are flagged as low-confidence.

### Grinder Method

For each regime indicator + bucket combination:
1. Count WINNER signals in bucket
2. Count total signals in bucket
3. Win rate = winners / total
4. Compare to overall baseline win rate (winners / all signals)
5. Score = win rate lift vs baseline

Find combinations of regime conditions (2-3 at most) that produce the highest win rate lift with enough signals to be statistically meaningful.

**Important:** This is a discriminative analysis, not a brute-force grind. The expression space is small (20-30 SPY indicators vs 12,175 ticker expressions). Exhaustive search over all pairs and triples is fast and doesn't require beam search.

### Output

**Per signal date:**
- All regime indicator values at that date
- Winner/loser classification
- Source (example / exit-triggered / no-exit)
- Manual verdict if applied

**Regime model:**
- Which indicator combinations predict high win rates
- Win rate per regime bucket (with confidence intervals based on sample count)
- Overall baseline win rate

**Live prediction (current market):**
- Compute all regime indicators for today's SPY bar
- Output: expected win rate given current conditions
- This feeds into the nightly watchlist — signals get a regime-adjusted win rate displayed alongside them

### Script: `scripts/market_grinder.py` ✅ COMPLETE

**Method (actual implementation):**
- Fetches signals + classifications from Railway API for the current cycle
- Builds a daily win rate time series: rolling ±5 trading day window, density-weighted by signal count per window
- Loads market cache (245 instruments × 15,805 expressions, ~4 GB) built by `local_runner/market_cache_builder.py`
- Computes weighted Pearson correlation of each (instrument × expression) time series vs win rate series — parallelized across all CPU cores via ProcessPoolExecutor
- Min coverage filter: feature must have valid values on ≥20% of series days (prevents sparse extension_structure expressions from dominating)
- Feature deduplication: greedy selection ranked by |corr|, skips any candidate with inter-corr ≥0.95 with already-selected features — ensures 50 genuinely independent signals
- Computes quartile win rates per feature, composite regime score 0-1 per signal
- Uploads regime model + per-signal scores to Railway

**Run:**
```bash
python scripts/market_grinder.py --setup dtss --dry-run   # validate first
python scripts/market_grinder.py --setup dtss              # upload live
```

**Output:** Regime model + 1111 signal scores uploaded to Railway via:
- `POST /api/market-grind/{setup_type}/upload`
- `POST /api/v2/cycles/{cycle_id}/signal-scores`

### UI (Market Grinder Page)

Replace the "coming soon" placeholder on Step 5 with:
- RUN button (triggers agent)
- Results display: regime model table showing win rate per bucket
- Current market indicator: "Today's regime → expected win rate: X%"
- Per-signal classification table (ticker, date, winner/loser, source)

---

## Step 6: Entry Bar Grinder — When Exactly To Pull The Trigger

**Goal:** Find conditions that identify the EXACT entry bar across all examples. The signal grinder (Step 3) finds "this setup exists somewhere nearby." The entry bar grinder finds "enter TODAY."

**Why this is critical:** With 10 setups running, you can't have 50 potentials on a watchlist all waiting 3-5 days to maybe fire. Need: scan fires tonight → enter tomorrow morning. One bar precision.

### How It Works

- **Examples (positives):** The entry bar itself for each of the 62 examples. Not the bar before. Not a window. THE bar.
- **Universe (negatives):** The 168 grinder signals (all bars that passed signal conditions). Most of these are NOT entry bars — they're bars where the setup conditions aligned but the actual entry trigger hasn't fired yet.
- **Method:** Same pyramid grinder architecture. Find conditions true on entry bars that are NOT true on other signal bars. The grinder separates "entry is now" from "setup exists but wait."
- **Expression library:** Same 12,175 expressions evaluated on the entry bar itself.

### Output

Additional filter conditions that, applied on top of the 86 signal conditions, identify the entry bar. The subset of 168 signals that pass BOTH the signal conditions AND the entry bar conditions = "enter tomorrow morning."

### Validation

After applying entry bar conditions to signals:
- Check: does each example's entry bar still pass? (must be 100%)
- Check: for the non-example signals that pass, does an entry-quality bar actually occur?
- The filtered signal count should be much smaller than 168 — these are actionable entries, not watchlist items

### Script: `local_runner/entry_bar_grinder.py` (NEW — to build)

---

## Step 7: Exit Management Grind — How Do the Examples Resolve?

**Goal:** Find the optimal TA-driven exit conditions on the validated examples' post-entry bars. No fixed bar counts, no arbitrary targets — the TA tells us when the move is done.

**This step MUST come before the outcome grind (Step 8) because it defines what "the move played out" looks like.**

### How It Works

- **Input:** The validated examples (current DTSS: 23) with entry dates
- **Data:** Post-entry OHLCV bars for each example, pulled from the 5yr cache. Open-ended — span enough bars to encompass all behavior (let the data tell us, not a predefined number).
- **Method:** Brute force a comprehensive post-signal expression library (~4,000 expressions) against the forward price paths. The grinder finds which expression states correlate with the bars that captured the most move. The "exit candle" isn't an input — it's the output.
- **Benchmark:** Entry bar high to exit bar close = captured move, in ADR. Simple, consistent, dependable.

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

### Script: `scripts/exit_grinder.py` (NEW)

---

## Step 8: Outcome Grind — Which Signals Are Clean, Tradeable Moves?

**Goal:** Split the Step 3 signals into OUTCOME signals (clean, tradeable moves worth being in) and non-outcome signals (junk — didn't go far enough, choppy, untradeable). Uses a base filter followed by a brute force expression matrix grind against the signal-to-exit segment.

**Input requirements:**
- Step 3 signal results (all signals with ticker + signal bar date)
- Step 6 exit conditions + exit grind results
- Validated examples with entry dates

### Phase 1: Base Filter — Exit Triggered + Minimum Move

Apply Step 6 exit conditions to all signals' post-signal bars. Two requirements to pass:

1. **Exit condition triggers** on the forward bars
2. **Minimum move threshold:** Exit bar close must be ≥ 1 ADR below signal bar close (shorts) or ≥ 1 ADR above signal bar close (longs).

Both must be true.

### Phase 2: Outcome Expression Grinder

Brute force expression matrix comparing validated examples vs all signals across their signal-to-exit segments.

- **Positives (examples):** Validated examples, measured from their signal bar forward through exit
- **Universe:** All Step 3 signals, measured from signal bar forward through exit
- **Expression library:** Generic segment expressions — characterize the quality of the move from signal to exit

### Output

**OUTCOME SIGNALS** — the subset of Step 3 signals where the exit triggered, the move reached at least 1 ADR, and the signal-to-exit segment matches example behavior.

### Script: `scripts/outcome_grinder.py`

---

## Step 9: Pre-Signal Refinement Grind — Did Step 3 Miss Anything?

**Goal:** Check if there are pre-signal (scan bar) conditions that distinguish outcome signals from non-outcome signals — conditions that Step 3 couldn't find because it was comparing examples vs the entire 4,000 ticker universe instead of comparing within the signal set.

### How It Works

- **Universe:** All Step 3 signals (the full signal set)
- **Examples:** OUTCOME signals from Step 8
- **Expressions:** The existing 12,175 pre-signal expression library (same as Step 3)
- **Method:** Standard pyramid grinder. Outcome signals as examples, all signals as universe.

**Critical property: TOTAL SIGNALS ≤ original signals, but OUTCOME SIGNALS don't change.** The new conditions only eliminate signals that were non-outcome anyway — the outcome signals pass by definition. Win rate goes up without re-running Steps 7-8.

### Script: `scripts/presignal_grinder.py` (NEW)

---

## Step 10: Environment Clustering — When Does This Setup Work Best?

**Goal:** Find the market environments where the setup produces the highest win rate on quality moves.

### The Math

- **Win rate** = OUTCOME SIGNALS ÷ TOTAL SIGNALS, computed per market environment
- **EV** = (win rate × average captured distance under optimal exit) − (loss rate × average loss)

### Process

1. Compute market context at each signal — SPY regime, breadth, VIX, etc.
2. Split signals by environment — bucket TOTAL SIGNALS into quantile groups
3. Compute win rate per bucket — OUTCOME SIGNALS ÷ TOTAL SIGNALS in bucket
4. Find high-EV environments

### Output

**Environment scoring model** — given tonight's market context, what's the expected win rate and EV?

### Script: `scripts/environment_scorer.py` (NEW)

---

## Summary

| Step | What | Output |
|------|------|--------|
| 1 | **Load** | Data & TA knowledge |
| 2 | **Receive** | Examples, entry dates, setup context |
| 3 | **Signal Grind** | Pyramid grinder → **SIGNALS** (~132/5yr for DTSS) |
| 4 | **Sample Expansion** | Vet signals, expand examples, re-grind until stable |
| 4a | **Exit Grinder** | Single + multi-stage exit → **EXIT CONDITIONS** |
| 4b | **Setup Grinder** | Blackout re-grind + refiner → clean signal set |
| **5** | **Market Grinder** | SPY regime analysis → **WIN RATE per regime** |
| 6 | **Entry Bar Grind** | Entry bar conditions → **ENTRY SIGNALS** |
| 7 | **Exit Management Grind** | Post-entry expression grind → **EXIT CONDITIONS** |
| 8 | **Outcome Grind** | Exit filter + segment grind → **OUTCOME SIGNALS** |
| 9 | **Pre-Signal Refinement** | Outcome vs non-outcome → **TOTAL SIGNALS** (tighter) |
| 10 | **Environment Clustering** | OUTCOME ÷ TOTAL by regime → **EV per environment** |

**The pipeline delivers:**
1. Signal grind → "this setup exists" (watchlist)
2. Entry bar grind → "enter TODAY" (action)
3. Exit grind → "exit HERE" (capture)
4. Market grinder → "take THIS one" (selection, juices win rate)

**The math:** Losers solidly under 1 ADR. Winners median 5.8+ ADR. Even 36% win rate = positive expectancy. Market grinder juicing to 50% = massive profit factor. 20 quality setups/year × 10 setups = plenty of opportunity. 2.5%/month compounding for 20 years = mid 8 figures.

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
  - `POST /api/backtest/signals/upload` — desktop runner uploads signals per setup_type
  - `GET /api/backtest/signals/{setup_type}` — get signals for Historical tab
  - `GET /api/chart-image/{type}/{id}` — chart image
  - `GET /docs` — full Swagger API docs
  - **Nightly endpoints:**
  - `POST /api/universe/append-daily` — incremental OHLCV update
  - **Grinder endpoints:**
  - `POST /api/grinder/start` — submit a grind job
  - `GET /api/grinder/jobs/pending` — pending jobs for agent pickup
  - `POST /api/grinder/status` — agent posts job status updates
  - `POST /api/grinder/progress` — agent posts progress updates
  - `GET /api/grinder/progress/{job_id}` — frontend polls progress
  - `GET /api/grinder/results/{setup_type}` — get latest results
  - `GET /api/grinder/agent/status` — check if desktop agent is online
  - `POST /api/grinder/agent/heartbeat` — agent heartbeat
  - **Market Grinder endpoints (NEW):**
  - `GET /api/setup-grinder/{setup_type}/signals` — all Step 4b signals with exit status and verdicts
  - `POST /api/market-grind/{setup_type}/upload` — upload market grinder results
  - `GET /api/market-grind/{setup_type}/results` — get latest regime model
- **Infrastructure:** SQLite on Railway persistent volume (/app/data)
- **DB tables:** examples, ohlcv, extension, conditions, signal_analysis, universe_ohlcv, tradable_universe, scan_backtest, scan_backtest_clean, ticker_sectors, backtest_status, ticker_classification, universe_exclusions, backtest_signals
