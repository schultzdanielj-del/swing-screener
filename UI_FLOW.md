# ScanPerfect UI — Design Document

This file describes the PySide6 desktop UI intent and design. Implementation status lives in `SWING_SCREENER_PROJECT.md`.

---

## Architecture

Native PySide6 desktop app (`scanperfect.py`). No browser, no server, no tabs.

The pipeline flowchart IS the interface. Each node expands in place to become its own workspace. DO nodes (Examples, Vetting, Scan Tuning) expand to fill the viewport with 20px padding. RUN nodes expand moderately from their position. Setup selector dropdown in the top bar. The daily OHLCV pickle is loaded into memory at startup — exact ticker count depends on the current OHLCV cache state (~11,500 tickers in the raw universe after Session 5 expansion; check `OHLCV_CACHE.md` for the current target). UI startup timing figures in this doc predate the universe expansion and should not be trusted without re-measurement.

---

## Design System

- **Font:** DM Sans (all text), JetBrains Mono / Consolas (monospace)
- **Background:** #000000
- **Card colors:** red (Examples), orange (Vetting), yellow (Scan Tuning), blue (grinds), green (Summary)
- **Borders:** 2.5px, color-matched
- **Locked nodes:** #060606 bg, #111 border
- **Text:** #E0E0E0 primary, #888 secondary, #555 muted

---

## What's Built and Verified Working

- Pipeline flowchart with 7 nodes, expand/collapse animation
- DO nodes expand to fill viewport with 20px padding — clean, fast
- RUN nodes expand to show sub-step badges, Run/Stop/Clear buttons, real-time log streaming
- Causative auto-chains 5 sub-steps: signal_grind → exit_grind → refinement_grind → signal_filter → entry_score
- Examples workspace: chart card grid with batched loading (4 per tick, no UI freeze)
- Vetting workspace: signal list + full candlestick chart + verdict buttons in top bar
- OHLCV loaded at startup, chart browsing is instant (preload thread removed — not needed)
- Mouse wheel zoom on charts
- Yes/No/Skip verdicts all working — buttons in top bar + keyboard 1/2/3
- ADR/ENTRY/COMBINED sort buttons in vetting left panel filter bar
- Causative/Correlative toggle switches signal sources
- Exit lines on vetting charts (from filtered file)
- Exit lines on example cards (computed from exit grinder condition on OHLCV)
- Amber exit lines only on example cards (no purple profit lines)
- Crash protection: vetting interactions wrapped in try/except
- Full DTSS setup description restored and editable
- entry_candle_scorer.py localized (reads local SQLite)

---

## Known Issues

- **Correlative signal count wrong** — shows 402 instead of matching causative winners. Scan Tuning workspace entry sliders now filter these independently.

---

## Examples Workspace

Title from flowchart header (no duplicate). Count + setup label in scroll body.

Add section (collapsible):
- Single add: TICKER + MM/DD/YYYY + ADD button
- Bulk paste textarea + IMPORT ALL
- Pending Final Review grid (4 columns) — shows ADR + entry candle % match + Approve/Reject buttons

Setup Description (right side, with SAVE) — editable text area, stored in SQLite setups table.

Chart card grid (4 columns, batched loading):
- MiniChartWidget: 80 lookback + 40 forward bars, EMA 8/21, entry dot
- Amber exit line computed from OHLCV using exit grinder condition (slope_xavgc21_off7_adr14 <= -1.128826)
- Label row below chart: ticker + date + delete button
- Sort: ADR MOVE / TICKER / DATE

Data sources: examples + pending_examples tables (SQLite), 5yr OHLCV pickle, exit condition computed live from OHLCV

---

## Vetting Workspace

Top bar: CAUSATIVE / CORRELATIVE toggle + signal stats + Entry label + YES/NO/SKIP buttons

Left panel (260px):
- Filter bar: V/U/N checkboxes + ADR/ENTRY/COMBINED sort buttons
- Signal list below

Center: CandlestickChart — candles, EMA 8/21, SMA 50/200, volume, hover crosshair

Metadata label below chart.

### Causative Mode
- Signals: filtered_{setup}.json (produced by signal_filter.py — every signal has exit_date, exit_bar, move_adr, capture_eff)
- Entry scores: joined from entry_scores_{setup}.json

### Correlative Mode
- Signals: ev_{setup}_*.json signals_post
- EV, predicted WR, MFE shown

### Chart Markers
- SIG (white) — signal date
- ENTRY (green) — user-clicked candle
- EXIT (amber) — exit signal date
- E (red) — earnings dates

### Interactions
- Click signal → loads chart
- Click candle → sets entry date
- 1=YES (requires entry_date)
- 2=NO
- 3=SKIP
- ↑↓ navigate
- Mouse wheel zoom
- Floating Yes button at click position

---

## Causative Pipeline — 5 Sub-Steps (Auto-Chained)

1. Signal Grind — pyramid_grinder.py
2. Exit Grind — exit_grinder.py
3. Refinement Grind — pyramid_grinder.py --blackout
4. Signal Filter — signal_filter.py (computes exit dates)
5. Entry Score — entry_candle_scorer.py

---

## Not Yet Built

- **Scan Tuning workspace** — ✅ BUILT. Two tabs (Entry/Exit) in yellow card header. Entry: setup/market feature sliders, refinement depth, WR floor. Exit: management objective toggle, exit expression display, trim slider. SPY bubble chart with signal overlay (green=winner sized by move_adr, red=loser). Drag-scroll, wheel zoom, hover tooltips. Settings auto-save on close, restore on open (`scan_settings_{setup}.json`). EV grinder outputs `setup_score` + `market_score` per signal for independent slider control.
- **Summary workspace** — setup readiness overview

---

## Shelved

- **AI chart review** — Claude CLI vision review of pending examples. Shelved: LSP often off-screen on thumbnails, pattern matching quality insufficient for the complexity. Manual vetting with 1/2/3 keys is fast enough.
- **Chart preloading thread** — OhlcvPreloadThread removed. Charts load instantly from in-memory pickle, no preloading needed.

---

## Functional Color

- Candle up: #4ade80 (vetting), #00e87b (thumbnails)
- Candle down: #f87171 (vetting), #ff3b3b (thumbnails)
- Entry: white #E0E0E0
- Exit signal: amber #E8A735
- Earnings: red #EF4444
