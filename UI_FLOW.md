# ScanPerfect UI — Design Document

**Updated:** 2026-03-18

---

## Architecture

Native PySide6 desktop app (`scanperfect.py`). No browser, no server, no tabs.

The pipeline flowchart IS the interface. Each node expands in place to become its own workspace. Setup selector dropdown in the top bar. 5yr OHLCV pickle loaded into memory at startup (~4,169 tickers in 0.5s).

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
- RUN nodes expand to show sub-step badges, Run/Stop/Clear buttons, real-time log streaming
- Causative auto-chains 5 sub-steps: signal_grind → exit_grind → refinement_grind → signal_filter → entry_score
- Examples workspace expands near-fullscreen with chart card grid
- Vetting workspace expands near-fullscreen with signal list + candlestick chart
- OHLCV loaded at startup, chart browsing is instant
- Yes/No/Skip verdicts save to SQLite + JSON
- Keyboard shortcuts: 1=yes 2=no 3=skip ↑↓=navigate
- Causative/Correlative toggle switches signal sources
- entry_candle_scorer.py localized (reads local SQLite)
- pyramid_grinder.py now saves exit_date + exit_bar per signal

---

## Known Broken / Unverified

- **Zoom non-functional** — Ctrl+wheel code exists but likely consumed by QScrollArea parent before reaching chart widget. Never tested.
- **Yellow exit lines only on ~24% of vetting charts** — causative reads refinement winners (365) and joins exit dates from filtered_dtss.json, but only 87 overlap because filtered file is from March 6 and refinement is from March 13. Fix requires re-running signal_filter.py. The pyramid_grinder now saves exit_date but that refinement hasn't been re-run either.
- **ADR/ENTRY/COMBINED sort buttons** — code added to vetting top bar but NOT verified to appear or produce different orderings. Dan reports they do not show on screen. May be a layout issue or git pull issue.
- **No button (2 key)** — reported broken early, debug prints added, focus fix added, never confirmed working.
- **Correlative signal count wrong** — shows 402 instead of matching 365 causative winners. EV grinder's signals_post includes signals at various refinement depths, not just the current cl102 file. Not fixed.
- **Chart preloading** — OhlcvPreloadThread code exists, never verified it runs.
- **Deferred chart loading in Examples** — code exists (QTimer 50ms), not verified.
- **Purple profit exit lines** — showing on examples cards because profit_dtss.json has 68 entries. This is correct behavior IF profit grind is considered finalized. Dan may not want these shown.

---

## Examples Workspace

Two-column top: Add Examples (left, collapsible) + Setup Description (right, with SAVE)

Add section contains:
- Single add: TICKER + MM/DD/YYYY + ADD button
- Bulk paste textarea + IMPORT ALL
- Pending AI Review grid inside add section (4 columns, PENDING tags, Approve/Reject)

Chart card grid (4 columns):
- MiniChartWidget: 80 lookback + 40 forward bars, EMA 8/21, entry dot
- Exit/profit markers drawn if data available (joined from filtered + profit JSON)
- Label row below chart: ticker + date + actions
- Sort: ADR MOVE / TICKER / DATE

Data sources: examples + pending_examples tables (SQLite), 5yr OHLCV pickle, exit dates from filtered_{setup}.json, profit exits from profit_{setup}.json

---

## Vetting Workspace

Top bar: CAUSATIVE / CORRELATIVE toggle + signal stats + keyboard hints
(ADR/ENTRY/COMBINED sort buttons added in code but not verified on screen)

Three-panel layout:
- Left (260px): Signal list with V/U/N filter checkboxes
- Center: CandlestickChart — candles, EMA 8/21, SMA 50/200, volume, hover crosshair
- Bottom (42px): Metadata + verdict buttons

### Causative Mode
- Signals: refinement_*_cl*.json winner_signals (365)
- Exit dates: joined from filtered_{setup}.json (only ~87/365 have exit dates currently)
- Entry scores: joined from entry_scores_{setup}.json

### Correlative Mode
- Signals: ev_{setup}_*.json signals_post (currently 402 — WRONG, should match causative set)
- EV, predicted WR, MFE shown

### Chart Markers
- SIG (white) — signal date ✅
- ENTRY (green) — user-clicked candle ✅
- EXIT (amber) — exit signal date (only when data exists — ~24% currently)
- PROFIT (purple) — profit grind exit date (only when data exists)
- E (red) — earnings dates ✅

### Interactions
- Click signal → loads chart ✅
- Click candle → sets entry date ✅
- 1=YES (requires entry_date) ✅
- 2=NO — reported broken, unverified fix
- 3=SKIP ✅
- ↑↓ navigate ✅
- Ctrl+wheel zoom — BROKEN

---

## Causative Pipeline — 5 Sub-Steps (Auto-Chained)

1. Signal Grind — pyramid_grinder.py
2. Exit Grind — exit_grinder.py
3. Refinement Grind — pyramid_grinder.py --blackout
4. Signal Filter — signal_filter.py (computes exit dates)
5. Entry Score — entry_candle_scorer.py

---

## Functional Color

- Candle up: #4ade80 (vetting), #00e87b (thumbnails)
- Candle down: #f87171 (vetting), #ff3b3b (thumbnails)
- Entry: white #E0E0E0
- Exit signal: amber #E8A735 (reduced opacity when profit exit exists)
- Profit exit: purple #A855F7
- Earnings: red #EF4444
