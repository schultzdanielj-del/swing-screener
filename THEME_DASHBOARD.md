# THEME_DASHBOARD

## Purpose

Browse the active-trading universe organized into market themes (Optics, Space, Memory, Cybersecurity, Uranium, Drones, Batteries, AI Compute, Neoclouds, etc.). Each theme produces an equal-weight synthetic composite candle chart plus a grid of member mini-charts. Output is a single self-contained HTML file so the dashboard runs in any browser — no server, no PySide6 launch — and one theme is shown at a time with arrow-key navigation through a sortable watchlist sidebar.

## EXACT spec

Generator: `local_runner/theme_dashboard.py`.
Theme map: `local_runner/theme_map.py` — `THEMES` dict (theme key → ticker list), `THEME_LABELS` dict (theme key → display label), `THEME_NARRATIVES` dict (theme key → 1-2 sentence trader narrative, source-cited via `# source: <url>` comment above each entry), and `UNIVERSE` list (the full hand-picked universe). Edited by hand. Non-obvious cross-narrative ticker placements carry inline `# rationale: <reason>` comments grounded in each company's actual business.
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
- **TC2000 RS PCF** in three windows (1-bar, 5-bar, 20-bar):
  - `avg = mean over last N bars of (close/open − 1) × 100`
  - `mult = ((close + close_50_bars_ago) / 2) / ATR50`
  - `theme_RS = avg × mult`
  - SPY RS computed with the same formula. Ratio = `theme_RS / SPY_RS`.
  - 1-bar form collapses to `(close/open − 1) × 100` for the most recent bar, multiplied by the same 50-bar multiplier.
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
- Top-left annotation stack: theme label, "N symbols · equal weight" subtitle, wrapped member list, and — when a narrative exists for the theme in `THEME_NARRATIVES` — a 4th line rendering the trader narrative in gold, positioned below the member list and clamped so a long member list does not push it below the candle panel.

Member mini-chart (hand-built inline SVG, one per member per theme):
- 100 daily bars, single candle panel, black background, ~18% empty space on the right.
- Header strip — gray gradient. Layout depends on whether per-ticker company metadata is available:
  - With `company_meta.json` present: header grows to 38px and shows two text lines on the left (ticker on line 1, truncated `longName` on line 2). The SVG itself includes a `<title>` child element containing the first 1-2 sentences of `longBusinessSummary`, so mouse-hover anywhere on the card surfaces a native browser tooltip.
  - Without `company_meta.json`: header reverts to the original 24px single-line layout (ticker only).
- Last close + day change % always render on the right of line 1 (green if up, red if down).
- No SMAs, no axis labels.

HTML shell:
- Fixed gray-gradient top header with cells: brand, generated timestamp, bar count, theme count, cache last-bar date, SPY 5d return + ADR, sort label, position indicator, navigation hint, universe summary.
- Sticky left sidebar holds a **sortable watchlist table** (320px wide). Columns: Theme, 1d RS, 5d RS, 20d RS, N. Click any column header to sort by it; click again to flip direction. Active column shows ▾/▴. Theme name colored red when the composite is below its 200-day SMA.
- Watchlist controls bar: `Hide below 200D` checkbox (**checked by default** so below-200D themes start hidden) + visible-row count.
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
- Optional input: `local_runner/cache/company_meta.json` (produced by `scripts/fetch_company_meta.py`). When present, the dashboard surfaces per-ticker `longName` and `longBusinessSummary` in three places: the mini-card SVG header (longName as the second line, longBusinessSummary as a native `<title>` hover tooltip), the validator output (longName + first sentence of longBusinessSummary printed beneath each flagged outlier), and indirectly in the composite chart via `THEME_NARRATIVES` (which is written by hand using the per-ticker summaries as source material). When `company_meta.json` is absent, every consumer falls back gracefully to its pre-meta layout.
- Sentence splitter for SVG hover tooltips + validator output masks common company-suffix abbreviations (`Inc.`, `Corp.`, `Ltd.`, `N.V.`, `Co.`, `LLC.`, `PLC.`, etc.) before splitting on `.!?` so the first real sentence survives instead of getting clipped at the company name's period.
- Generation is on-demand only — not wired into `nightly.py`. The pipeline does not consume the dashboard output. Regenerate when you want a fresh view.
- Tickers can appear in multiple themes; overlap is expected and intentional (e.g., MRVL in optics, AI compute, AI connectivity).
- Tickers in the theme map but missing from the OHLCV cache are reported on stdout and silently dropped from that theme's composite. The dashboard's universe-summary count reflects what's actually placed vs ungrouped vs missing-from-cache.
- Theme rendering requires the composite to have at least 51 bars (for the TC2000 RS formula's `C50_ago` reference and ATR50).
- The TC2000 RS PCF is scale-invariant: when the composite is normalized to start at 100, the price-per-ATR multiplier still produces meaningful values because numerator and denominator scale together.
- Initial sort is 5d RS desc; user can re-sort interactively by clicking any column header.

## Known bugs

- Tickers stored in the OHLCV cache under non-dot variants (e.g., `BF.B` lookup misses; the cache key is `BF-B`) are reported as missing. No automatic re-key fallback yet.

## Pending research

None currently active.

## Pending build

**1. Market index internals + composite score tab.** A second tab/page
in the dashboard HTML showing market breadth and a single derived
composite score that compresses internals into one "what's the market
doing right now" number with regime labels. Candidate internals:
advance/decline line, new-highs vs new-lows, percent of universe above
50/100/200-day SMA, McClellan / NYMO, sector-level breadth, RS leaders
and laggards by theme. The composite is statistical (z-scored, weighted,
regime-bucketed) — not a trade signal. Same single-HTML self-contained
delivery model as the existing theme page, served as a second tab
alongside the existing themes view, sharing the same chrome / sidebar /
status bar.

**2. Daily 4:20 PM ET intraday close-snapshot refresh.** A new
`local_runner/intraday_refresh.py` script and Windows scheduled task that
fires daily at 4:20 PM ET, pulls EODHD `/real-time/` quotes for the
theme-dashboard universe only (~915 tickers, not the full 11k OHLCV
cache), and regenerates `theme_dashboard.html` with the live snapshot
substituted into the last daily bar. Purpose: an after-dinner glance at
where the day landed before the next morning's official EODHD bulk close
arrives.

Constraints / design points:
- **Scope: theme-dashboard universe only.** Do not refresh the full 11k
  OHLCV cache here — that's nightly's job. This task touches only the
  ~915 tickers needed by the theme view.
- **EODHD endpoint: `/real-time/{ticker}?s=...` batched at ~15-20 tickers
  per call.** Costs roughly 50-100 API quota per run. Within tier budget
  (the standard All World tier at 100K/day already absorbs ~700 quota
  for nightly with thousands of quota to spare).
- **Storage: separate file, not in-place mutation.** Write a
  `universe_ohlcv_daily_intraday.pkl` (or similar) alongside the main
  pickle. Theme dashboard prefers the intraday pickle when its mtime is
  newer than the main pickle's mtime, otherwise reads the main file.
  Avoids contaminating the official daily cache with synthetic snapshots.
- **Reconciliation: nightly overwrites the synthetic.** Next morning's
  EODHD bulk EOD lands the official MOC auction print into the main
  pickle; the intraday pickle is then older and ignored. Source of truth
  remains nightly.
- **UI marker.** The dashboard header should show that the last bar is
  an intraday snapshot (e.g., "Cache Last Bar: 2026-05-21 (intraday
  4:20pm)") when reading the intraday pickle, so it's never ambiguous
  whether you're looking at synthetic or official data.
- **Scheduled task template.** Same footgun-free pattern as the existing
  two tasks: `cmd /c python local_runner/intraday_refresh.py > local_runner/cache/intraday_log.txt 2>&1`,
  MultipleInstancesPolicy=StopExisting.
- **Failure mode.** When EODHD is unreachable or the live endpoint
  errors, the intraday refresh logs the failure and exits without
  touching the intraday pickle. The dashboard then continues to read the
  most recent successful intraday or the main pickle.

Out of scope for this task: pulling intraday/live for the full 11k
universe; running multiple times per day; bullish/bearish commentary on
the snapshot levels; trade signals.

**Out of scope (do not build):**
- News fetchers
- Catalyst calendars
- Earnings tracking
- Per-ticker research reports
- Sentiment scoring
- Trade-management features (entries, exits, position sizing, ADR-based logic, squeeze mechanics, setup classification, "this is bullish/bearish" commentary on the index-internals score)

The dashboard's purpose is **identity + narrative surfacing + market
regime context**, not a research terminal or a trade-management tool.
Dan handles all trade decisions himself.
