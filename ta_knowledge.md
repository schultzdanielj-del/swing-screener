# TA Knowledge Base

## Market Stages (SPY / Broad Market)

### Stage 1 — Basing / Accumulation
- (To be defined)

### Stage 2a — "Buy Stupid"
- Follows a deep correction (e.g. tariff scare April 2025)
- 10/20 SMA bull cross triggers entry
- Break of downtrending/capitulating 200 SMA extension structure
- 50 SMA extension is upsloping
- 200 SMA extension is in a bullish channel
- 50 extension builds above ~3x ADR — confirms bullish run
- **Strategy:** Hammer every breakout. Go go go.
- **Reference:** SPY April–July 2025

### Stage 2b — "Buy Dips"
- Begins after the first big ATR expansion since stage 2a start (~7/30/2025 on SPY)
- 50 SMA extension starts downsloping (declining side of the pyramid)
- 200 SMA extension still in bull channel
- Stage 2a + 2b together form a pyramid/mountain shape on the 50 extension histogram
- **Critical:** Once 50 extension breaks from upslope to downslope, it almost never returns to 2a-style upslope directly. Must flush through the inverse underside extension structure first (correction/bigger move) before a new 2a can start. Exception: extreme liquidity events like QE 2021.
- Declining 2b structure forms a pennant/triangle on the 50 extension (descending peaks, AND ascending troughs — the underside dips get shallower and shallower). By stage 3 the extension is compressed tight near zero. Must break to the downside to reset and start a new cycle.
- **Strategy:** Buy dips, not breakouts. More selective.
- **Entry signal:** VIX dips back below ~17-18 AND price is over the 21 EMA = good to go
- **Transition out:** Stage 2 ends when 200 extension channel breaks structure AND 50 extension flattens out

### Stage 3 — Distribution
- 50 SMA is flat (no slope) — no trend direction
- 200 SMA extension is declining / breaking its bull channel
- Price chops around the 50 SMA with no directional conviction
- **Strategy:** Short setups begin to dominate. 3-4DB setups increase.
- Precedes stage 4 (markdown)
- **Reference:** SPY Nov 2025–Feb 2026

### Stage 4 — Markdown / Correction
- 200 SMA extension (already declining from stage 3) capitulates below a common support area
- On SPY, ~5% below the 200 SMA is roughly the threshold (eyeballed, not exact — needs validation)
- Visually confirmed on 2021-2026 SPY weekly: dips stay above -5% on 200 extension, corrections (2022 bear, tariff scare) blow through it
- Breaking below ~5% with TA confirmation typically gives sustained bearish 10/20 SMA trend
- Corrections are fast and proportionally infrequent

---

## Extension Structure — Individual Stock Profiles

- Extension from 50 SMA (in multiples of ADR) is the universal normalized cycle indicator
- Works across all timeframes and instruments — same rhythm, different magnitudes
- **Max extension varies by stock maturity:**
  - Recent IPOs: 7-10-12+ ADR multiples above 50 SMA
  - Metals/commodities (crazy events): can go extreme
  - Established large caps (F, KO): rarely past 6x, usually 3-5x
- Older/more liquid stocks have lower extension ceilings — decades of mean-reversion conditioning
- Implication: knowing a stock's typical max extension helps gauge where it is in its cycle and conviction level on setups
- Extension peaks often cluster at specific levels (bimodal) — e.g. AAPL either fakes out just above 50 SMA (1-2x ADR) or trends to 6-7.5x ADR. Gap in between = stock doesn't hang out at intermediate extensions.
- **200 SMA extension also provides ceilings** — e.g. AAPL has a hard cap at ~25% above the 200 SMA across 4 years. Fade swing short at that level for near top-tick entries. Each stock has its own 200 extension profile.
- Use historical extension peak/valley clustering to improve fade timing — short at the statistical ceiling, not arbitrary levels
- **For longs/breakouts:** Check current position on 50 extension to estimate remaining upside. If stock is already at 5x ADR and historically peaks at 6-7x, only 1-2 ADR of upside left — trim there and trail tighter. Some stocks have great setups but statistically no room to run.
- **Proximity to 50 SMA = upside potential.** The closer a setup is to the 50, the more room it has. Closer to 50 AND coming out of a recent correction = maximum upside potential.
- Trendline breaks on the 50 extension structure itself can confirm the move is starting.
- **Why big base breakouts are the biggest movers:** A huge base typically has a correction below the 50 embedded in it, which resets the ADR extension potential back to zero. Breaking out of that base = full statistical runway to the max extension ceiling. Same reason post-correction stocks run hardest — the extension counter is reset.

- Bull channels break one side: either establish steeper channel on top (continuation) or break opposite side for measured move
- If lower trendline breaks and retest fails → measured move = channel width projected downward
- 3-4DB setup = channel lower TL break + retest of broken TL from below (flipped to resistance)
- Steeper channels more likely to snap than stair-step — better 3-4DB candidates
- Channel slope of 2-3% per bar is typical for 3-4DB setups (from analysis of 14 examples)

## Trade Management — Context-Dependent

- Management strategy should match the market stage and extension position
- **Early trend / post-correction (stage 2a, near 50 SMA):** Full runway available. Sit on it with stop at break-even for weeks. Trim partial at 21 EMA break, hold rest until 50 SMA break.
- **Late trend / extended (stage 2b/3, near statistical ceiling):** Take the whole position off on a good win. Less room to run = less reason to hold.
- **Key principle:** Don't apply the same management to every trade. A long setup in stage 3 with 1-2 ADR of upside left gets a completely different plan than a post-correction breakout with full runway.
- Trim levels and trailing stop tightness should scale with remaining statistical upside (extension position relative to historical ceiling)
- **Example — Stage 3 long (UNR/undercut-and-rally):** RIVN historically peaks at ~1.0-1.25x ADR on 50 extension in stage 3. Take UNR entry at the low, exit entire position when extension hits that ceiling. Not hindsight — the extension history told you the max upside before you entered.

## Intraday AVWAP Confirmation

- Price must hold sequential daily AVWAPs on the same side of the 8 EMA to confirm a breakout
- Each AVWAP = volume-weighted fair value from that session
- If price skips or fails to hold one in sequence, breakout is suspect
- 10-30% winrate improvement when used as execution filter
- Used for intraday entry timing, not daily scan level

## Chart Themes & Wave Cycles

- Market produces setups in 3-5 day waves
- Flush day → 0 scan results (everything dropping, no bounces yet)
- Day 1-2 of bounce → results building as stocks retest broken support
- Day 3-4 of bounce → peak scan results, best entries, "chart theme" visible
- Bounce fails → results clear out, next leg begins
- High result count on a given night = wave is peaking
- "Chart theme" = many of the same setup type appearing across unrelated tickers

## PCF Terminology Reference

| Concept | TC2000 PCF Code |
|---------|----------------|
| EMA | XAVGC (e.g. EMA21 = XAVGC21) |
| SMA | AVGC (e.g. SMA50 = AVGC50) |
| ATR 14-period | ATR14 |
| Highest High N bars | MAXHN |
| Lowest Low N bars | MINLN |
| Offset by N bars | .N suffix (e.g. MAXH20.20) |
