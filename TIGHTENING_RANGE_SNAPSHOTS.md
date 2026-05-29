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
2. **Window sweep.** Per timeframe, try a fixed set of lookback windows — D: `{40, 60, 80, 100, 130}`, W: `{24, 36, 50, 65}`, M: `{18, 28, 40}`. For each window, run the shape test below. The match is the **tightest** window that qualifies (smallest end-range).
3. **Shape test on each window** (split into thirds):
   - **Highs descended:** 90th-percentile of highs in the last third < 90th-percentile of highs in the first third.
   - **Lows ascended:** 10th-percentile of lows in the last third > 10th-percentile of lows in the first third.
   - **Contracting envelope:** `end_range / start_range ≤ 0.70` (≥30% contraction). `range = high_p90 − low_p10` per third.
   - **Price inside.** Current close is between the absolute max-high and absolute min-low of the last third.
   - **Apex not pointing down.** Approximate the two trendlines from segment-center anchors (first-third midpoint and last-third midpoint), require resistance slope < 0, support slope > 0, and the normalized midline slope `((res_slope + sup_slope) / 2) / close ≥ 0`.
4. Among the windows that pass, keep the one with the tightest end-range — that's the match.

Percentile bounds (90th / 10th) instead of absolute max / min mean a single wick at the edge of a segment doesn't blow up the shape test — the detector tolerates jitters and noise. The window sweep means wedges of different lengths all get a fair look.

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
- `res`, `sup` — current envelope (absolute max-high / min-low of the matched window's last third).
- `band_pct`, `band_adr` — current band width as a % of price and as a multiple of 20-bar ADR.
- `bars_to_apex` — approximate bars until the segment-center-anchored lines would meet.
- `wedge_span` — bars in the matched window.
- `mid_norm` — normalized midline slope (≥ 0 always; non-negative apex direction).
- `asof_date` — the last bar's date (YYYY-MM-DD) at this timeframe.
- `contraction` — `end_range / start_range` from the shape test (≤ 0.70 always).
- `window_bars` — which window length matched (e.g. 40 for the tightest daily fit).
- `start_hi`, `start_lo` — first-third percentile bounds (the wedge "mouth").

Partial-bar drop semantics: when the source is the intraday pickle, the producer's worker strips the last (partial today) row before resampling. Weekly / monthly bars then never include a half-formed period. When the source is the EOD pickle, no drop is needed.

## Details you need to know

- The detector is **shape-based, not trendline-fitting**. Earlier attempts that fit specific lines (regression through last-K pivots, envelope/convex-hull tangent lines, touch-count) all overfit and either missed obvious triangles or fit fake wedges to choppy pivots. The percentile-of-thirds shape test side-steps the line-fit entirely — it just asks "does the envelope taper?". The trendlines reported in the payload are coarse approximations from segment centers, only used for the dashboard table's apex / band columns.
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
- Bearish (descending-triangle) variants. The match rule requires the midline to not point down — i.e. a non-bearish converging wedge or symmetrical triangle, not a falling wedge / bear pattern.
