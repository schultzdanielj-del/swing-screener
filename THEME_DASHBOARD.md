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

**Full per-ticker theme placement audit — investigation pass complete; follow-up workstreams remain.**

Resumable, multi-session curation pass over every ticker in the dashboard
universe. Goal: every placement is the product of actual business
understanding + recent-news context, not Yahoo-summary keyword
matching. Existing placements get validated; ungrouped names get
placed where a single dominant narrative exists; cross-listings get
added where peer-theme parity warrants. Truly diversified names stay
ungrouped *with story explaining why* — never silent-skipped.

The investigation pass is complete. Authoritative output lives in
`local_runner/cache/theme_placement_output.json` (one record per
ticker, schema below). Two follow-up workstreams remain before audit
results land in `local_runner/theme_map.py`:

1. **Apply B-bucket cross-listings.** Append each ticker to its
   `secondary_themes_add` themes in `theme_map.py`, with a rationale
   comment matching `theme_reasoning`. Mechanical; no judgment calls.

2. **Resolve C-bucket flags with Dan.** Each C row carries a proposed
   primary-theme change or cross-listing removal. Surface one at a time;
   apply only after explicit go-ahead.

Bucket A and D do nothing further.

### Why this exists

Earlier auto-classification passes used industry-signature + keyword
matching against business summaries. Two failures that drove the
rewrite:

- **NOK** was missed entirely. Yahoo summary leads with "mobile network
  solutions," so keyword matching gave up. The actual story — Feb 2024
  Infinera acquisition ($2.3B) added ICE6 800G + ICE7 1.6T coherent
  optical engines, making Nokia top-2 coherent transport vendor
  alongside CIEN — required web search for recent corporate actions,
  not just summary reading.
- **AAP** was placed shallowly. The first auto-pass would have given
  "aftermarket parts retailer" as the rationale. The full story is the
  Pro/DIFM channel mix shift, Worldpac divestiture late 2024, ~500
  store closures, market-hub regional distribution rollout — and the
  fact that those moves push the business model TOWARD LKQ's wholesale
  positioning, tightening the `auto_parts_tech` placement.

The lesson encoded in the methodology: **even "obvious" placements get
web-searched. No punting.**

### State files

All under `local_runner/cache/` (gitignored alongside other working
cache):

| File | Schema | Purpose |
|---|---|---|
| `theme_placement_input.json` | `{records: [{ticker, long_name, sector, industry, business_summary, current_themes, currently_ungrouped}], theme_context: {themes: {theme_id: {label, narrative, members}}}}` | Frozen snapshot of every ticker + the theme structure at audit start. Read-only after creation. |
| `theme_placement_queue.json` | `{remaining: [ticker, ...], processed: [ticker, ...]}` | Workflow state. `remaining` is processed head-first; ungrouped tickers come first, then existing placements (for validation pass). |
| `theme_placement_output.json` | `{placements: [<placement record>, ...]}` | Appended one placement at a time. Each entry is a structured record (see schema below). |

The input file is built once via the helper at the start of
`scripts/propose_theme_placements.py` (or equivalent inline script); it
is NOT regenerated mid-audit so the dataset stays consistent across
sessions.

### Per-ticker investigation pipeline — locked, no shortcuts

Every ticker, every time. Even when the Yahoo summary looks
unambiguous. Even on the validation pass for already-placed tickers.

1. **Read the full `business_summary`** from the input file. Not the
   first sentence. Most descriptions bury segment-level revenue and
   strategic story below the lead.
2. **Read `sector` + `industry`** for baseline classification.
3. **Web search 1 — segments / revenue / financial state.** Query:
   `"<ticker> business segments revenue 2025 2026"`. Surfaces actual
   revenue mix, current strategic narrative, recent earnings
   trajectory. Mandatory on every ticker. No exceptions.
4. **Web search 2 — strategy / corporate actions.** Query:
   `"<ticker> strategy pivot acquisition 2025 2026"`. Catches M&A,
   divestitures, management changes, segment exits, regulatory events.
   Mandatory on every ticker. No exceptions.
5. **Optional WebFetch into SEC filings / press releases / earnings
   transcripts.** Only when steps 3-4 surface a major action that needs
   verification, OR when the search results are marketing-fluff and you
   need actual segment-revenue percentages. Skip when not needed.
6. **Peer-theme check.** Look at which themes currently roster the
   company's closest competitors. AAP → LKQ in `auto_parts_tech`. NOK →
   CIEN in `ai_optics` + `ai_networking`. Peer parity is the strongest
   single signal for narrative fit when it exists.
7. **Reason through theme candidates against narrative-level criteria.**
   What drives the stock for the next ~24 months? What's the secular
   tailwind? Bull case / bear case? Use the `theme_context.themes` map
   in the input file as your reference for what each theme captures.
8. **Self-critique.** Identify the runner-up theme. Why does it lose?
   If the runner-up is genuinely competitive (~80% as good a fit),
   either propose a cross-listing OR downgrade confidence and route to
   bucket C (review).

### Output schema (one record per ticker)

| Field | Type | Required | Notes |
|---|---|---|---|
| `ticker` | string | yes | |
| `current_themes` | string[] | yes | Whatever was in input. Empty list if ungrouped. |
| `proposed_themes` | string[] | yes | Final placement. Empty list = stays ungrouped. May contain >1 entry for cross-listings. |
| `bucket` | string | yes | One of A / B / C / D / E (see below). |
| `confidence` | string | yes | `high` / `medium` / `low`. |
| `runner_up` | string \| null | yes | Theme ID of the second-best fit, or null if none competitive. |
| `company_story` | string | yes | 200-400 words. Positioning in industry, ownership / history relevant to narrative, recent-news context (1-2 years), bull / bear case as currently traded. **This is the quality bar.** Anything shorter or shallower is a failure. |
| `theme_reasoning` | string | yes | 50-150 words. Why this theme, why not the runner-up. Explicit peer-theme citation when applicable. |
| `investigation_sources` | string[] | yes | Which steps from the pipeline were actually used. Example: `["yahoo_summary", "industry_classification", "peer_theme_check (LKQ in auto_parts_tech)", "web_search: segments revenue 2025 2026", "web_search: strategy pivot acquisition 2025 2026"]` |

### Bucket definitions

| Bucket | Meaning | Auto-apply on completion? |
|---|---|---|
| **A** — `A_ungrouped_to_theme` | Was ungrouped; you're proposing single-theme placement | Yes |
| **B** — `B_cross_listing_added` | Currently placed in theme X; you're proposing X + new theme Y | Yes |
| **C** — `C_change_requires_review` | Proposed REMOVAL from current theme OR primary-theme CHANGE | No — surfaces to Dan as a review list |
| **D** — `D_confirmed_no_change` | Existing placement is correct, no change. Story still gets written; confirms validation. | No change applied |
| **E** — `E_stays_ungrouped` | Was ungrouped, after investigation no single theme captures it. Story explains why. | No change applied |

### Locked rules

- **Existing placements are a strong Bayesian prior.** If `current_themes` is non-empty, the user has already curated that placement. Only propose REMOVAL or primary-theme CHANGE when evidence is overwhelming (recent business pivot, divested key segment, etc.). Default to confirming existing placements.
- **Cross-listings are allowed and encouraged** when a ticker has real exposure to multiple narratives. See MRVL (ai_compute + ai_connectivity + ai_optics) for the existing pattern. NOK → ai_optics + ai_networking (cross-list parallel to CIEN) is the canonical example added by this audit.
- **No punting on "obvious" cases.** Web search 1 + web search 2 are mandatory on every ticker. Skipping them was the failure mode that produced the shallow first AAP analysis.
- **Truly diversified names stay ungrouped, but the story still gets written.** Bucket E placements must include a `company_story` and a `theme_reasoning` explaining why no single theme captures the dominant exposure.

### Quality bar

The AAP entry in `theme_placement_output.json` is the reference.
Anything shallower than that level of company-story depth + recent-news
detail + peer-theme reasoning is unacceptable. Future sessions can
spot-check by reading `theme_placement_output.json[0]` to recover the
discipline.

### Resume protocol for new sessions

1. Read this section of `THEME_DASHBOARD.md` to recover the methodology.
2. The investigation pass is complete; the queue is empty. New work is
   bucket B application + bucket C review, not new-ticker processing.
   If a future need arises to re-investigate or add new tickers, the
   pipeline below still applies.
3. Read `local_runner/cache/theme_placement_output.json[0]` (the AAP
   entry) as a quality-bar reference before processing any new
   tickers.
4. Read `local_runner/cache/theme_placement_input.json` to load the
   per-ticker dataset + the frozen `theme_context`.
5. Process tickers one at a time per Dan's pace. Never bulk-process
   without an explicit ask. After each ticker: append to output, move
   from `remaining` to `processed`.
6. Bucket B placements append the ticker to each `secondary_themes_add`
   theme list in `local_runner/theme_map.py` with a rationale comment
   matching `theme_reasoning`. Bucket C surfaces as a review list for
   Dan — surface one flag at a time; apply only after he confirms each.

### Anti-patterns (the explicit do-NOTs)

- **No scratch JSONs outside `local_runner/cache/`.** State files live
  there; nowhere else. Memory rule against project-scratch files
  applies.
- **No skipping web search 1 or web search 2.** Even on "obvious"
  cases. The NOK and AAP examples are why.
- **No auto-applying bucket C.** Removals and primary-theme changes
  require Dan's review.
- **No degrading rationale depth below the AAP bar.** If a ticker has
  no notable recent news, the `company_story` still covers positioning,
  history, competitive context, and bull/bear case at the AAP depth.
- **No bulk-processing without explicit ask.** Default to one ticker at
  a time so token usage spreads across sessions.

### Token budget per ticker

Rough estimate at the locked pipeline depth:

| Component | Tokens |
|---|---|
| Read input record + theme context | ~500 |
| Web search 1 (results + analysis) | ~3,000 |
| Web search 2 (results + analysis) | ~3,000 |
| Optional WebFetch (when used, ~30% of tickers) | ~3,000 |
| Reasoning + self-critique | ~1,500 |
| `company_story` + `theme_reasoning` output | ~700 |
| **Per ticker total** | **~8-11K tokens** |

Across 918 tickers: ~7-10M tokens spread across multiple Max sessions.
Pace dictated by user; no auto-pacing.

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
