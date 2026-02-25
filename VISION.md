# Swing Screener — Ultimate Vision & Roadmap

**Last updated:** 2026-02-25

---

## THE ENDGAME

Every night, the system delivers a shortlist of 5-15 trades with:

1. **The ticker and direction** (long or short)
2. **Why it has edge right now** — extension position, market stage alignment, structural setup
3. **Room to run** — where the stock sits on its extension structure vs its historical ceiling
4. **Key levels** — breakout AVWAP, algo lines, MOC/LSP levels, baby/daddy MAs
5. **Gamma check** — is the options hedging profile aligned with the trade direction (positive above for longs, negative below for shorts)
6. **Entry structure** — where to get in with the tightest possible stop

At the open, Dan watches intraday action on these pre-vetted charts. AVWAP foothold confirmation, baby/daddy MA behavior, RVOL. Just execution — no discovery, no scrambling.

The concept of named "setups" eventually dissolves. The system screens for probability and edge, not pattern labels.

---

## WORKING BACKWARDS — THE LAYERS

### Layer 6: The Delivery (FINAL)
- Clean nightly output: ticker, direction, levels, extension room, gamma, entry plan
- Delivered before market open (night before or pre-market)
- Format TBD (dashboard, Discord alert, simple web page, etc.)

### Layer 5: Gamma Check
- For high-liquidity options names, check GEX profile alignment
- Binary: is gamma below entry negative (for shorts) / above entry positive (for longs)?
- Pull from gexbot or similar source, 90 DTE monthlies
- Only fat strikes matter — vol trigger flip point, biggest positive, biggest negative
- 24-48hr freshness window

### Layer 4: Level Identification
- Auto-identify key D1 levels per ticker:
  - Breakout AVWAP (anchored to highest contextual volume candle, NOT the peak)
  - Algo lines (trendlines from high-RVOL D1 candle highs/lows)
  - MOC lines (highs/lows of highest RVOL candles)
  - LSP (left side pivot — major structural level)
  - Baby/daddy MA positions relative to breakout level
- Requires OHLCV + volume data with MA of volume for RVOL identification

### Layer 3: Extension Analysis
- Compute 50 SMA extension (x ADR from 50 SMA) for each candidate
- Profile each stock's historical extension ceiling (statistical peaks)
- Calculate remaining runway: current extension vs ceiling = room to run
- Flag whether extension has been "reset" (prior correction below 50) = full runway vs capped
- 200 SMA extension for macro context (dip vs correction threshold)
- Extension trendline structure (declining peaks = stage 3 behavior)

### Layer 2: Market Context
- Determine current market stage (1, 2a, 2b, 3, 4) from SPY extension structure
  - 50 SMA extension slope and structure (pyramid shape, pennant compression)
  - 200 SMA extension channel (bull channel, breaking, declining)
  - 10/20 SMA cross direction
- Stage determines strategy: buy stupid → buy dips → short setups → cash/correction
- T2104 breadth confirmation (trending + smooth = safe, divergent = mean reversion)
- VIX level check (stage 2b: below ~17-18 + over 21 EMA = go)
- Wave cycle position: are we at flush (0 results expected) or bounce peak (max results)?

### Layer 1: Candidate Screening (CURRENT FOCUS)
- Pyramid grinder: automated 6-tier expression discovery from validated examples
- 4,017 expressions across 29 categories, exhaustive beam search (beam=10000)
- Produces ~2-7 signals/day historically across 5yr from ~4,000 tradable tickers
- **Current state:** DTSS complete (26 conditions, peak 6/day, 201 signals/5yr). 3-4DB and HTF queued.

### Layer 0: Setup Library (FOUNDATION)
- Upload example trades → grinder discovers optimal conditions automatically
- Universal system — same pipeline for any setup type
- Current: DTSS (26 examples, complete), 3-4DB (21 examples, loaded), HTF (scaffolded)
- Pattern labels are entry vehicles — extension structures define actual probability

---

## CURRENT PRIORITIES

1. **Market context (Layer 2)** — correlate DTSS signal outcomes with market regime/stage
2. **EV optimization** — exhaustive management parameter search (stop/target/trail)
3. **3-4DB + HTF setups** — run through same grinder pipeline
4. **Extension analysis (Layer 3)** — per-stock extension ceiling profiling
5. **Level identification (Layer 4)** — auto-detect AVWAP, algo lines, MOC/LSP levels

---

## CONSTRAINTS

- $0 additional cost — all within existing subscriptions (Claude Max, TC2000, GitHub, Railway, Discord)
- Pyramid grinder runs on Dan's desktop (i5-12600K), ~5 min per grind sweep
- Railway hosts the API + frontend + SQLite DB (11M+ OHLCV rows)
- Dan's discretion remains the final filter — system proposes, Dan disposes

---

## PHILOSOPHY

The market is an order fulfillment engine. All price action is liquidity seeking and institutional order flow. TA isn't mystical — it's reading the footprints of large orders that can't fill all at once.

Extension structures show the potential. Setups provide tight entry structure. Management extracts the profit. The screener's job is to find where all these align — and get out of Dan's way at the open.
