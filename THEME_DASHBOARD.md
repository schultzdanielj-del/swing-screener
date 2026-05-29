# THEME_DASHBOARD

## Purpose

Browse the active-trading universe organized into market themes (Optics, Space, Memory, Cybersecurity, Uranium, Drones, Batteries, AI Compute, Neoclouds, etc.). Each theme produces an equal-weight synthetic composite candle chart plus a grid of member mini-charts. The same chrome hosts three pages cycled via the header brand: **Themes** (theme tree), **Tickers** (flat per-ticker table), and **Setups** (scanner-style flat table of pattern matches, with setup-type tabs — Extension Peek, First Flags, and Tightening Range). Output is a single self-contained HTML file so the dashboard runs in any browser — no server, no PySide6 launch — and one row at a time is active with arrow-key navigation through a sortable watchlist sidebar.

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
- **Relative Strength PCF** in seven windows: **0D** (intraday today) + 1d / 5d / 20d / 65d / 130d (day-over-day):
  - **Multi-bar windows (1d / 5d / 20d / 65d / 130d):**
    - `avg = mean over last N bars of (close / prev_close − 1) × 100` — day-over-day return, NOT same-day close/open. A gap-up-and-fade that still closes green counts as a positive day, matching end-of-day P&L.
    - `mult = ((close + close_50_bars_ago) / 2) / ATR50` (TC2000 PCF multiplier).
    - `theme_RS = avg × mult`.
  - **0D (intraday today):**
    - Same formula but the "today's strength" term is single-bar `(close / open − 1) × 100`. Captures intraday participation (who controlled the tape today) instead of day-over-day P&L.
  - **Benchmark = equal-weight Universe composite**, NOT SPY. SPY is cap-weighted and dominated by mag7, which is structurally inconsistent with the equal-weight theme composites. The Universe composite is built once from all `UNIVERSE` tickers using the same `build_composite` machinery the themes use. Self-contribution from a theme's own members into the denominator is accepted — even the largest themes are ≤ ~3% of UNIVERSE.
  - **Ratio uses abs() in the denominator:** `displayed = theme_RS / abs(bench_RS)`. Sign of the displayed ratio always follows the theme's own raw RS direction — without abs(), a negative-benchmark window (broad gap-up-and-fade) would flip every theme's sign. With abs(): positive ratio = theme outperformed in the same direction as universe magnitude; negative = theme underperformed.
- **MACD-line divergences** (price pivots paired with EMA6 − EMA20 pivots):
  - Pivot window 3 bars on each side; lookback 240 bars; minimum spacing 6 bars; MACD-pivot offset tolerance ±10 bars.
  - For each price pivot, scan backward through prior price pivots and accept the FIRST pair whose connecting trendline is *not pierced* by an intervening bar (low must not break a bullish trendline; high must not break a bearish trendline). One divergence per anchor pivot.
  - Both bullish (price LL, MACD HL) and bearish (price HH, MACD LH) are detected.

Composite chart (interactive Plotly, one per theme):
- Three panels: candles + SMAs (62%), volume (13%), MACD 6/20/9 (25%).
- (Per-ticker chart — separate template used when a single ticker row is active — has **four** panels: candles (50%), volume (10%), MACD (18%), "X ADR to 50sma" extension panel (22%). The extension panel shows the ext50 series as a green/red histogram plus any unbroken descending u1/u2/u3 trendlines from the day's snapshot drawn as line overlays.)
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
- SMAs 5/10/20/50/200 drawn in the same palette as the composite chart (5 orange, 10 cyan, 20 beige, 50 yellow, 200 white), computed on full history then sliced to the visible window and clipped to the chart rect — a long MA that runs off the bottom in a strong trend simply disappears rather than compressing the candles. The longest MAs are skipped for tickers with too little history (e.g. SMA200 on a sub-200-bar IPO). No axis labels.

HTML shell:
- Fixed gray-gradient top header with cells: brand, generated timestamp, bar count, theme count, cache last-bar date, SPY 5d return + ADR, sort label, position indicator, navigation hint, universe summary.
- Sticky left sidebar holds a **sortable watchlist table** (320px wide). Columns: Flag, Theme, 1d RS, 5d RS, 20d RS, 65d RS, 130d RS, N. Click any column header (except Flag) to sort by it; click again to flip direction. Active column shows ▾/▴. Theme name colored red when the composite is below its 200-day SMA. The Flag column holds a clickable triangular icon per theme — click toggles flagged state (outlined gray = off, filled cyan = on); right-click anywhere in the flag column opens a small `Unflag all` context menu. Flagged set persists in `localStorage` under `themeDashboard.flaggedThemes`.
- Watchlist controls bar: four checkboxes + visible-row count + filter gear icon.
  - `Hide < 200D` — **checked by default** so below-200D themes start hidden.
  - `Tight D1` — hides rows whose today candle range / 20d ADR is ≥ 1.10. Unchecked by default.
  - `Near 50SMA` — **Tickers view only.** Hides ticker rows whose distance from the 50-day SMA (in ADRs — the same `ext50` metric the Setups page uses, positive = above the 50) is outside −2.0 to +4.1. Theme rows, theme-tree child rows, and the Setups view are exempt. Sub-50-bar tickers (no computable ext50) are hidden when the filter is on. Unchecked by default.
  - `Flagged` — hides rows that do not belong to any flagged theme. Unchecked by default. When checked with zero flags, nothing visible (intentional).
- Filter gear icon opens a slide-out filter panel with three sections:
  - **Sectors** — checkbox per GICS sector across the universe; unchecked = excluded.
  - **Themes** — checkbox per theme; unchecked = excluded. Includes a search box.
  - **Strength** — two parallel sets of checkboxes, one window per row:
    - **Hot N** = rsN ≥ 1.20 (Hot 0D / Hot 5 / Hot 20 / Hot 65 / Hot 130)
    - **Cold N** = rsN < 1.20 (Cold 0D / Cold 5 / Cold 20 / Cold 65 / Cold 130)
    - **AND semantics across all checked boxes** (Hot + Cold can mix; e.g. Hot 0D + Cold 65 = intraday-strong AND quarter-weak — a fade/pump-and-dump candidate).
    - Missing rsN sentinel −1e9 fails any Hot check; trivially passes any Cold check (don't hide rows with no data from a weakness scan).
    - Persisted per-window in `localStorage`.
- **Sidebar width = 600px** (480px under 1200px viewport). Sized to fit Flag + Theme/Ticker + 0D/1d/5d/20d/65d/130d + Comp + N columns without truncation.
- **Compression column "Comp N"** (theme rows + ticker-flat rows): N-bar range / 20-bar ADR. `comp_N = (maxH[-N:] / minL[-N:] − 1) × 100 / ADR20%`. Lower = tighter consolidation. Period picker: **left-click sorts**, **right-click pops up a radio menu** (3 / 5 / 10 / 20 / 30 bars). Default period = 10, persisted in `localStorage`. Lines that can't be computed get sentinel `1e9` so they sort to the bottom on ascending (tightest-first) sort.
- **Tickers view** (second sidebar pane, toggled via the brand header): flat sortable table, one row per ticker that has a price pack. Columns: Ticker, Theme membership, 0D, 1d, 3d, 65d, 130d RS, Comp, ADR. All rows visible by default; the Hide / Tight / Near 50SMA / Flagged / Hot N / Cold N checkboxes hide rows via inline `display` styles (Near 50SMA acts on this view only). Carries `data-theme-ids="t1,t2,..."` per row so the Flagged filter can match any theme the ticker belongs to.
- **Setups view** (third sidebar pane, toggled via the brand header — cycle is Themes → Tickers → Setups → Themes): a flat sortable table of pattern matches, with **setup-type tabs** at the top (`Extension Peek` / `First Flags` / `Tightening Range`). A single click switches the table to that setup's own columns; the active setup is remembered in `localStorage`. Tightening Range additionally carries a `[D][W][M]` sub-toggle to flip the visible timeframe in place. Every Setup row carries the SAME per-ticker data attrs as the Tickers view, so the existing Hide / Tight / Flagged / Sector / Theme / Hot / Cold filters all apply uniformly (Near 50SMA stays Tickers-only). Each setup's definition and columns are in the "Setups page" section below.
- Only the active theme's section is visible at a time; all others have `display: none`. Click a row → activate that theme. Click a column header → resort the table.
- Keyboard navigation: ← / ↑ / k / PageUp = previous visible row; → / ↓ / j / Space / PageDown = next visible row; Home / End = first / last visible. The sequence respects the current sort and filter.
- URL hash updates on each navigation (e.g., `#optics_photonics`) for deep-linking.
- Each chart section has its own thin pure-black info bar above the chart: `RS 5d vs SPY` cell, `vs 200D` cell, optional `BULL DIV` / `BEAR DIV` tags (with ×N count if more than one), date, OHLC, Chg, Chg%, Vol, APTR, SMAs values.
- Fixed gray-gradient status bar at the bottom: live indicator, cache date, active theme label.
- All CSS inlined. Plotly.js loaded from cdn.plot.ly. No other external assets.

Setups page (setup-type tabs — `Extension Peek` / `First Flags` / `Tightening Range`, single click to switch; the table below swaps to that setup's columns, the active tab is remembered in `localStorage`, and each setup keeps its own default sort. Tightening Range carries its own internal `[D][W][M]` sub-toggle):

**Extension Peek** — Columns: Ticker, Theme(s), Line (u1/u2/u3), |Peek|, Yest sd, Drop, 0D, 1d, Comp10, ADR. Default sort: tightest |Peek| first (ascending).
- **Definition:** a ticker whose intraday close just crossed above an unbroken descending 50-SMA-extension trendline that survived a strict break-enforcement filter. The setup historically rips the next day even when the price-chart signal looked ambiguous on the day (the extension chart's resistance break is the "tell").
- **Inputs:** reads `local_runner/cache/ext50_trendline_snapshots.json` (produced by `local_runner/ext50_trendline_snapshot_builder.py` — see `EXT50_TRENDLINE_SNAPSHOTS.md`). The snapshot fixes the day's u1/u2/u3 line equations as of the most recent EOD bar; the dashboard projects each line forward one bar and compares against today's live ext50 (computed in-process from the intraday OHLCV cache).
- **Peek detection (per ticker, per slot u1/u2/u3):**
  - `proj_today = v1 + slope × (today_bar − i1)` — project the snapshot line to today.
  - `today_sd = proj_today − today_ext` (locked sign convention from `scripts/ext50_trendlines.py`).
  - Peek = `today_sd < 0 AND yest_sd ≥ 0` — today's price above the line, yesterday's price at-or-below. First slot that qualifies wins; the ticker shows up once.
- **Live ext50 formula** (must match the snapshot builder):
  - `adr20 = mean((H / L − 1) × 100)` over the last 20 bars.
  - `ext50 = ((close − SMA50) / SMA50 × 100) / adr20` — extension expressed as "ADRs above SMA50."
- **Snapshot rebuild semantics:** trendlines do NOT re-fit during the trading day. The intraday bar is partial and is not allowed to create new pivots. The snapshot builder runs at the start of every `_build_html_to_disk` call (via direct `build()` import from `ext50_trendline_snapshot_builder`); intraday refreshes and ad-hoc rebuilds within the day can pass `--skip-snapshot` to reuse the morning's lines.
- **No 200-SMA gate.** Tested — APP's canonical "perfect peek" (2026-05-26) was below its 200-SMA at the time; the gate would have hidden it. Setups stays open; the user vets via the chart.

**First Flags** — Columns: Ticker, Theme(s), Bottom (divergence-bottom date), B<200% (how far below the 200-SMA the bottom closed), Pole% (move from the bottom low to the highest high since the stack formed), Days (trading bars since the bottom), Pullback% (how far the live close sits below that pole high), 0D, 1d, Comp10, ADR. Default sort: freshest bottom first (Days ascending).
- **Definition** (a bottom-reversal continuation candidate; all conditions must hold): the most-recent bullish MACD 6/20-line divergence bottomed with its anchor low **below the 200-SMA** and at least **2% below the prior pivot low** (a real lower-low); after the bottom the **10/20/50 SMAs stacked in order** (10 > 20 > 50 — the trend, no fixed % move) and are **still stacked now**; there have been **at most 2 swing highs since the stack formed** (the first/early flag, not a multi-leg runner); and price is currently **below the highest high since the stack** (pulled back), **riding the fast MA** (close within 5% of the 10 or 20 SMA).
- **Inputs:** reads `local_runner/cache/first_flags_snapshots.json` (produced by `local_runner/first_flags_snapshot_builder.py` — see `FIRST_FLAGS_SNAPSHOTS.md`). The snapshot fixes the bottom / stack / divergence at the most recent EOD bar; the dashboard's `compute_first_flags` only refreshes the live pullback against today's close — no divergence re-detection during the day.
- **Tuned for recall.** The scan only needs to surface a name on SOME day during its flag; the user takes it to a TC2000 watchlist and confirms the entry on the chart. The `-2%` lower-low, `≤2` swing-highs, and `5%` riding band were derived from a labeled A+/fail set, not eyeballed; some borderline names pass and are sifted by eye.

**Tightening Range** — Columns: Ticker, Theme(s), TF, Range lo, Range hi, Width (band in ADRs), Apex (bars to the lines' meeting point), Span (bars in the wedge), 0D, 1d, Comp10, ADR. Default sort: tightest band first. The setup carries a `[Daily] [Weekly] [Monthly]` sub-toggle inside the tab — single click to switch which timeframe's matches show. The choice is remembered in `localStorage`.
- **Definition.** A contracting triangle / converging wedge on the selected timeframe — the price envelope tapers over time. Specifically: when the lookback window is split into thirds, the 90th-percentile of highs in the last third is *below* the 90th-percentile of highs in the first third (highs descended), the 10th-percentile of lows in the last third is *above* the 10th-percentile of lows in the first third (lows ascended), and the last-third range is at most 70% of the first-third range (≥30% contraction). Price must be inside the recent envelope, and the apex (where the lines would meet) must not point down. The detector sweeps multiple window lengths and takes the tightest one that qualifies.
- **Shape-based, not trendline-fitting.** The match rule looks at the *shape* of the price envelope (percentile bounds per segment), not at specific trendline fits — earlier line-fitting attempts overfit and either missed obvious triangles or stitched fake wedges from choppy pivots. The trendline values shown on the table (range lo / hi, apex bars) are coarse approximations from segment-center anchors, intended only to give the table something to sort and the user something to glance at. The actual lines + entry are drawn on the chart in TC2000.
- **Inputs:** reads `local_runner/cache/tightening_range_snapshots.json` (produced by `local_runner/tightening_range_snapshot_builder.py` — see `TIGHTENING_RANGE_SNAPSHOTS.md`). The producer computes daily / weekly / monthly per ticker, so the toggle is instant — no recompute on switch.
- **Tuned for recall, you sift.** Sloppy contractions whose envelope happens to taper (e.g. choppy ranges near the highs) still pass; they're accepted as sift noise. Flat-top "key level" setups are *not* matched here — they're a separate planned setup.

Ungrouped section:
- Computed from `UNIVERSE` — any ticker in the universe list that is not in any theme is rendered as a final section at the bottom titled "Ungrouped", with member mini-charts only (no composite). The watchlist row for it is highlighted gold.
- Tickers missing from the OHLCV cache (e.g., `BF.B` → cache key `BF-B`) are reported on stdout and counted in the header's universe summary but not rendered.

Cache rules applied at startup:
- Cache source selection: if `local_runner/cache/universe_ohlcv_daily_intraday.pkl` exists AND its mtime is newer than `local_runner/cache/universe_ohlcv_daily.pkl`'s mtime, load the intraday pickle. Otherwise load `universe_ohlcv_daily.pkl` (legacy `universe_ohlcv.pkl` as last fallback). `load_daily_cache()` returns `(cache, source_meta)` where `source_meta["source"]` is `"intraday"` or `"main"` and downstream renderers branch on it.
- Prints cache path, ticker count, source label, and SPY last-bar date.
- Halts if ticker count < 11,200.
- Header "Cache Last Bar" cell: renders `YYYY-MM-DD (intraday 4:20pm)` in gold accent when source = intraday; plain `YYYY-MM-DD` when source = main.

Intraday refresh (`local_runner/intraday_refresh.py`):
- Purpose: an after-close glance at where the day landed, ~20 minutes after the regular session close, before the next morning's nightly bulk EOD lands. Statistical / informational only — no commentary, no signals.
- Scope: the theme dashboard universe only — union of `THEMES` ticker lists, `UNIVERSE` list, and `BENCHMARK_TICKERS` (currently `["SPY"]`). SPY is included even though it isn't a theme member because the dashboard reads it directly for the header date and every theme/SPY TC2000 RS ratio.
- Source: EODHD `/real-time/` endpoint, batched 18 tickers per call. Roughly 51 API calls per run, ~50–100 quota total, well inside the standard tier's 1,000-req/min HTTP cap.
- Last-bar substitution: for each ticker with a valid quote, if the main pickle's last bar date matches today (America/New_York), overwrite O/H/L/C/V in place; otherwise append a new row at today's date. Tickers in the theme universe but missing from the main pickle (e.g., `BF.B` vs `BF-B`) are reported and skipped.
- Output: `local_runner/cache/universe_ohlcv_daily_intraday.pkl` — full ticker dict copy of the main pickle with theme universe tickers mutated. Atomic write via `.tmp` + rename. Sidecar `universe_ohlcv_daily_intraday.meta` JSON file carries `{snapshot_et, written_at, quotes_received, bars_updated, bars_appended, label}`.
- Failure modes: EODHD unreachable, token rejected, sub-70% quote success rate, or every quote matching the prior close (likely closed market) → log, exit non-zero, do NOT touch the intraday pickle. Dashboard then continues to read whichever pickle is currently newer.
- Dashboard regen: on success the script invokes `theme_dashboard.main()` directly with `--bars 250` (no `--open`), so the HTML is rewritten against the freshly-mutated pickle in the same process.
- Reconciliation: the next morning's nightly overwrites `universe_ohlcv_daily.pkl` with the official MOC close; the intraday pickle is then older than main and silently ignored — source-of-truth remains nightly.

Scheduled task (Windows Task Scheduler):
- Task name: `ScanPerfect Intraday Refresh`. XML template at `local_runner/cache/intraday_refresh_task.xml` (mirrors the existing two task XMLs; gitignored alongside them).
- Trigger: daily at 16:20 local (ET on the host).
- Action: `cmd.exe /c python local_runner\intraday_refresh.py > local_runner\cache\intraday_log.txt 2>&1`, working directory = repo root.
- `MultipleInstancesPolicy=StopExisting`, `ExecutionTimeLimit=PT30M`, `StartWhenAvailable=true`, `LogonType=InteractiveToken`. Matches the footgun-free pattern used by `ScanPerfect Nightly Refresh` and `ScanPerfect Theme Dashboard`.
- Register on a new host with: `schtasks /Create /XML local_runner\cache\intraday_refresh_task.xml /TN "ScanPerfect Intraday Refresh" /F`.

## Details you need to know

- Visual design tokens copied verbatim from the regime-meter worktree at `swing-screener-regime-meter/regime_meter/dashboard/colors_and_type.css`. TC2000-flavored: pure black canvas, thin Win9x gray-gradient chrome strips, bright green/red candles, light-gray price axis, cyan/gold accents, sharp corners (no border-radius), Segoe UI for chrome, Consolas for numbers.
- Indicator math reuses `local_runner/vectorized_indicators.py` (`sma_2d`, `ema_2d`, `macd_2d`, `atr_2d`) for consistency with the rest of the project.
- Optional input: `local_runner/cache/company_meta.json` (produced by `scripts/fetch_company_meta.py`). When present, the dashboard surfaces per-ticker `longName` and `longBusinessSummary` in three places: the mini-card SVG header (longName as the second line, longBusinessSummary as a native `<title>` hover tooltip), the validator output (longName + first sentence of longBusinessSummary printed beneath each flagged outlier), and indirectly in the composite chart via `THEME_NARRATIVES` (which is written by hand using the per-ticker summaries as source material). When `company_meta.json` is absent, every consumer falls back gracefully to its pre-meta layout.
- Sentence splitter for SVG hover tooltips + validator output masks common company-suffix abbreviations (`Inc.`, `Corp.`, `Ltd.`, `N.V.`, `Co.`, `LLC.`, `PLC.`, etc.) before splitting on `.!?` so the first real sentence survives instead of getting clipped at the company name's period.
- Generation is on-demand only — not wired into `nightly.py`. The pipeline does not consume the dashboard output. Regenerate when you want a fresh view.
- **Step 0 of every dashboard build is the snapshot rebuild** — first the ext50-trendline snapshot (`ext50_trendline_snapshot_builder.build()`, ~2 min with multiprocessing), then **Step 0b** the First Flags snapshot (`first_flags_snapshot_builder.build()`, well under a minute), then **Step 0c** the Tightening Range snapshot (`tightening_range_snapshot_builder.build()`, well under a minute). All three are called directly. Pass `--skip-snapshot` for fast interactive rebuilds when the morning snapshots are still valid for the day. `intraday_refresh.py` passes `--skip-snapshot` automatically (the 4:20 PM refresh doesn't change the day's pivots — see EXT50_TRENDLINE_SNAPSHOTS.md, FIRST_FLAGS_SNAPSHOTS.md, and TIGHTENING_RANGE_SNAPSHOTS.md for the locked "snapshot stays fixed for the trading day" semantic).
- Tickers can appear in multiple themes; overlap is expected and intentional (e.g., MRVL in optics, AI compute, AI connectivity).
- Tickers in the theme map but missing from the OHLCV cache are reported on stdout and silently dropped from that theme's composite. The dashboard's universe-summary count reflects what's actually placed vs ungrouped vs missing-from-cache.
- Theme rendering requires the composite to have at least 51 bars (for the TC2000 RS formula's `C50_ago` reference and ATR50).
- The TC2000 RS PCF is scale-invariant: when the composite is normalized to start at 100, the price-per-ATR multiplier still produces meaningful values because numerator and denominator scale together.
- Initial sort is 5d RS desc; user can re-sort interactively by clicking any column header.
- Shareable JSON export of `theme_map.py` is produced by `scripts/export_theme_map_json.py` → `local_runner/cache/theme_map_export.json`. Schema: `{schema_version, generated_at, themes: {<theme_id>: {label, narrative, members: [{ticker, rationale?}]}}, universe: [...]}`. Re-run after any `theme_map.py` edit. The output is gitignored (cache dir).

## Known bugs

- Tickers stored in the OHLCV cache under non-dot variants (e.g., `BF.B` lookup misses; the cache key is `BF-B`) are reported as missing. No automatic re-key fallback yet.

## Pending research

None currently active.

## Pending build

**Market index internals + composite score tab.** A second tab/page
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

---

**Add theme entries for the residual ungrouped clusters.** The full per-ticker audit (applied 2026-05-24 via `scripts/apply_audit_to_theme_map.py`) wrote 331 additions + 24 removals to `theme_map.py` and left 64 tickers in Ungrouped. Almost all of those are the audit's "no theme captures X" flags — coherent clusters with no existing home: coal (AMR, BTU, CNR, HCC), recreational vehicles / powersports (BC, HOG, PATK, PII, THO), edtech (COUR, LOPE, LRN), auto dealers (ABG, AN, GPI), fitness (LTH, PLNT, PTON), meat processing (JBS, PPC), packaged foods (FLO, MZTI, VITL), construction rental (EQPT, HRI), alcoholic beverages (BF.B, SAM), China ADRs (BZ, HAO), non-BTC digital-asset treasury (PURR, SBET). Audit reasoning per ticker lives in `local_runner/cache/theme_placement_output.json[].theme_reasoning`. Adding a theme = three small inserts in `theme_map.py` (`THEMES`, `THEME_LABELS`, `THEME_NARRATIVES`) plus the member tickers. No script work needed.

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
