# DTSS Setup Analysis — Next Chat Prompt

## TASK
Analyze the DTSS (Double Top Short Sell) setup across 26 validated examples to find universal numerical conditions for TC2000 PCF scan code.

## WHAT WE HAVE
- **26 DTSS examples** loaded in ScanPerfect app (Railway: web-production-e3025.up.railway.app)
- **LSP data** (Left Side Pivot — the prior high that forms the "first top"): `data/dtss_lsp_data.json` in the repo
- **OHLCV data** for all tickers available in Railway's `universe_ohlcv` table (5yr daily, 11K tickers)
- **Entry dates** stored in the app for each example

## WHAT TO DO
1. Clone the repo: `https://github.com/schultzdanielj-del/swing-screener.git`
2. Read `data/dtss_lsp_data.json` — this has ticker, entry_date, example_id, LSP date, and LSP price for all 26 examples
3. Fetch OHLCV data from Railway API or yfinance for each ticker (need ~200 bars before entry date)
4. For each example at the ENTRY DATE, compute metrics including:
   - **LSP relationship:** distance in bars, overshoot % (entry HOD vs LSP high), overshoot in ATR
   - **Approach rally:** how many days/bars from recent low to entry, gain %, gain in ATR, volume trend
   - **Entry candle:** range in ATR, upper wick %, red/green, RVOL, body size
   - **Stop size:** HOD - entry close in ATR (stop = HOD)
   - **Position:** extension from SMA50, SMA200, SMA50 slope, EMA8/21 positioning
   - **Pullback between tops:** did price pull back meaningfully between LSP and entry?
5. Find conditions that hit 24/26+ examples (≥92%)
6. Present results — DO NOT proceed to writing PCF code until I say go

## REFERENCE
Look at `data/ohlcv/3-4db/signal_day_analysis.json` for the format used in the 3-4DB analysis (14 examples). Follow similar structure but adapted for DTSS-specific metrics (LSP relationship is the key differentiator).

## KEY DTSS CONCEPT
The setup is: price made a significant high (the LSP), pulled back, then rallied back to test that same level and GOT REJECTED. Entry is the rejection candle day. The LSP can be anywhere from 5 to 200+ bars back. The "double top" doesn't need to be exact — overshoots of a few percent are normal.
