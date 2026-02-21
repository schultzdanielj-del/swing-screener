# Setup Build Process — The Playbook Factory

**Purpose:** This is the step-by-step process for building, validating, and optimizing any new trading setup. Follow this every time we're working on a setup from scratch or refining an existing one.

---

## The Grand Goal

Find the biggest winning moves in the market — long and short — and work **backwards** from those to discover every reliable way to get into them. Then manage mechanically.

Individual setups (3-4DB, DTSS, HTF, and ones not yet discovered) are not standalone strategies. They are **different entry paths into the same winning trades.** The setups are doors — the moves are the room.

This means Claude needs two capabilities:
1. **Deep knowledge of all known setups** — their conditions, when they work, how to manage them
2. **Deep knowledge of TA principles** — to recognize new entry paths that don't match any existing setup template

The bottom-up work (building individual setups through Phases 1-7 below) feeds the top-down work (identifying the biggest moves and reverse engineering every entry). Everything converges.

**Best setups × Best markets × Best management = Highest EV possible**

---

## Phase 1 — Data Foundation

- All OHLCV data lives in the ScanPerfect database (`universe_ohlcv`). It updates nightly with the most recent trading day.
- The tradable universe (~4,167 tickers) is pre-filtered for liquidity (≥$1 close, ≥$5M avg dollar volume). No Yahoo Finance fetching needed.
- Load `ta_knowledge.md` to understand what TA concepts and patterns apply.

**Nothing is fetched from external sources. Everything runs against the existing database.**

---

## Phase 2 — Setup Definition

User provides:
- **Example trades** — real tickers with entry dates
- **Setup description** — what the pattern looks like, what it captures
- **Entry dates** — exact candle where the trade triggers
- **Any additional TA context** — channel structure, AVWAP behavior, market stage, etc.

This gives Claude a concrete foundation of what the setup looks like in real data, not just a visual description.

---

## Phase 3 — Condition Discovery (Zero False Negatives)

Find PCF conditions that match **every single example**. Rules:

- All conditions normalized to **ATR14 or ADR** — no fixed dollar/percentage thresholds
- Eliminate duplicate ETFs (use underlying stock only)
- Exclude inverse/leveraged ETFs
- Ask user whether to exclude biotech (setup-dependent)
- Test each condition against ALL examples before including it
- **Zero false negatives first** — every example must pass every condition

Run the conditions against the **most current day** of tradable tickers to see how many results the scan produces.

---

## Phase 4 — Iterative Tightening

Keep testing new conditions and combinations to reduce scan results:

- Try tighter thresholds on existing conditions
- Add new complementary conditions (moving average relationships, volume patterns, extension measurements, slope requirements)
- Each iteration: verify zero false negatives, then check result count
- **Target: <100 results for a single-day scan**

Track what each condition adds/removes so we understand its filtering power.

---

## Phase 5 — Advanced Filtering & Collaboration

Go beyond simple threshold filtering:

- Relative strength/weakness vs market
- Sector concentration analysis
- Multi-timeframe confirmation
- AVWAP structure analysis
- Channel structure validation
- Volume profile patterns
- Extension structure analysis (ATR multiples from MAs)

Collaborate with user's pattern recognition to identify what separates the best setups from noise. Push toward the **tightest possible conditions** — prefer missing some opportunities over wasting time on false positives.

---

## Phase 6 — Store & Backtest

Once conditions are tight enough:

1. **Store the final condition set** in ScanPerfect database
2. **Run historical backtest** — scan every trading day across available history
3. **Analyze signal distribution** — when and where do these setups appear?
4. **Map to market conditions** — which market stages (1-4), which regimes produce the most signals?
5. **Identify clustering** — do signals pulse in waves? Are there "chart theme" days?

This reveals the **market context** where the setup has the highest probability of success.

---

## Phase 7 — EV Optimization

Focus on the highest-success market conditions identified in Phase 6:

- **Condition tweaking** — can we tighten/loosen conditions for better performance in the best regimes?
- **Entry optimization** — exact entry timing, confirmation requirements
- **Stop placement** — ATR-based, structure-based, what produces best R:R?
- **Target/management rules** — partial exits, trailing stops, time-based exits
- **Win rate vs. profit-per-trade tradeoff** — find the optimal balance

Test combinations of conditions + management rules to maximize:
- Win rate
- Average R per trade
- Expectancy (win% × avg win - loss% × avg loss)

---

## Output: The Playbook Entry

Each completed setup produces:

1. **Setup name & description**
2. **Final PCF conditions** (copy-paste ready for TC2000)
3. **Market regime filter** — when to run this scan
4. **Entry rules** — exact trigger criteria
5. **Management rules** — stops, targets, position sizing
6. **Expected performance** — win rate, avg R, expectancy by market condition
7. **Historical signal distribution** — when this setup fires and when it doesn't

---

## Key Principles

- **Least noise over maximum coverage** — miss some setups rather than waste time on false positives
- **Work backwards from real examples** — math from actual OHLCV, not visual descriptions
- **Market moves in 3-5 day waves** — scan results pulse with cycles
- **The setup is only as good as its market context** — timing matters more than the pattern alone
- **Every condition must earn its place** — if it doesn't meaningfully reduce noise, drop it
