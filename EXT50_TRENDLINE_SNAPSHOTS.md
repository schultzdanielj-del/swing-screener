# EXT50_TRENDLINE_SNAPSHOTS

## Purpose

Pre-compute the u1/u2/u3 + l1/l2/l3 50-SMA-extension trendline equations for the theme-dashboard universe (UNIVERSE in `theme_map.py`, ~916 tickers) at the most recent EOD bar. The dashboard's Setups page reads the resulting JSON, projects each line forward to today, and detects Extension Peek matches without ever re-fitting the trendlines mid-day. Trendlines do not refit during the trading day; only the projection forward + comparison against today's live ext50 moves.

## EXACT spec

Generator: `local_runner/ext50_trendline_snapshot_builder.py`.
Output: `local_runner/cache/ext50_trendline_snapshots.json`.
Inputs:
- `local_runner/cache/universe_ohlcv_daily.pkl` (or `_intraday.pkl` when newer — partial today bar is dropped before fitting; see "Partial-bar drop" below).
- `local_runner/theme_map.UNIVERSE` — the ticker list.
- Locked algorithms in `scripts/ext50_trendlines.py` (`cascade_at`, `_has_line_break`) and `scripts/reversal_profile.py` (`compute_all_reversal_profile_series`). No spec divergence — same code path the expression cache builder uses.

Invocation:
- `python local_runner/ext50_trendline_snapshot_builder.py` — runs for all UNIVERSE tickers in parallel using `ProcessPoolExecutor` (default workers = cpu_count − 2). Typical wall time: ~2 min for ~916 tickers on a 16-core box.
- No CLI flags — the only knob is `workers=` if called programmatically.
- Called directly from `theme_dashboard._build_html_to_disk` as Step 0; pass `--skip-snapshot` to the dashboard CLI to bypass when the morning snapshot is still valid.

Per-ticker compute (worker function `_ticker_snapshot`):
1. **ext50 series** (full history): `((close − SMA50) / SMA50 × 100) / adr20`, where `adr20 = mean((H/L − 1) × 100)` over 20 bars. NaN until 50 bars accumulated.
2. **Reversal-profile levels series** (full history) via `compute_all_reversal_profile_series(ext)`. Only the last bar's values feed `cascade_at` but the primitive computes the curve in one pass.
3. **`cascade_at(ext, asof_bar, levels_at_asof)`** — produces all candidate trendlines + the algorithm's top-3-per-side ranking.
4. **Strict break filter on descending lines:** walk `cascade_at`'s `all_candidates`, run `_has_line_break(ext, i0, v0, i1, v1, asof_bar)` on every peak-anchored (descending) candidate, drop any line whose ext50 series crosses through the line between `i0` and `asof_bar`. The locked algorithm's `_has_origination_side_break` tolerates crosses once the projection goes below zero — that's the wrong shape for "is this a currently valid resistance," so the snapshot builder re-filters with the strict full-line check. Re-rank survivors by `abs(signed_dist)`, take top 3. Ascending side passes through with the algorithm's existing strict-break enforcement.
5. **Pack and write.** Per-slot: `i0`, `v0`, `i1`, `v1`, `slope`, `anchor_type`, `proj_asof`, `signed_dist`, `span`. Per-ticker payload also carries `asof_bar`, `asof_date`, `asof_ext50`.

Output JSON top-level fields:
- `built_at` — ISO timestamp (UTC) when the builder ran.
- `built_in_seconds` — wall time of the build.
- `source_cache` — basename of the OHLCV pickle the build read.
- `intraday_partial_dropped` — `true` when source was the intraday pickle (so the asof bar reflects yesterday's EOD, not today's partial). `false` when source was the EOD-only pickle.
- `n_universe` — count of UNIVERSE tickers.
- `n_snapshots` — count of tickers that successfully produced a snapshot.
- `n_errors` — count of tickers that errored (usually "insufficient bars" for recent IPOs).
- `tickers` — dict mapping ticker → per-ticker payload.

Per-ticker payload schema:
- `asof_bar` (int): absolute bar index in the ticker's OHLCV df (last EOD bar, intraday partial dropped).
- `asof_date` (string YYYY-MM-DD): date of the asof bar.
- `asof_ext50` (float | None): ext50 value at the asof bar.
- `u` (list, 0-3 entries): descending trendlines, sorted by ascending `abs(signed_dist)`. Each entry has `i0`, `v0`, `i1`, `v1`, `slope`, `anchor_type` ("peak_anchored"), `proj_asof`, `signed_dist`, `span`.
- `l` (list, 0-3 entries): ascending trendlines, same schema, anchor_type = "trough_anchored".

Partial-bar drop semantics:
- When the source is the intraday pickle (today's partial bar is the last row), the builder drops that row before fitting. The snapshot's asof_bar then equals the most recent EOD bar (typically yesterday). This is the locked semantic — the intraday bar is NOT allowed to create new pivots; the day's trendline set is fixed at yesterday's close.
- When the source is the EOD-only pickle (no intraday refresh ran since the last nightly), the last row already IS the most recent EOD bar; no drop.
- The `intraday_partial_dropped` flag in the output reflects which path ran.

## Details you need to know

- The `_has_line_break` re-filter is intentional and load-bearing for the Setups scan. Without it, lines with `v0` barely above zero (e.g. +0.83) have a trivially-short origination-side segment and the locked algorithm's lenient break rule waves through visually-broken lines. TEM was the canonical regression that exposed this; the fix dropped 214 broken-line matches from a representative UNIVERSE scan.
- The locked sign convention (`signed_dist = proj_asof − ext_asof`) is preserved in the output. Positive = price below the line (under resistance); negative = price above the line (peeked). The dashboard's peek check is `today_sd < 0 AND yest_sd ≥ 0`.
- Multiprocessing on Windows uses `spawn`; worker reimports the snapshot builder module. The locked-algorithm modules are imported inside the worker function to keep cold-start light.
- Snapshot is the ONLY consumer of `scripts/reversal_profile.py` outside the expression cache pipeline. If reversal_profile's compute path changes, the snapshot's `levels_at_asof` input changes — must be kept in sync.
- The dashboard's `compute_extension_peeks` does its own live ext50 computation using a parallel formula to the snapshot builder. Both use 20-bar ADR. **If you change the formula in one, change it in the other.** Drift between them would produce ghost peeks (snapshot says line is here, dashboard computes ext50 differently and thinks it crossed) or missed peeks.

## Known bugs

None currently active.

## Pending research

None currently active.

## Pending build

None currently active.

## Out of scope

- Fitting trendlines on indicators other than the 50-SMA extension. The snapshot is named `ext50` for a reason — 200-SMA extension trendlines are a separate question with their own meaning.
- Persisting historical snapshots (e.g. one JSON per day). Today's snapshot overwrites yesterday's. If a historical replay is ever needed, write a separate component.
- Detecting setups beyond the line equations themselves. The Setups page's Extension Peek detector lives in `theme_dashboard.compute_extension_peeks`, not here. This component's only job is to produce the line equations.
