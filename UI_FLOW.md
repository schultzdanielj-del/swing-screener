# ScanPerfect UI — Design Document

**Updated:** 2026-03-18

---

## Architecture

Native PySide6 desktop app (`scanperfect.py`). No browser, no server, no tabs.

The pipeline flowchart IS the interface. Each node expands in place to become its own workspace. Setup selector dropdown in the top bar. 5yr OHLCV pickle loaded into memory at startup (~4,169 tickers in 0.5s).

---

## Design System

- **Font:** DM Sans (all text — titles, labels, card content, detail panels)
- **Monospace:** JetBrains Mono / Consolas (signal lists, metadata, chart labels)
- **Background:** #000000
- **Card colors:** Each node type has a distinct deep saturated background color
  - Examples: deep red (#2a1215 → #3d1b20 gradient)
  - Vetting: deep orange (#2a1f0d → #3d2e15)
  - Scan Tuning: deep yellow (#2a2610 → #3d3818)
  - Grinds (Causative, Correlative, Optimal Management): deep blue (#0d1a2a → #15263d)
  - Summary: deep green (#0d2a1a → #153d26)
- **Borders:** 2.5px, color-matched to card type
- **Locked nodes:** Nearly invisible (#060606 bg, #111 border)
- **Text:** #E0E0E0 primary, #888 secondary, #555 muted

---

## Pipeline Flowchart — 7 Nodes, Two Loops

```
        ┌──────────────────────────────┐
        │         LOOP 1               │
  [Examples] ──→ [Causative Processing] ──→ [Vetting]
        ↑                                      │
        └──────── add examples ────────────────┘
                                               │
                                    [Correlative Targeting]
                                               │
        ┌──────────────────────────────────────┤
        │         LOOP 2                       │
  [Scan Tuning] ──→ [Optimal Management]       │
        ↑                    │                 │
        └── tweak · re-run ──┘        [Summary]
```

**DO nodes (left column, warm colors, rounded corners):**
- Examples — manage example library + pending review ✅ BUILT
- Vetting — review winners, bank new examples ✅ BUILT
- Scan Tuning — quality score + WR threshold sliders (future)

**RUN nodes (right column, blue, sharp corners):**
- Causative Processing — 5 sub-steps with auto-chaining ✅ BUILT
- Correlative Targeting — EV grinder scoring ✅ BUILT
- Optimal Management — exit strategy optimization via SQN (future)

**Summary (right side, green):**
- Setup readiness overview (future)

---

## Unlock Progression

Everything starts locked except Examples. Gates:
- **Causative Processing:** ≥20 examples
- **Vetting:** Causative complete (refinement_*_cl*.json exists)
- **Correlative Targeting:** ≥60 examples AND Causative complete
- **Scan Tuning:** Correlative complete (ev_*.json exists)
- **Optimal Management:** Correlative complete
- **Summary:** Optimal Management complete (future)

---

## Card Interaction

**Animation:** Cards expand/collapse at 80/60 px/frame (~60fps). Near-fullscreen for Examples and Vetting (32px thin header for collapse click target). Moderate size for RUN nodes.

**DO nodes** (Examples, Vetting): Click expands in place to near-fullscreen workspace. Click the thin header to collapse.

**RUN nodes** (Causative, Correlative, Optimal Management): Click expands with animation. Shows:
- Sub-step progress badges (Causative has 5: Signal Grind / Exit Grind / Refinement / Signal Filter / Entry Score)
- Metrics row: Status, Last Run, Duration, Setup
- Run / Stop / Clear Log buttons
- Real-time log viewer (QProcess stdout streaming)
- **Auto-chaining:** When a sub-step completes successfully, the next sub-step auto-starts (500ms delay)

Click again to collapse.

---

## Causative Processing — 5 Sub-Steps (Auto-Chained)

1. **Signal Grind** — pyramid_grinder.py (beam search)
2. **Exit Grind** — exit_grinder.py (optimal exit condition)
3. **Refinement Grind** — pyramid_grinder.py --blackout (winner/loser classification + elimination)
4. **Signal Filter** — signal_filter.py (computes exit dates for all signals)
5. **Entry Score** — entry_candle_scorer.py (entry candle similarity scoring)

Hit Run once → all 5 chain automatically. If any step errors, the chain stops.

---

## Examples Card (Collapsed)

Shows progress bar: `66 / 365` (examples / winner clusters from latest refinement_*_cl*.json).
Progress bar is thick with rounded ends, gradient fill in the card's red accent color.
Count shown inline next to the title.

---

## Examples Workspace (Expanded) ✅ BUILT

Two-column top area:
- **Left:** Collapsible "Add Examples" section
  - Single add: TICKER + MM/DD/YYYY + ADD button
  - Bulk paste textarea + IMPORT ALL button
  - Pending AI Review grid (when pending examples exist): 4-column card grid with PENDING/APPROVE/REJECT tags, Approve/Reject buttons per card
  - Auto-opens when pending items exist
- **Right:** Setup Description textarea with SAVE button

Chart legend bar: ■ Entry (white) · ■ Exit Signal (amber) · ■ Profit Exit (purple)
Sort toggle: ADR MOVE ↓ / TICKER / DATE (right-aligned)

4-column chart card grid:
- Cards ~180px tall, chart fills card via MiniChartWidget
- ~120 bars (80 lookback + 40 forward), EMA 8 + EMA 21 lines
- Green (#00e87b) / red (#ff3b3b) candles
- White entry marker line + white dot at close price
- Amber exit signal line (reduced opacity when profit exit exists)
- Purple profit exit line (when profit grind data exists)
- Label row below chart: ticker + date + actions
- Charts load deferred (50ms after grid appears) for instant expansion

Data: examples + pending_examples tables (SQLite), OHLCV from 5yr pickle, exit dates from filtered_{setup}.json, profit exits from profit_{setup}.json

---

## Vetting Workspace (Expanded) ✅ BUILT

Top bar: CAUSATIVE / CORRELATIVE toggle + stats + ADR / ENTRY / COMBINED sort + keyboard hints

Three-panel layout:
- **Left (260px):** Signal list with V/U/N filter checkboxes. Plain text items for speed.
- **Center:** CandlestickChart (QPainter) — candles, MAs, markers, volume, hover crosshair
- **Bottom (42px):** Metadata + YES (1) / NO (2) / SKIP (3) buttons

### Causative Mode
- Signals from refinement_*_cl*.json winner_signals
- Exit dates joined from filtered_{setup}.json
- Entry candle scores joined from entry_scores_{setup}.json

### Correlative Mode
- Signals from ev_{setup}_*.json signals_post
- EV, predicted WR, MFE, quality score displayed
- Entry candle scores joined same as causative

### Sort Options
- **ADR** — move_adr descending
- **ENTRY** — entry_candle_pct descending
- **COMBINED** — combined_score descending (default)

### Chart Markers
- **SIG** (white) — signal date
- **ENTRY** (green) — user-clicked entry candle
- **EXIT** (amber) — exit signal date. Reduced opacity when profit exit exists.
- **PROFIT** (purple) — profit grind exit date.
- **E** (red) — earnings dates

### Interactions
- Click signal → loads chart (instant from in-memory OHLCV)
- Click candle → sets entry date
- 1=YES 2=NO 3=SKIP, ↑↓ navigate, wheel pan, Ctrl+wheel zoom
- Focus auto-restored after every verdict

---

## Functional Color (within workspaces)

- Candle up: #4ade80 (vetting), #00e87b (thumbnails)
- Candle down: #f87171 (vetting), #ff3b3b (thumbnails)
- Entry marker: white (#E0E0E0)
- Exit signal: amber (#E8A735)
- Profit exit: purple (#A855F7)
- Earnings: red (#EF4444)
- EMA 8: #5dade2 / EMA 21: #d4a853 / SMA 50: #f5c542 / SMA 200: #e74c3c
