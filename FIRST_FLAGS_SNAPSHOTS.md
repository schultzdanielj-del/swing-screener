# FIRST_FLAGS_SNAPSHOTS

## Purpose

Pre-compute the day's "First Flags" matches for the theme-dashboard universe
(`UNIVERSE` in `theme_map.py`) at the most recent EOD bar. A First Flag is a
bottom-reversal continuation candidate: a real bullish MACD 6/20 divergence that
bottomed below the 200-day SMA, followed by an uptrend (the 10/20/50 SMAs
stacked in order) now in its first pullback — the flag — riding the fast MA. The
dashboard's Setups page reads the resulting JSON and renders the matches; the
exact entry is vetted on the chart. Divergence pivots do not refit during the
trading day.

## EXACT spec

Generator: `local_runner/first_flags_snapshot_builder.py`.
Output: `local_runner/cache/first_flags_snapshots.json`.
Inputs:
- `local_runner/cache/universe_ohlcv_daily.pkl` (or `_intraday.pkl` when newer — the partial today bar is dropped before scanning; see "Partial-bar drop").
- `local_runner/theme_map.UNIVERSE` — the ticker list.
- `theme_dashboard.detect_divergences` — the SAME bullish/bearish MACD 6/20-line divergence detector the composite chart uses. Imported (not re-implemented) so there is one source of truth for the divergence logic.

Invocation:
- `python local_runner/first_flags_snapshot_builder.py` — runs for all UNIVERSE tickers in parallel using `ProcessPoolExecutor` (default workers = cpu_count − 2). Light per-ticker compute; the full universe runs in well under a minute.
- No CLI flags. Programmatic knobs: `workers=`, `cache_dir=` (where the OHLCV pickle is read), `out_dir=` (where the snapshot is written; defaults to `cache_dir`).
- Called directly from `theme_dashboard._build_html_to_disk` as Step 0b; pass `--skip-snapshot` to the dashboard CLI to bypass when the morning snapshot is still valid.

Match rule (per ticker, worker function `_ticker_snapshot`):
1. **Most recent bullish divergence, below the 200.** Run `detect_divergences`; take the last bullish entry. Its anchor low (`p2`) is the bottom, and that bar's close must be below its 200-day SMA. Tickers with fewer than 210 bars can't qualify (no SMA200 at the bottom).
2. **Real divergence.** The anchor low must be at least `DIV_MIN_LL_PCT` (2%) below the prior pivot low — drops the `-0.1%` micro "divergences" the detector would otherwise count.
3. **Trend / pole: 10/20/50 stacked.** After the bottom, the 10/20/50 SMAs stack in order (10 > 20 > 50) at some bar. No fixed % move — the MA stack IS the trend.
4. **Stack still intact** at the scan bar (10 > 20 > 50 right now).
5. **First / early flag.** At most `MAX_SWING_HIGHS` (2) swing highs (3-bar pivots) since the stack formed — drops multi-leg runners that already ran far past the divergence.
6. **Pulled back.** Price (close) is currently below the highest high made since the stack formed, and that high is in the past (not a fresh breakout today).
7. **Riding the fast MA.** Close is within `RIDE_PCT` (5%) of the 10 or 20 SMA — the flag hugging the rising MAs.

Only tickers passing all of the above are written. SMAs (`sma_2d`) match TC2000's Avg 10/20/50/200. Conditions 2 and 5 were derived from a labeled set of A+ first flags vs clear fails; `RIDE_PCT`, `DIV_MIN_LL_PCT`, `MAX_SWING_HIGHS` are the dials. The scan is tuned for recall — it only needs to surface a name on SOME day during its flag; the user takes it to a TC2000 watchlist.

Output JSON top-level fields:
- `built_at` — ISO timestamp (UTC) when the builder ran.
- `built_in_seconds` — wall time of the build.
- `source_cache` — basename of the OHLCV pickle the build read.
- `intraday_partial_dropped` — `true` when the source was the intraday pickle (the as-of bar reflects the prior EOD, not today's partial). `false` when the source was the EOD-only pickle.
- `ride_pct` — the riding band in effect (5.0).
- `div_min_ll_pct` — the minimum divergence lower-low % in effect (2.0).
- `max_swing_highs` — the max swing highs since the stack in effect (2).
- `n_universe` — count of UNIVERSE tickers.
- `n_matches` — count of tickers that matched.
- `n_errors` — count of tickers that errored.
- `tickers` — dict mapping ticker → per-ticker payload.

Per-ticker payload schema:
- `asof_bar` (int), `asof_date` (YYYY-MM-DD) — last EOD bar (intraday partial dropped).
- `bottom_idx`, `bottom_date`, `bottom_low`, `bottom_close` — the divergence bottom.
- `below_200_pct` — how far below its 200-SMA the bottom closed.
- `div_ll_pct` — how far the anchor low is below the prior pivot low (the divergence's lower-low; ≤ −2 for every match).
- `stack_start_idx`, `stack_start_date` — the first bar after the bottom where 10/20/50 stacked in order.
- `swing_highs_since_stack` — swing-high pivots since the stack formed (≤ 2 for every match).
- `pole_high_idx`, `pole_high_date`, `pole_high_price`, `pole_pct` — the highest high since the stack and its % gain off the bottom low.
- `bars_since_bottom`, `bars_since_stack` — trading bars from the bottom / from the stack to the as-of bar.
- `pullback_pct` — how far the as-of close sits below that pole high (the flag depth).
- `ride_pct` — distance from the close to the nearer of the 10/20 SMA (≤ 5 for every match).

Partial-bar drop semantics:
- When the source is the intraday pickle, the builder drops the last (partial) row before scanning, so the as-of bar is the most recent EOD bar (typically yesterday) and the divergence pivots are fixed at that close. The intraday bar is not allowed to create new pivots.
- When the source is the EOD-only pickle, the last row already IS the most recent EOD bar; no drop.
- The `intraday_partial_dropped` flag in the output reflects which path ran.

## Details you need to know

- Divergence detection is **imported from `theme_dashboard`**, not copied. The worker lazy-imports `theme_dashboard.detect_divergences` inside `_ticker_snapshot` so the parent process and cold start stay light, and so there is no second divergence implementation to drift. If `detect_divergences` (or its pivot params) changes, First Flags changes with it.
- The dashboard's `compute_first_flags` does NOT re-detect divergences. It reads this snapshot and only refreshes the pullback against today's live close — the bottom/pole/divergence are fixed for the day. The match SET is fixed at the snapshot; the live tape only moves the pullback number shown.
- Multiprocessing on Windows uses `spawn`; each worker reimports the builder module (light top-level imports only — `theme_dashboard` is imported inside the worker, not at module top) and `theme_dashboard`.
- The snapshot stores only matches, so it is small (tens of KB), unlike the per-ticker ext50 snapshot.

## Known bugs

None currently active.

## Pending research

- **Dials.** `RIDE_PCT` (5%), `DIV_MIN_LL_PCT` (2%), and `MAX_SWING_HIGHS` (2) are the recall/precision dials, derived from a labeled win/fail set rather than eyeballed. A few borderline names (recent, real divergence, few legs) still pass and are sifted by eye — tightening further risks dropping A+ names, and the user prefers recall + sifting over missing setups. "No real divergence but kickass post-earnings flag" setups are accepted as missed by design.

## Pending build

None currently active.

## Out of scope

- Pinpointing the exact entry trigger. The scan surfaces names currently in a first flag; the precise entry (breakout bar, stop) is vetted on the chart.
- Persisting historical snapshots (one JSON per day). Today's snapshot overwrites yesterday's.
- Fitting divergences on indicators other than the 6/20 MACD line, or detecting bearish-divergence setups — First Flags is a bullish bottom-reversal scan only.
