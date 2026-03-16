# ScanPerfect UI Flow — Design Document

**Updated:** 2026-03-16 (all questions answered, Phase 1 + Vetting built)

**Design spec:** DM Sans, pure grayscale palette, no accent colors. Functional color only for candle up/down (green/red), yes/no verdicts (green/red), earnings dates (red).

**Architecture:** Currently on Railway, migrating to fully local (see LOCALIZE.md). UI code is identical either way.

---

## Top-Level Structure

**Navigation:** Four tabs — Pipeline, Examples, Vetting, Watchlist. Setup selector dropdown top-right.

**Pipeline tab:** Will become a visual flowchart post-localization (see TODO #8). Currently a sidebar + panel layout with 4 grinder steps.

**Design system:** Gordon Murray stripped style. Black background, grayscale hierarchy (#000 bg, #0A0A0A surface, #1A1A1A rules, #555/#888/#B0B0B0/#E0E0E0 text). No cards, no rounded corners, no shadows. DM Sans font.

---

## Examples Tab (Phase 1) — BUILT

**Purpose:** Define setups and manage example libraries.

**Layout (top to bottom):**
1. Header: setup name + "New Setup" button + example count
2. Side-by-side: Add Examples (collapsible, left) + Setup Description (editable textarea, right)
3. Legend + sort bar (Entry white, Exit amber, Profit purple) + sort buttons (ADR/Ticker/Date)
4. Chart grid: 4-wide, each card = candlestick thumbnail with entry marker, ticker, date, ADR move

**Decisions made:**
- Examples added one at a time (ticker + date) or via paste list (TICKER MM/DD/YYYY format, one per line)
- No chart preview needed when adding — user already knows the trade from Discord/TC2000
- No CSV import — paste box only
- New setups created from UI (name, direction long/short, description)
- Setup description is editable and feeds into AI reviewer prompt
- Chart grid lazy-loads with IntersectionObserver, preloads 2 rows ahead
- Click card to expand to full width
- Pending AI reviews live inside the Add Examples collapsible (chart grid format, not text)
- Sort default: ADR Move descending (biggest movers first)
- Green flash animation + scroll-to on newly added examples

**Entry/Exit/Profit markers on charts:**
- Entry: solid white line
- Exit Signal: solid amber (#E8A735) line
- Profit Exit: solid purple (#A855F7) line (when profit grind data exists)
- All solid lines, no dashed/dotted

---

## Pipeline Tab (Phases 2-4) — BUILT (basic), REDESIGN PLANNED

**Current:** 4-step sidebar + panel layout. Signal Grind, Exit Grind, Refinement Grind, EV Grinder. Each has run/stop buttons and log stream.

**Post-localization redesign:** Visual flowchart. Each pipeline stage is a clickable node. Click to expand inline (run controls, logs, results) or navigate to relevant tab. Shows setup development progress visually.

**Decisions made:**
- Grinder output always taken as-is — no manual condition editing
- No margin progression slider (scrapped)
- No depth progression slider (scrapped)
- Always trust the grinder for exit expression — no manual override
- Vetting is optional — can skip straight to EV Grinder
- Entry candle scorer integrated into refinement grind step (not a separate button)

**Flowchart nodes (planned):**
Examples → Signal Grind → Exit Grind → Refinement Grind → Vetting → EV Grinder → Scan Tuning (sliders) → Profit Grind → Live Watchlist

---

## Vetting Tab (Phase 5) — BUILT

**Purpose:** Review winner signals from refinement grind, bank new examples.

**Layout:**
- Header: "Winners" label + count | V/U/N checkboxes (stacked, with counts) | Legend (Signal/Exit/Profit/Earnings)
- Left panel: signal list (ticker, date, ADR move, verdict indicator)
- Center: candlestick chart (large, zoomable)
- Bottom bar: signal metadata + Yes/No/Skip buttons

**Decisions made:**
- Shows winner pile ONLY — no pre/post refinement toggle, no losers
- V/U/N checkboxes to filter: V (green) = vetted yes, U (gray) = unvetted, N (red) = no
  - U checked by default
  - Check N to see and undo rejected signals
- YES requires clicking an entry bar on the chart first — button disabled until clicked
- Clicking entry bar shows floating green "Yes ✓" button right next to click position
- YES → pending_examples → AI second-pass → approve on Examples tab (AI gate kept)
- NO removes signal from unvetted list, stored in rejected_signals table
- Signal bar labeled "SIG" (not "ENTRY") — ENTRY label only appears when you click
- Mouse wheel zoom: scroll up = zoom in, scroll down = zoom out, centered on signal
- Chart preloading: first 15 signals fetched on load, next 10 preloaded on every navigation
- Earnings dates: bold red (#EF4444) solid vertical lines with "E" label
- Keyboard: 1=yes, 2=no, 3=skip, ↑↓=navigate

---

## EV Grinder (Phase 6) — NOT YET BUILT

**Purpose:** Score every signal with predicted WR, MFE, EV.

**Planned UI:**
- Run button + log
- Feature summary, calibration table, RMSE
- Slider 1: quality_score threshold
- Slider 2: minimum predicted WR
- Top signals table ranked by EV

**Open questions (for when we build it):**
- Q13: Do sliders pass fixed values to Profit Grind, or does Profit Grind test across slider ranges?
- Q15: Per-year breakdown of signal performance?

---

## Profit Grind (Phase 7) — NOT YET BUILT

**Purpose:** Optimize exit strategy for max account growth (SQN).

**Key decisions already made:**
- Uses SQN (System Quality Number) as objective function, not raw compound growth
- Brute-forces stop/target/trail parameters across Slider 1/2 threshold combinations
- Uses actual entry candle prices where available
- Trim-and-trail strategies tested
- Data source: 5yr OHLCV cache

**Open questions (for when we build it):**
- Q16-Q20: See TODO.md Phase 4 section for full spec

---

## Watchlist Tab (Phase 8) — NOT YET BUILT

**Purpose:** Nightly ranked signal list across all setup types.

**Open questions (for when we build it):**
- Q21: Today's signals only, or also active trades?
- Q22: Paper trade mode?
- Q23: Interleaved by EV rank or grouped by setup type?
