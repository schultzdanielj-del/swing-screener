# FIRST_FLAGS_SNAPSHOTS

## Purpose

Pre-compute the day's "First Flags" matches for the theme-dashboard universe
(`UNIVERSE` in `theme_map.py`) at the most recent EOD bar. A First Flag is a
bottom-reversal continuation candidate: a bullish MACD 6/20 divergence that
bottomed below the 200-day SMA, followed by a thrust up, where price has
reclaimed the 50-day SMA but the MACD has rolled back under its signal — the
first flag (entry pullback) actively forming. The dashboard's Setups page reads
the resulting JSON and renders the matches; the flag itself is vetted on the
chart, not auto-detected. Divergence pivots do not refit during the trading day.

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
1. **Most recent bullish divergence.** Run `detect_divergences`; take the last (most recent) bullish entry. Its anchor low (`p2`) is the bottom.
2. **Bottom below the 200-SMA.** The bottom bar's close is below its 200-day SMA. Tickers with fewer than 210 bars can't qualify (no SMA200 at the bottom).
3. **≥25% pole.** Price has risen ≥25% from the bottom low to the highest high after it, through the as-of bar.
4. **Above the 50-SMA, held.** The close has been above the 50-day SMA for the last 10 bars straight.
5. **MACD still in bear cross.** The 6/20 MACD line is below its 9-EMA signal at the as-of bar (the flag is forming, not yet released).

Only tickers passing all five are written. Indicator math (`sma_2d`, `macd_2d`, `ema_2d`) reuses `vectorized_indicators.py`; the MACD line is `EMA6 − EMA20` and the signal is its `EMA9`, matching the composite chart.

Output JSON top-level fields:
- `built_at` — ISO timestamp (UTC) when the builder ran.
- `built_in_seconds` — wall time of the build.
- `source_cache` — basename of the OHLCV pickle the build read.
- `intraday_partial_dropped` — `true` when the source was the intraday pickle (the as-of bar reflects the prior EOD, not today's partial). `false` when the source was the EOD-only pickle.
- `move_min_pct` — the pole threshold in effect (25.0).
- `above50_bars` — the 50-SMA hold length in effect (10).
- `requires_macd_bearcross` — `true`.
- `n_universe` — count of UNIVERSE tickers.
- `n_matches` — count of tickers that matched.
- `n_errors` — count of tickers that errored.
- `tickers` — dict mapping ticker → per-ticker payload.

Per-ticker payload schema:
- `asof_bar` (int), `asof_date` (YYYY-MM-DD) — last EOD bar (intraday partial dropped).
- `bottom_idx`, `bottom_date`, `bottom_low`, `bottom_close` — the divergence bottom.
- `sma200_at_bottom`, `below_200_pct` — the bottom's 200-SMA and how far below it closed.
- `prior_low_idx`, `prior_low_date`, `prior_low_price` — the divergence's earlier (higher) price low.
- `macd_prior`, `macd_bottom` — the MACD-line values at the two divergence pivots (MACD higher-low confirms the divergence).
- `pole_high_idx`, `pole_high_date`, `pole_high_price`, `pole_pct` — the pole peak and its % gain off the bottom low.
- `bars_since_bottom` — trading bars from the bottom to the as-of bar.
- `pullback_pct` — how far the as-of close sits below the pole high.
- `macd_line`, `macd_signal` — the 6/20 MACD line and its 9-EMA signal at the as-of bar (`macd_line < macd_signal` for every match).

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

- **Filter set is still being tuned.** The five-condition rule above is the current cut; additional gates (e.g. pole-relative pullback depth, distance from the 50, volume) are likely to be added as the candidate list is vetted visually. Threshold choices are driven by chart-vetting, not picked to hit a count target.

## Pending build

None currently active.

## Out of scope

- Detecting the flag/pullback entry itself. This component surfaces the bottom-reversal candidates; the entry is vetted on the chart. Auto-detecting the pullback is a separate, deferred layer.
- Persisting historical snapshots (one JSON per day). Today's snapshot overwrites yesterday's.
- Fitting divergences on indicators other than the 6/20 MACD line, or detecting bearish-divergence setups — First Flags is a bullish bottom-reversal scan only.
