# TIGHTENING_RANGE_SNAPSHOTS

## Purpose

Pre-compute the day's "Tightening Range" matches for the theme-dashboard
universe (`UNIVERSE` in `theme_map.py`) at the most recent EOD bar, on three
timeframes (daily, weekly, monthly). A Tightening Range is a contracting
triangle / converging wedge — the price envelope tapers over time, with the
recent highs lower than earlier highs and the recent lows higher than earlier
lows. The dashboard's Setups page reads the resulting JSON and renders one
list per timeframe via a `[D][W][M]` sub-toggle. The exact trendlines and the
entry are drawn / picked on the chart, not by the scan; the scan only surfaces
candidates.

## EXACT spec

Generator: `local_runner/tightening_range_snapshot_builder.py`.
Output: `local_runner/cache/tightening_range_snapshots.json`.
Inputs:
- `local_runner/cache/universe_ohlcv_daily.pkl` (or `_intraday.pkl` when newer — the partial today bar is dropped before resampling, so weekly/monthly bars never include a half-formed period).
- `local_runner/theme_map.UNIVERSE` — the ticker list.

Invocation:
- `python local_runner/tightening_range_snapshot_builder.py` — runs across UNIVERSE in parallel via `ProcessPoolExecutor` (default workers = cpu_count − 2). Light per-ticker compute; the full UNIVERSE runs in well under a minute.
- No CLI flags. Programmatic knobs: `workers=`, `cache_dir=`, `out_dir=`.
- Called directly from `theme_dashboard._build_html_to_disk` as Step 0c; pass `--skip-snapshot` to the dashboard CLI to bypass when the morning snapshot is still valid.

Per-timeframe detector (function `_fit_wedge`, applied for `tf ∈ {D, W, M}`):
1. **Resample.** Daily bars are used as-is. Weekly is `W-FRI` OHLC aggregation; monthly is `ME` (period-end). The partial intraday daily bar (when the cache is the intraday pickle) is dropped before resampling, so the current week / month bar reflects the EOD-completed days only.
2. **Window sweep.** Per timeframe, try a fixed set of lookback windows — D: `{40, 60, 80, 100, 130}`, W: `{24, 36, 50, 65}`, M: `{18, 28, 40}`. For each window, run the shape screen + line fit below. The match is the window whose **live band is tightest** (the one caught nearest the apex).
3. **Shape screen on each window** (`_check_triangle`, split into thirds). The last ~10% of the window (min 5 bars) is reserved as a breakout zone and excluded from the envelope, so a recent breakout can't inflate the envelope and disguise itself as "inside":
   - **Highs descended:** 90th-percentile of highs in the last third < 90th-percentile in the first third.
   - **Lows ascended:** 10th-percentile of lows in the last third > 10th-percentile in the first third.
   - **Contracting envelope:** `end_range / start_range ≤ 0.85` (≥15% contraction; the floor is loose because the breakout zone is excluded). `range = high_p90 − low_p10` per third.
   - **Not broken out.** The latest close must sit in the lower 80% of the (pre-margin) envelope band — pressing the ceiling means it is breaking out the top, below the floor means it broke down. Additionally the recent (margin) bars must not have poked more than 20% of the band past the envelope. This is what keeps freshly-broken-out names (the canonical reject list) out of the scan.
4. **Live trendlines projected to today** (`_ray_to_today`). The range a trader actually draws — not the wedge's wide mouth:
   - **Resistance** is the ray from the window's highest high that just grazes the highest of the later highs (the upper hull's final segment — the shallowest descending line off the peak that still sits above every later high).
   - **Support** is the mirror: the ray from the lowest low grazing the constraining later lows.
   - Both are read at the current bar to give `res_today` / `sup_today`. Require the lines to still **converge at today** — resistance slope < 0, support slope > 0, and `res_today > sup_today` (apex not yet passed).
5. **Apex direction.** The normalized midline slope of the live lines `((res_slope + sup_slope) / 2) / close ≥ −0.0015` — a mild downward tilt is allowed (resistance falling a bit faster than support rises is still a valid converging triangle), but a clearly down-pointing midline (falling wedge / bear pattern) is rejected.
6. **Tradeable tightness.** The live band `res_today − sup_today` must be ≤ `MAX_BAND_PCT` (25%) of price. A range that is still a quarter-to-half the stock price is too soon / too wide to trade (the stop wouldn't fit) and is dropped.
7. Among the windows that pass all of the above, keep the one with the tightest live band — that's the match.

Percentile bounds (90th / 10th) instead of absolute max / min mean a single wick at the edge of a segment doesn't blow up the shape screen. The trendlines, by contrast, are anchored on the true price extremes and projected forward, because the trader needs the live band at today — the percentile box would report the wedge's old, wide mouth. The window sweep means wedges of different lengths all get a fair look.

Output JSON top-level fields:
- `built_at` — ISO timestamp (UTC).
- `built_in_seconds` — wall time of the build.
- `source_cache` — basename of the OHLCV pickle the build read.
- `intraday_partial_dropped` — `true` when the source was the intraday pickle.
- `pivot_w`, `anchor_k` — legacy fields (kept for snapshot self-description).
- `n_universe` — count of UNIVERSE tickers.
- `n_matches` — count of tickers that matched on at least one timeframe.
- `n_matches_by_tf` — `{D, W, M}` counts.
- `n_errors` — count of tickers that errored.
- `tickers` — dict mapping ticker → `{tf: payload}` for each matched timeframe.

Per-(ticker, timeframe) payload schema:
- `res`, `sup` — the live trendlines projected to today (`res_today` / `sup_today`): resistance and support as they read at the current bar. Approximate (window-dependent); the trader draws the exact lines on the chart.
- `band_pct`, `band_adr` — current live band width (`res − sup`) as a % of price and as a multiple of 20-bar ADR. `band_pct ≤ 25` always (the tightness gate).
- `bars_to_apex` — approximate bars until the live lines would meet.
- `wedge_span` — bars in the matched window.
- `mid_norm` — normalized midline slope of the live lines (≥ −0.0015 always; mild downward tilt allowed).
- `asof_date` — the last bar's date (YYYY-MM-DD) at this timeframe.
- `contraction` — `end_range / start_range` from the shape screen (≤ 0.85 always).
- `window_bars` — which window length matched (e.g. 40 for the tightest daily fit).
- `start_hi`, `start_lo` — first-third percentile bounds (the wedge "mouth").

Partial-bar drop semantics: when the source is the intraday pickle, the producer's worker strips the last (partial today) row before resampling. Weekly / monthly bars then never include a half-formed period. When the source is the EOD pickle, no drop is needed.

## Details you need to know

- **Shape screen first, then lines.** The percentile-of-thirds screen ("does the envelope taper, and has price not broken out?") is the gate; only names that pass it get trendlines fit. Earlier attempts that *started* from a line fit (regression through last-K pivots, full convex-hull tangents, touch-count) overfit — they either missed obvious triangles or fit fake wedges to choppy pivots. Screening on the shape first and only then anchoring rays on the true extremes avoids that.
- **The reported range is the lines projected to today, not the wedge mouth.** This was a real bug: the old payload reported the last-third percentile box as the range, which scoops up month-old extremes and reports a band 2–3× too wide (e.g. CRML read $7.50–$13.96 when the live coil was ~$10.25–$12.64). The `_ray_to_today` lines converge forward to the current bar, which is what the trader sees and what the tightness gate measures.
- **The line values are approximate and window-dependent.** A single tall spike can swing a ray's slope, and the window sweep picks the tightest-band fit, which may not be the same anchor the trader would eyeball. The reported `res`/`sup` are good enough to gate tightness and seed the dashboard columns; the user draws the exact lines on the chart.
- **Tuned for recall.** The scan surfaces candidates; the user vets each chart and draws their own lines. Sloppy contractions (no clean triangle, but the envelope did taper) can slip through and are accepted as sift noise.
- The Setups page's `[D][W][M]` toggle is a row-level sub-filter on a single table — the producer outputs one payload per (ticker, matched timeframe), and the toggle hides the rows whose `data-tighttf` doesn't match the active timeframe. No table swapping.
- Daily-cache history must reach back at least `WINDOWS[tf][-1]` bars on the chosen timeframe for the largest sweep window to be tested. The current universe daily cache has 5+ years per ticker — plenty for D / W / M.

## Known bugs

- Sloppy multi-month consolidations whose envelope happens to taper (e.g. ALM) can still pass the shape test even though the structure isn't a clean triangle visually. Tightening the contraction floor or adding a touch-count overlay would drop them but at the cost of borderline-good names. Sift noise for now.

## Pending research

- A touch-count overlay (require multiple pivots to fall within tolerance of the approximate trendlines) could drop the sloppy false positives without re-introducing the overfitting that killed the line-fit approaches.
- Flat-top "key level" setups — ascending triangles where the top is horizontal rather than descending — are intentionally *not* matched here; that's a separate setup type planned later.

## Pending build

None currently active.

## Out of scope

- Pixel-perfect trendline drawing. The scan surfaces candidates; the user draws lines and picks entries on the chart.
- Flat-top / horizontal resistance variants (ascending triangles, breakouts from key levels). Those belong to a separate "key level" setup, not Tightening Range.
- Bearish (descending-triangle) variants. The match rule requires the midline to not point clearly down (normalized slope ≥ −0.0015) — a converging wedge or symmetrical triangle with at most a mild downward lean, not a falling wedge / bear pattern.
