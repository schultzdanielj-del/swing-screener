# THEME_DASHBOARD

## Purpose

Browse the active-trading universe organized into market themes (Optics, Space, Memory, Cybersecurity, Uranium, Drones, Batteries, AI Compute, Neoclouds, etc.). Each theme produces an equal-weight synthetic composite candle chart plus a grid of member mini-charts. Output is a single self-contained HTML file so the dashboard runs in any browser — no server, no PySide6 launch — and one theme is shown at a time with arrow-key navigation through a sortable watchlist sidebar.

## EXACT spec

Generator: `local_runner/theme_dashboard.py`.
Theme map: `local_runner/theme_map.py` — `THEMES` dict (theme key → ticker list), `THEME_LABELS` dict (theme key → display label), and `UNIVERSE` list (the full hand-picked universe). Edited by hand.
Output: `local_runner/cache/theme_dashboard.html`.

Invocation:
- `python local_runner/theme_dashboard.py` — render every theme in `THEMES`.
- `python local_runner/theme_dashboard.py --theme NAME` — render one theme (dev/debug).
- `python local_runner/theme_dashboard.py --bars 250` — change the bar count (default 250 daily bars).
- Add `--open` to launch the HTML in the default browser after writing.

Composite OHLC math (per theme):
1. Take the last N daily bars (N = `--bars`, default 250) using the most-recent member's date axis as the canonical window.
2. For each member with data in the window, find that member's first valid bar inside the window and scale all OHLC and volume so close at that first bar = 100. Newer IPOs join the composite mid-window.
3. Composite OHLC at each bar = arithmetic mean across members that have data at that bar.
4. Composite volume at each bar = arithmetic mean of normalized member volumes.
5. Bars where fewer than half the members are present are dropped.

Per-theme metrics computed at the last bar:
- **Position vs 200-day SMA** as a signed percentage. Themes below 200-day get flagged red.
- **TC2000 RS PCF** in two windows (5-bar and 20-bar):
  - `avg = mean over last N bars of (close/open − 1) × 100`
  - `mult = ((close + close_50_bars_ago) / 2) / ATR50`
  - `theme_RS = avg × mult`
  - SPY RS computed with the same formula. Ratio = `theme_RS / SPY_RS`.
- **MACD-line divergences** (price pivots paired with EMA6 − EMA20 pivots):
  - Pivot window 3 bars on each side; lookback 240 bars; minimum spacing 6 bars; MACD-pivot offset tolerance ±10 bars.
  - For each price pivot, scan backward through prior price pivots and accept the FIRST pair whose connecting trendline is *not pierced* by an intervening bar (low must not break a bullish trendline; high must not break a bearish trendline). One divergence per anchor pivot.
  - Both bullish (price LL, MACD HL) and bearish (price HH, MACD LH) are detected.

Composite chart (interactive Plotly, one per theme):
- Three panels: candles + SMAs (62%), volume (13%), MACD 6/20/9 (25%).
- SMAs drawn: 5 (orange `#ff8800`), 10 (cyan `#5fc8ff`), 20 (beige `#e8c890`), 50 (yellow `#ffcc00`), 200 (white).
- MACD panel: MACD line (cyan), signal line (orange), zero line. **No histogram bars.**
- All MACD-line divergence pairs over the lookback window draw a dotted connector on both panels (green for bullish, red for bearish). Older divergences drawn at 55% opacity; the most recent in each direction draws at full opacity with a text label arrow pointing at the second pivot.
- Right-side x-axis padded by ~30 calendar days beyond the last bar; `rangebreaks` hide Saturday/Sunday so bars sit shoulder-to-shoulder.
- Candle up = bright green `#1eff1e`, down = bright red `#ff3030`. Background pure black, grid `#1a1a1c`, axis ticks light gray; crosshair spike lines cyan.

Member mini-chart (hand-built inline SVG, one per member per theme):
- 100 daily bars, single candle panel, black background, ~18% empty space on the right.
- 24px gray-gradient header strip showing ticker on the left and last close + day change % on the right (green/red).
- No SMAs, no axis labels.

HTML shell:
- Fixed gray-gradient top header with cells: brand, generated timestamp, bar count, theme count, cache last-bar date, SPY 5d return + ADR, sort label, position indicator, navigation hint, universe summary.
- Sticky left sidebar holds a **sortable watchlist table** (320px wide). Columns: Theme, 5d RS, 20d RS, N. Click any column header to sort by it; click again to flip direction. Active column shows ▾/▴. Theme name colored red when the composite is below its 200-day SMA.
- Watchlist controls bar: `Hide below 200D` checkbox + visible-row count.
- Only the active theme's section is visible at a time; all others have `display: none`. Click a row → activate that theme. Click a column header → resort the table.
- Keyboard navigation: ← / ↑ / k / PageUp = previous visible row; → / ↓ / j / Space / PageDown = next visible row; Home / End = first / last visible. The sequence respects the current sort and filter.
- URL hash updates on each navigation (e.g., `#optics_photonics`) for deep-linking.
- Each chart section has its own thin pure-black info bar above the chart: `RS 5d vs SPY` cell, `vs 200D` cell, optional `BULL DIV` / `BEAR DIV` tags (with ×N count if more than one), date, OHLC, Chg, Chg%, Vol, APTR, SMAs values.
- Fixed gray-gradient status bar at the bottom: live indicator, cache date, active theme label.
- All CSS inlined. Plotly.js loaded from cdn.plot.ly. No other external assets.

Ungrouped section:
- Computed from `UNIVERSE` — any ticker in the universe list that is not in any theme is rendered as a final section at the bottom titled "Ungrouped", with member mini-charts only (no composite). The watchlist row for it is highlighted gold.
- Tickers missing from the OHLCV cache (e.g., `BF.B` → cache key `BF-B`) are reported on stdout and counted in the header's universe summary but not rendered.

Cache rules applied at startup:
- Loads `local_runner/cache/universe_ohlcv_daily.pkl` (falls back to legacy `universe_ohlcv.pkl`).
- Prints cache path, ticker count, and SPY last-bar date.
- Halts if ticker count < 11,200.

## Details you need to know

- Visual design tokens copied verbatim from the regime-meter worktree at `swing-screener-regime-meter/regime_meter/dashboard/colors_and_type.css`. TC2000-flavored: pure black canvas, thin Win9x gray-gradient chrome strips, bright green/red candles, light-gray price axis, cyan/gold accents, sharp corners (no border-radius), Segoe UI for chrome, Consolas for numbers.
- Indicator math reuses `local_runner/vectorized_indicators.py` (`sma_2d`, `ema_2d`, `macd_2d`, `atr_2d`) for consistency with the rest of the project.
- Generation is on-demand only — not wired into `nightly.py`. The pipeline does not consume the dashboard output. Regenerate when you want a fresh view.
- Tickers can appear in multiple themes; overlap is expected and intentional (e.g., MRVL in optics, AI compute, AI connectivity).
- Tickers in the theme map but missing from the OHLCV cache are reported on stdout and silently dropped from that theme's composite. The dashboard's universe-summary count reflects what's actually placed vs ungrouped vs missing-from-cache.
- Theme rendering requires the composite to have at least 51 bars (for the TC2000 RS formula's `C50_ago` reference and ATR50).
- The TC2000 RS PCF is scale-invariant: when the composite is normalized to start at 100, the price-per-ATR multiplier still produces meaningful values because numerator and denominator scale together.
- Initial sort is 5d RS desc; user can re-sort interactively by clicking any column header.

## Known bugs

- Tickers stored in the OHLCV cache under non-dot variants (e.g., `BF.B` lookup misses; the cache key is `BF-B`) are reported as missing. No automatic re-key fallback yet.

## Pending research

**Theme narrative writeup + per-ticker rationale for non-obvious placements.**

Each theme in `local_runner/theme_map.py` needs a one-line `narrative`
field describing the trader story behind the basket (not the GICS bucket).
Hand-written, evidence-based. Examples of the right tone:
- `ai_optics`: "800G/1.6T transceivers and optical components feeding
  hyperscaler AI buildouts — beneficiaries of every step-up in DC bandwidth."
- `nuclear_renaissance`: "AI power demand + Big Tech PPAs reviving nuclear
  baseload; SMR pure-plays the speculative leg."
- `bitcoin_treasury`: "Equity proxies for Bitcoin via balance-sheet hoarding;
  convertible-debt leverage on BTC price."

For non-obvious cross-narrative placements (MOD in datacenter_buildout,
TEM in ai_apps_platforms, IREN in neoclouds, TLN in neoclouds, MPWR in
power_demand_for_ai, etc.) — add an inline `# rationale: ...` comment
next to the ticker in `theme_map.py` explaining why it fits despite its
GICS classification. Obvious placements (NVDA in ai_compute, FSLR in
solar) need no comment.

Authoritative source for narrative: WebSearch on the theme name + recent
catalysts (e.g., "neoclouds CoreWeave Stargate," "AI optics 800G
transceiver," "uranium China export tariff trade"). Cite the source in
the narrative or rationale. Zero confabulation.

**Priority order** — narrative-heaviest themes first: AI buildout cluster,
crypto, nuclear/uranium, critical minerals, biotech sub-baskets, defense,
quantum, drones. Industry-shaped themes (restaurants, airlines, packaging)
last — the existing GICS validator is mostly sufficient there.

## Pending build

1. **`scripts/fetch_company_meta.py`** — mirrors `scripts/fetch_fundamentals.py`
   (Yahoo `quoteSummary` / `assetProfile` module) but saves `longName` +
   `longBusinessSummary` per ticker. Runs over `theme_map.UNIVERSE` only
   (~470 tickers, ~8-10 min with rate limiting). Output:
   `local_runner/cache/company_meta.json` keyed by ticker as
   `{ticker: {longName, longBusinessSummary}}`.

2. **`local_runner/theme_map.py` schema bump** — add `THEME_NARRATIVES`
   dict (theme_key → 1-2 sentence narrative string) alongside the existing
   `THEMES` and `THEME_LABELS`. Loaded by `theme_dashboard.py` and rendered
   under the theme title annotation.

3. **Dashboard UI — `local_runner/theme_dashboard.py`:**
   - **Member mini-chart card** — extend `build_mini_svg()` so the SVG
     header shows `TICKER` on line 1 and the `longName` (truncated to card
     width) on line 2. Add an SVG `<title>` element containing the first
     1-2 sentences of `longBusinessSummary` so mouse hover surfaces it as
     a native tooltip.
   - **Composite chart title overlay** — extend `build_composite_figure()`
     so the existing top-left title annotation gets a third line beneath
     the member-list with the theme narrative.

4. **Validator extension** — when `company_meta.json` is present,
   `validate_theme_sectors()` prints `longName` + first sentence of
   `longBusinessSummary` alongside each ticker so manual review is
   evidence-based rather than label-based.

**Out of scope (do not build):**
- News fetchers
- Catalyst calendars
- Earnings tracking
- Per-ticker research reports
- Sentiment scoring
- Trade-management features (entries, exits, position sizing, ADR-based logic, squeeze mechanics, setup classification)

The dashboard's purpose is **identity + narrative surfacing**, not a
research terminal or a trade-management tool. Dan handles all trade
decisions himself.
