"""
Signal Exit Pool Grinder — aggregate-P&L objective over the combined breakout pool.

Per CLASSIFIER_SPEC.md §2 (fixed constraints), §15 (labeler mechanic),
§17.7 (tag mechanic), and SIGNAL_EXIT_GRINDER.md Pending build.

For a setup in {htf, bf, base}:

  1. Read <classifier worktree>/research/classifier_tags/{setup}_tags.json.
  2. Filter to tag == "ENTRY". Non-ENTRY rows keep their pool-order slot
     with SKIPPED_NOT_ENTRY status (never enter the forward race).
  3. Consume entry_idx, entry_date per ENTRY row. ADR14 at entry is recomputed
     from OHLCV (Option B per Dan 2026-04-24).
  4. Build stop / effective_entry / cap_bar / cap_cause per §15.4 and §2 c9a.
  5. Compute rule-independent mfe_adr per cluster.
  6. For each (expression, direction in {>=, <=}, threshold):
       stop hit first -> pnl = -1 ADR, exit_cause = stop_hit
       exit fires     -> pnl = (close - effective_entry) / ADR14_at_entry,
                         exit_cause = exit_fire
       forced at cap  -> pnl from cap close, exit_cause =
                         forced_earnings if cap came from ern_bar-1,
                         forced_time_cap otherwise (120-cap or end-of-tape)
     final_label = WIN iff pnl > 0 else LOSS (scratch counted as LOSS per §15.5).
  7. Objective = aggregate_pnl_adr (sum across entered clusters). No tie-break.
  8. Keep top-N. Enrich with aggregate summary block. Write.

Output: <WORKTREE>/data/signal_exit_grind/signal_exit_pool_{setup}.json
(does not clobber the existing signal_exit_{setup}.json).
"""

import argparse
import json
import os
import pickle
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


# ============================================================
# Path resolution — cache always read-only from main repo.
# ============================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WORKTREE_ROOT = os.path.dirname(SCRIPT_DIR)

MAIN_REPO_ROOT = os.environ.get(
    "SCANPERFECT_MAIN_REPO",
    r"C:\Users\Dan\Documents\ScanPerfect\swing-screener",
)
CLASSIFIER_REPO = os.environ.get(
    "SCANPERFECT_CLASSIFIER_REPO",
    r"C:\Users\Dan\Documents\ScanPerfect\swing-screener",
)

if not os.path.isdir(os.path.join(MAIN_REPO_ROOT, "local_runner", "cache")):
    sys.exit(f"MAIN_REPO_ROOT missing local_runner/cache at {MAIN_REPO_ROOT}")
if not os.path.isdir(os.path.join(CLASSIFIER_REPO, "research", "classifier_tags")):
    sys.exit(f"CLASSIFIER_REPO missing research/classifier_tags at {CLASSIFIER_REPO}")

sys.path.insert(0, MAIN_REPO_ROOT)
sys.path.insert(0, os.path.join(MAIN_REPO_ROOT, "local_runner"))

from expr_cache_builder import ExprSeriesCache  # noqa: E402


# ============================================================
# Constants
# ============================================================
OHLCV_PATH = os.path.join(MAIN_REPO_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB_PATH = os.path.join(MAIN_REPO_ROOT, "data", "scanperfect.db")
TAGS_DIR = os.path.join(CLASSIFIER_REPO, "research", "classifier_tags")
OUTPUT_DIR = os.path.join(WORKTREE_ROOT, "data", "signal_exit_grind")

EXPECTED_MIN_TICKERS = 11200
MIN_N_EXPRESSIONS = 1000  # runtime floor; real count read from ExprSeriesCache

TRADE_LIFETIME_CAP_BARS = 120
N_THRESHOLDS_DEFAULT = 20
TOP_N_SAVED = 50
ADR_LOOKBACK = 14

SUPPORTED_SETUPS = ("htf", "bf", "base")

# Cap-cause codes
CAP_EARNINGS = 0
CAP_TIME = 1

# Exit-cause codes
EX_EXIT_FIRE = 0
EX_STOP_HIT = 1
EX_FORCED_EARNINGS = 2
EX_FORCED_TIME_CAP = 3

EXIT_CAUSE_STR = {
    EX_EXIT_FIRE: "exit_fire",
    EX_STOP_HIT: "stop_hit",
    EX_FORCED_EARNINGS: "forced_earnings",
    EX_FORCED_TIME_CAP: "forced_time_cap",
}


# ============================================================
# Cluster metadata
# ============================================================
@dataclass
class ClusterMeta:
    cluster_id: int
    ticker: str
    is_example: int
    tag: str
    signal_bar_idx: int
    status: str = "PENDING"
    reason: str = ""
    entry_k: Optional[int] = None
    entry_bar: Optional[int] = None                  # OHLCV-relative
    entry_date: Optional[str] = None                 # YYYY-MM-DD
    cap_bar: Optional[int] = None                    # OHLCV-relative, inclusive
    cap_date: Optional[str] = None                   # YYYY-MM-DD
    cap_cause: Optional[int] = None                  # CAP_EARNINGS or CAP_TIME
    horizon: int = 0
    entry_high: Optional[float] = None
    entry_low: Optional[float] = None
    entry_close: Optional[float] = None
    adr14_at_entry: Optional[float] = None
    effective_entry: Optional[float] = None
    stop: Optional[float] = None
    stop_hit_bar: int = 0                            # j-indexed; horizon if never
    mfe_adr: Optional[float] = None                  # rule-independent per cluster
    forward_closes: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    forward_lows: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))
    forward_highs: np.ndarray = field(default_factory=lambda: np.empty(0, dtype=np.float64))


# ============================================================
# Loaders
# ============================================================
def load_ohlcv():
    print(f"  Loading OHLCV cache from {OHLCV_PATH}")
    with open(OHLCV_PATH, "rb") as f:
        cache = pickle.load(f)
    n = len(cache)
    print(f"  OHLCV tickers: {n:,}")
    if n < EXPECTED_MIN_TICKERS:
        sys.exit(f"  ABORT: ticker count {n} < expected {EXPECTED_MIN_TICKERS}")
    return cache


def load_earnings_map():
    print(f"  Loading earnings dates from {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT ticker, earnings_date FROM earnings_dates").fetchall()
    conn.close()
    by_ticker = {}
    for t, d in rows:
        by_ticker.setdefault(t, []).append(str(d)[:10])
    for t in by_ticker:
        by_ticker[t] = np.array(sorted(set(by_ticker[t])), dtype="<U10")
    print(f"  Earnings tickers: {len(by_ticker):,}  rows: {len(rows):,}")
    return by_ticker


def load_tags(setup):
    path = os.path.join(TAGS_DIR, f"{setup}_tags.json")
    with open(path) as f:
        tags = json.load(f)
    n = len(tags.get("clusters", []))
    by_tag = tags.get("counts", {}).get("by_tag", {})
    print(f"  Tags {setup}: {n} clusters  (ENTRY={by_tag.get('ENTRY')}  "
          f"REDUNDANT={by_tag.get('REDUNDANT')}  NOENTRY={by_tag.get('NOENTRY')}  "
          f"MISSING={by_tag.get('MISSING')})")
    return tags


# ============================================================
# Earnings cap (spec §2 c9a)
# ============================================================
def next_earnings_bar(earnings_arr, ohlcv_dates_str, entry_bar_idx):
    """Bar idx of the first earnings strictly after entry_bar_idx; None if none."""
    if earnings_arr is None or len(earnings_arr) == 0:
        return None
    entry_date_str = ohlcv_dates_str[entry_bar_idx]
    pos = np.searchsorted(earnings_arr, entry_date_str, side="right")
    if pos >= len(earnings_arr):
        return None
    next_ern = earnings_arr[pos]
    bp = int(np.searchsorted(ohlcv_dates_str, next_ern, side="left"))
    if bp <= entry_bar_idx or bp >= len(ohlcv_dates_str):
        return None
    return bp


# ============================================================
# Phase 1 — build cluster meta from tag file
# ============================================================
def build_cluster_meta(tags_json, ohlcv, earnings_map):
    clusters = tags_json["clusters"]
    meta_list: List[Tuple[int, ClusterMeta]] = []

    # Non-ENTRY rows keep pool-order slots with SKIPPED_NOT_ENTRY status.
    for i, c in enumerate(clusters):
        tag = c.get("tag", "")
        if tag == "ENTRY":
            continue
        meta_list.append((i, ClusterMeta(
            cluster_id=c["cluster_id"], ticker=c["ticker"],
            is_example=c.get("is_example", 0),
            tag=tag,
            signal_bar_idx=c.get("sig_idx", -1),
            status="SKIPPED_NOT_ENTRY",
            reason=f"tag={tag}",
        )))

    # Group ENTRY rows by ticker for shared OHLCV load.
    by_ticker: dict = {}
    for i, c in enumerate(clusters):
        if c.get("tag") == "ENTRY":
            by_ticker.setdefault(c["ticker"], []).append((i, c))

    for ticker, items in by_ticker.items():
        df = ohlcv.get(ticker)
        if df is None:
            for i, c in items:
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=c.get("sig_idx", -1),
                    status="SKIPPED_MISSING_DATA",
                    reason="ticker not in OHLCV cache",
                )))
            continue

        highs = df["high"].values.astype(np.float64)
        lows = df["low"].values.astype(np.float64)
        closes = df["close"].values.astype(np.float64)
        dates_str = np.array([str(d)[:10] for d in df["date"].values], dtype="<U10")
        n_bars = len(df)
        ern_arr = earnings_map.get(ticker)

        for i, c in items:
            sig_idx = c.get("sig_idx", -1)
            entry_bar = c.get("entry_idx")
            entry_date = c.get("entry_date")
            entry_k = c.get("entry_k")

            if entry_bar is None or entry_bar < 0 or entry_bar >= n_bars:
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=sig_idx,
                    status="SKIPPED_MISSING_DATA",
                    reason=f"entry_idx {entry_bar} out of range (n_bars={n_bars})",
                )))
                continue

            if entry_date is not None and dates_str[entry_bar] != entry_date:
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=sig_idx,
                    status="SKIPPED_MISSING_DATA",
                    reason=f"entry_date mismatch: tag={entry_date} ohlcv={dates_str[entry_bar]}",
                )))
                continue

            e_high = highs[entry_bar]
            e_low = lows[entry_bar]
            e_close = closes[entry_bar]

            # ADR14 recomputed at entry_idx per §15.4 (Option B).
            start = max(0, entry_bar - (ADR_LOOKBACK - 1))
            adr14 = float(np.mean(highs[start:entry_bar + 1] - lows[start:entry_bar + 1]))
            if adr14 <= 0 or np.isnan(adr14):
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=sig_idx,
                    status="SKIPPED_MISSING_DATA",
                    reason=f"invalid ADR14 at entry ({adr14})",
                )))
                continue

            effective_entry = min(e_high, e_low + adr14)
            stop = e_low

            ern_bar = next_earnings_bar(ern_arr, dates_str, entry_bar)
            time_cap = entry_bar + TRADE_LIFETIME_CAP_BARS
            end_of_tape = n_bars - 1
            earnings_cap = ern_bar - 1 if ern_bar is not None else None

            candidates = [time_cap, end_of_tape]
            if earnings_cap is not None:
                candidates.append(earnings_cap)
            cap_bar = min(candidates)

            if cap_bar <= entry_bar:
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=sig_idx,
                    status="SKIPPED_MISSING_DATA",
                    reason="cap_bar <= entry_bar (earnings or end-of-tape)",
                )))
                continue

            # earnings wins only if it was the tightest cap; else time (covers 120-cap + end-of-tape).
            if earnings_cap is not None and cap_bar == earnings_cap:
                cap_cause = CAP_EARNINGS
            else:
                cap_cause = CAP_TIME

            horizon = cap_bar - entry_bar
            fwd_lows = lows[entry_bar + 1 : cap_bar + 1]
            fwd_closes = closes[entry_bar + 1 : cap_bar + 1]
            fwd_highs = highs[entry_bar + 1 : cap_bar + 1]

            stop_mask = fwd_lows <= stop
            stop_hit_bar = int(np.argmax(stop_mask)) if stop_mask.any() else horizon

            if fwd_highs.size > 0:
                mfe_adr = float((fwd_highs.max() - effective_entry) / adr14)
            else:
                mfe_adr = 0.0

            meta_list.append((i, ClusterMeta(
                cluster_id=c["cluster_id"], ticker=ticker,
                is_example=c.get("is_example", 0),
                tag="ENTRY",
                signal_bar_idx=sig_idx,
                status="ENTERED",
                entry_k=entry_k, entry_bar=entry_bar,
                entry_date=str(dates_str[entry_bar]),
                cap_bar=cap_bar, cap_date=str(dates_str[cap_bar]),
                cap_cause=cap_cause,
                horizon=horizon,
                entry_high=float(e_high), entry_low=float(e_low), entry_close=float(e_close),
                adr14_at_entry=adr14,
                effective_entry=float(effective_entry), stop=float(stop),
                stop_hit_bar=stop_hit_bar,
                mfe_adr=mfe_adr,
                forward_closes=fwd_closes, forward_lows=fwd_lows, forward_highs=fwd_highs,
            )))

    meta_list.sort(key=lambda x: x[0])
    return [m for _, m in meta_list]


# ============================================================
# Phase 2 — forward expression slab
# ============================================================
def build_expr_slab(entered, expr_cache, n_expressions):
    """Returns slab (n_ent, max_h, n_expressions) float32 NaN-padded
    plus list of (entered_idx, reason) for clusters dropped due to expr-cache misalignment.
    """
    n_ent = len(entered)
    max_horizon = max(m.horizon for m in entered) if entered else 0
    if n_ent == 0 or max_horizon == 0:
        return np.empty((0, 0, n_expressions), dtype=np.float32), []

    print(f"  Building forward slab: {n_ent} entered x {max_horizon} bars x {n_expressions} exprs "
          f"(~{n_ent * max_horizon * n_expressions * 4 / 1e9:.2f} GB)")

    slab = np.full((n_ent, max_horizon, n_expressions), np.nan, dtype=np.float32)

    by_ticker = {}
    for idx, m in enumerate(entered):
        by_ticker.setdefault(m.ticker, []).append(idx)

    dropped = []
    t_load = 0.0
    t_fill = 0.0
    n_load = 0

    for ticker, idxs in by_ticker.items():
        t0 = time.time()
        expr_dates, expr_data = expr_cache.get_ticker(ticker)
        t_load += time.time() - t0
        n_load += 1
        if expr_dates is None:
            for idx in idxs:
                dropped.append((idx, "ticker not in expression cache"))
            continue

        expr_dates_str = np.array([str(d)[:10] for d in expr_dates], dtype="<U10")

        t1 = time.time()
        for idx in idxs:
            m = entered[idx]
            pos_entry = int(np.searchsorted(expr_dates_str, m.entry_date, side="left"))
            if pos_entry >= len(expr_dates_str) or expr_dates_str[pos_entry] != m.entry_date:
                dropped.append((idx, f"entry_date {m.entry_date} not in expr cache"))
                continue
            pos_cap = int(np.searchsorted(expr_dates_str, m.cap_date, side="left"))
            if pos_cap >= len(expr_dates_str) or expr_dates_str[pos_cap] != m.cap_date:
                dropped.append((idx, f"cap_date {m.cap_date} not in expr cache"))
                continue
            fwd = expr_data[pos_entry + 1 : pos_cap + 1, :]
            if fwd.shape[0] != m.horizon:
                dropped.append((idx, f"expr fwd len {fwd.shape[0]} != ohlcv horizon {m.horizon}"))
                continue
            slab[idx, : m.horizon, :] = fwd.astype(np.float32, copy=False)
        t_fill += time.time() - t1

    print(f"  Slab built. Loaded {n_load} unique tickers in {t_load:.1f}s, fill {t_fill:.1f}s, dropped {len(dropped)}.")
    return slab, dropped


# ============================================================
# Phase 3 — grind
# ============================================================
def percentile_thresholds(values, n_thresholds):
    clean = values[~np.isnan(values)]
    if len(clean) < 5:
        return np.empty(0)
    pcts = np.linspace(5, 95, n_thresholds)
    raw = np.percentile(clean, pcts)
    rounded = np.round(raw, 6)
    return np.unique(rounded)


def score_candidate(series, thresh, op_str, close_mat, stop_bars, horizons, effs, adrs,
                    cap_causes, bar_idx_mat):
    """Score one (expression, direction, threshold) candidate.

    Returns (aggregate_pnl, pnl_per_cluster, exit_offset_per_cluster, exit_cause_codes).
    """
    if op_str == "ge":
        fires = series >= thresh
    else:
        fires = series <= thresh
    fires &= bar_idx_mat < stop_bars[:, None]  # exit can only fire strictly before stop bar
    any_fire = fires.any(axis=1)
    first_fire = np.argmax(fires, axis=1)

    forced_idx = horizons - 1
    stop_first = stop_bars < horizons

    exit_idx = np.where(
        any_fire,
        first_fire,
        np.where(stop_first, stop_bars, forced_idx),
    )
    exit_idx_clip = np.clip(exit_idx, 0, close_mat.shape[1] - 1)
    close_at_exit = np.take_along_axis(close_mat, exit_idx_clip[:, None], axis=1)[:, 0]
    pnl_close = (close_at_exit - effs) / adrs
    pnl = np.where(
        any_fire,
        pnl_close,
        np.where(stop_first, -1.0, pnl_close),
    )

    exit_offset = exit_idx + 1  # bars after entry (j=0 -> offset=1)

    cause = np.where(
        any_fire,
        EX_EXIT_FIRE,
        np.where(
            stop_first,
            EX_STOP_HIT,
            np.where(cap_causes == CAP_EARNINGS, EX_FORCED_EARNINGS, EX_FORCED_TIME_CAP),
        ),
    ).astype(np.int8)

    aggregate = float(pnl.sum())
    return aggregate, pnl, exit_offset, cause


def grind(slab, close_mat, stop_bars, horizons, effs, adrs, cap_causes,
          expr_names, n_thresholds, top_n):
    n_ent, max_h, n_exprs = slab.shape
    bar_idx_mat = np.arange(max_h, dtype=np.int64)[None, :]

    print(f"  Grinding {n_exprs:,} expressions x ~{n_thresholds} thresholds x 2 directions")
    print(f"  Population: {n_ent} entered clusters, max horizon {max_h} bars")
    t0 = time.time()

    keep: List[dict] = []
    prune_every = 200000
    candidates_seen = 0

    for e in range(n_exprs):
        series = slab[:, :, e]
        thresholds = percentile_thresholds(series.ravel(), n_thresholds)
        if thresholds.size == 0:
            continue
        for t_val in thresholds:
            for op_str, dir_label in (("ge", ">="), ("le", "<=")):
                aggregate, pnl, offsets, cause = score_candidate(
                    series, float(t_val), op_str, close_mat,
                    stop_bars, horizons, effs, adrs, cap_causes, bar_idx_mat,
                )
                candidates_seen += 1
                rec = dict(
                    expression=expr_names[e],
                    direction=dir_label,
                    threshold=float(round(t_val, 6)),
                    aggregate_pnl_adr=aggregate,
                    _pnl=pnl, _offsets=offsets, _cause=cause,
                )
                keep.append(rec)
                if len(keep) > prune_every:
                    keep.sort(key=lambda r: -r["aggregate_pnl_adr"])
                    del keep[top_n:]

        if (e + 1) % 1000 == 0:
            elapsed = time.time() - t0
            rate = (e + 1) / elapsed if elapsed > 0 else 0
            top_agg = max(r["aggregate_pnl_adr"] for r in keep) if keep else 0
            print(f"    [{e+1}/{n_exprs}] {rate:.0f} expr/s  {candidates_seen:,} candidates  "
                  f"current top agg={top_agg:.2f}")

    keep.sort(key=lambda r: -r["aggregate_pnl_adr"])
    keep = keep[:top_n]
    print(f"  Done. {candidates_seen:,} candidates scored in {time.time()-t0:.1f}s. Top {len(keep)} kept.")
    return keep


# ============================================================
# Phase 4 — enrich top with labels + aggregate summary
# ============================================================
def enrich_top(top, entered_mfe_adr):
    aggregate_mfe_adr = float(entered_mfe_adr.sum())
    for r in top:
        pnl = r["_pnl"]
        cause = r["_cause"]

        final_labels = np.where(pnl > 0, "WIN", "LOSS")
        exit_causes = np.array([EXIT_CAUSE_STR[int(c)] for c in cause], dtype=object)

        win_mask = pnl > 0
        loss_mask = ~win_mask
        n_win = int(win_mask.sum())
        n_loss = int(loss_mask.sum())
        n_tot = n_win + n_loss

        if n_win > 0:
            win_pnl = pnl[win_mask]
            win_pnl_mean = float(win_pnl.mean())
            win_pnl_median = float(np.median(win_pnl))
            win_pnl_p25 = float(np.percentile(win_pnl, 25))
            win_pnl_p75 = float(np.percentile(win_pnl, 75))
        else:
            win_pnl_mean = win_pnl_median = win_pnl_p25 = win_pnl_p75 = None

        loss_pnl_mean = float(pnl[loss_mask].mean()) if n_loss > 0 else None
        win_rate = (n_win / n_tot) if n_tot > 0 else None
        capture = (
            r["aggregate_pnl_adr"] / aggregate_mfe_adr
            if aggregate_mfe_adr != 0.0
            else None
        )

        r["_final_labels"] = final_labels
        r["_exit_causes_str"] = exit_causes
        r["aggregate_summary"] = dict(
            n_win=n_win,
            n_loss=n_loss,
            win_rate=float(win_rate) if win_rate is not None else None,
            win_pnl_mean=win_pnl_mean,
            win_pnl_median=win_pnl_median,
            win_pnl_p25=win_pnl_p25,
            win_pnl_p75=win_pnl_p75,
            loss_pnl_mean=loss_pnl_mean,
            aggregate_mfe_adr=aggregate_mfe_adr,
            aggregate_pnl_capture_fraction=(float(capture) if capture is not None else None),
        )


# ============================================================
# Phase 5 — pool-ordered per-cluster expansion
# ============================================================
def expand_per_cluster(top, meta, entered_pool_indices):
    n_pool = len(meta)
    for r in top:
        pnl_pool = [None] * n_pool
        off_pool = [None] * n_pool
        label_pool = [None] * n_pool
        cause_pool = [None] * n_pool
        pnl = r.pop("_pnl")
        off = r.pop("_offsets")
        r.pop("_cause")
        final_labels = r.pop("_final_labels")
        exit_causes_str = r.pop("_exit_causes_str")
        for e_i, pool_i in enumerate(entered_pool_indices):
            pnl_pool[pool_i] = float(round(float(pnl[e_i]), 4))
            off_pool[pool_i] = int(off[e_i])
            label_pool[pool_i] = str(final_labels[e_i])
            cause_pool[pool_i] = str(exit_causes_str[e_i])
        r["per_cluster_pnl_adr"] = pnl_pool
        r["per_cluster_exit_bar_offset"] = off_pool
        r["per_cluster_final_label"] = label_pool
        r["per_cluster_exit_cause"] = cause_pool


# ============================================================
# Phase 6 — verification + write
# ============================================================
def verify_examples_lock(top, meta):
    if not top:
        return False, "no candidates"
    rule = top[0]
    pnl_pool = rule["per_cluster_pnl_adr"]
    violations = []
    for i, m in enumerate(meta):
        if m.is_example and m.status == "ENTERED":
            v = pnl_pool[i]
            if v is None or v <= 0:
                violations.append((m.cluster_id, m.ticker, v))
    return (len(violations) == 0), violations


def verify_aggregate_positive(top):
    if not top:
        return False, 0.0
    return top[0]["aggregate_pnl_adr"] > 0, top[0]["aggregate_pnl_adr"]


def write_output(setup, top, meta, dropped, n_clusters_pool, halted, halt_reason, verify_lock_violations):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    n_entered = sum(1 for m in meta if m.status == "ENTERED")
    n_not_entry = sum(1 for m in meta if m.status == "SKIPPED_NOT_ENTRY")
    n_skipped = sum(1 for m in meta if m.status == "SKIPPED_MISSING_DATA")

    output = {
        "setup_type": setup,
        "grinder_type": "signal_exit_pool_aggregate_pnl",
        "timestamp": datetime.now().isoformat(),
        "adr_source": "ADR14_at_entry (recomputed from OHLCV)",
        "n_clusters_pool": n_clusters_pool,
        "n_entered": n_entered,
        "n_skipped_not_entry": n_not_entry,
        "n_skipped_missing_data": n_skipped,
        "n_dropped_expr_cache_misalign": len(dropped),
        "trade_lifetime_cap_bars": TRADE_LIFETIME_CAP_BARS,
        "examples_lock_passed": (not halted) or (halt_reason != "examples_lock"),
        "examples_lock_violations": [
            {"cluster_id": cid, "ticker": tk, "pnl_adr": pn}
            for cid, tk, pn in (verify_lock_violations or [])
        ],
        "halted": halted,
        "halt_reason": halt_reason,
        "cluster_meta": [
            {
                "cluster_id": m.cluster_id,
                "ticker": m.ticker,
                "is_example": m.is_example,
                "tag": m.tag,
                "signal_bar_idx": m.signal_bar_idx,
                "status": m.status,
                "reason": m.reason,
                "entry_k": m.entry_k,
                "entry_bar": m.entry_bar,
                "entry_date": m.entry_date,
                "cap_bar": m.cap_bar,
                "cap_date": m.cap_date,
                "cap_cause": (
                    "earnings" if m.cap_cause == CAP_EARNINGS
                    else ("time" if m.cap_cause == CAP_TIME else None)
                ),
                "horizon": m.horizon,
                "adr14_at_entry": m.adr14_at_entry,
                "effective_entry": m.effective_entry,
                "stop": m.stop,
                "stop_hit_bar": m.stop_hit_bar if m.status == "ENTERED" else None,
                "mfe_adr": m.mfe_adr,
            }
            for m in meta
        ],
        "top_conditions": [
            {
                "expression": r["expression"],
                "direction": r["direction"],
                "threshold": r["threshold"],
                "aggregate_pnl_adr": float(round(r["aggregate_pnl_adr"], 4)),
                "aggregate_summary": r["aggregate_summary"],
                "per_cluster_pnl_adr": r["per_cluster_pnl_adr"],
                "per_cluster_exit_bar_offset": r["per_cluster_exit_bar_offset"],
                "per_cluster_final_label": r["per_cluster_final_label"],
                "per_cluster_exit_cause": r["per_cluster_exit_cause"],
            }
            for r in top
        ],
    }

    fname = f"signal_exit_pool_{setup}.json"
    if halted:
        fname = f"signal_exit_pool_{setup}_HALTED_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path = os.path.join(OUTPUT_DIR, fname)
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"  Wrote {out_path}")
    return out_path


# ============================================================
# Main
# ============================================================
def run_setup(setup, ohlcv, earnings_map, expr_cache, n_thresholds, top_n):
    print(f"\n{'=' * 60}\n  SIGNAL EXIT POOL GRINDER — {setup.upper()}\n{'=' * 60}")
    tags_json = load_tags(setup)
    n_clusters_pool = len(tags_json["clusters"])

    print("\n  PHASE 1: build cluster meta (ADR14 at entry, cap, stop_hit_bar, mfe_adr)")
    t0 = time.time()
    meta = build_cluster_meta(tags_json, ohlcv, earnings_map)
    n_entered = sum(1 for m in meta if m.status == "ENTERED")
    n_not_entry = sum(1 for m in meta if m.status == "SKIPPED_NOT_ENTRY")
    n_skipped = sum(1 for m in meta if m.status == "SKIPPED_MISSING_DATA")
    print(f"  meta built in {time.time()-t0:.1f}s — entered={n_entered}  not_entry={n_not_entry}  skipped_missing={n_skipped}")

    if n_entered == 0:
        print("  No entered clusters — aborting setup.")
        write_output(setup, [], meta, [], n_clusters_pool, halted=True, halt_reason="no_entered_clusters",
                     verify_lock_violations=None)
        return

    entered = [m for m in meta if m.status == "ENTERED"]
    entered_pool_indices = [i for i, m in enumerate(meta) if m.status == "ENTERED"]

    print("\n  PHASE 2: build forward expression slab")
    slab, dropped = build_expr_slab(entered, expr_cache, expr_cache.n_expressions)
    if dropped:
        print(f"  WARN: dropped {len(dropped)} clusters from slab build (expr-cache misalignment)")
        for idx, reason in dropped[:5]:
            m = entered[idx]
            print(f"    {m.ticker} cluster {m.cluster_id}: {reason}")
        for idx, reason in dropped:
            entered_meta = entered[idx]
            entered_meta.status = "SKIPPED_MISSING_DATA"
            entered_meta.reason = "expr cache misalign: " + reason
        keep_mask = [entered[idx].status == "ENTERED" for idx in range(len(entered))]
        if not all(keep_mask):
            keep_idxs = [i for i, k in enumerate(keep_mask) if k]
            slab = slab[keep_idxs]
            entered = [entered[i] for i in keep_idxs]
            entered_pool_indices = [entered_pool_indices[i] for i in keep_idxs]
            print(f"  After drop: entered={len(entered)}")

    if len(entered) == 0:
        print("  No usable entered clusters after expr-cache alignment — aborting setup.")
        write_output(setup, [], meta, dropped, n_clusters_pool,
                     halted=True, halt_reason="all_clusters_dropped",
                     verify_lock_violations=None)
        return

    n_ent = len(entered)
    max_h = slab.shape[1]
    close_mat = np.full((n_ent, max_h), np.nan, dtype=np.float64)
    stop_bars = np.empty(n_ent, dtype=np.int64)
    horizons = np.empty(n_ent, dtype=np.int64)
    effs = np.empty(n_ent, dtype=np.float64)
    adrs = np.empty(n_ent, dtype=np.float64)
    cap_causes = np.empty(n_ent, dtype=np.int64)
    mfe_adrs = np.empty(n_ent, dtype=np.float64)
    for i, m in enumerate(entered):
        close_mat[i, : m.horizon] = m.forward_closes
        stop_bars[i] = m.stop_hit_bar
        horizons[i] = m.horizon
        effs[i] = m.effective_entry
        adrs[i] = m.adr14_at_entry
        cap_causes[i] = m.cap_cause
        mfe_adrs[i] = m.mfe_adr

    print("\n  PHASE 3: grind")
    top = grind(slab, close_mat, stop_bars, horizons, effs, adrs, cap_causes,
                expr_cache.expr_names, n_thresholds, top_n)

    del slab

    print("\n  PHASE 4: enrich top with labels + aggregate summary")
    enrich_top(top, mfe_adrs)

    print("\n  PHASE 5: expand per-cluster arrays to pool order")
    expand_per_cluster(top, meta, entered_pool_indices)

    print("\n  PHASE 6: verify")
    lock_pass, lock_violations = verify_examples_lock(top, meta)
    agg_pass, agg_value = verify_aggregate_positive(top)
    print(f"  examples_lock: {'PASS' if lock_pass else 'FAIL'}  ({len(lock_violations) if not lock_pass else 0} violations)")
    print(f"  aggregate>0:   {'PASS' if agg_pass else 'FAIL'}  (top aggregate = {agg_value:.2f})")

    halted = False
    halt_reason = ""
    if not lock_pass:
        halted = True; halt_reason = "examples_lock"
        print("  HALTED — examples-lock violations:")
        for cid, tk, pn in lock_violations:
            print(f"    cluster {cid} {tk} pnl_adr={pn}")
    elif not agg_pass:
        halted = True; halt_reason = "aggregate_not_positive"
        print(f"  HALTED — top aggregate {agg_value:.2f} <= 0")

    write_output(setup, top, meta, dropped, n_clusters_pool, halted, halt_reason,
                 lock_violations if not lock_pass else None)

    if top and not halted:
        r = top[0]
        s = r["aggregate_summary"]
        print(f"\n  TOP RULE: {r['expression']} {r['direction']} {r['threshold']}")
        print(f"    aggregate_pnl_adr = {r['aggregate_pnl_adr']:.2f}")
        wr = s.get("win_rate")
        cap_frac = s.get("aggregate_pnl_capture_fraction")
        print(f"    win_rate = {wr if wr is not None else 'n/a'}  "
              f"(n_win={s['n_win']}  n_loss={s['n_loss']})")
        print(f"    aggregate_mfe_adr = {s['aggregate_mfe_adr']:.2f}  "
              f"capture_fraction = {cap_frac if cap_frac is not None else 'n/a'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", required=True, choices=SUPPORTED_SETUPS,
                        help="Setup to run (one of htf, bf, base)")
    parser.add_argument("--n-thresholds", type=int, default=N_THRESHOLDS_DEFAULT)
    parser.add_argument("--top-n", type=int, default=TOP_N_SAVED)
    args = parser.parse_args()

    print(f"  SCRIPT_DIR     = {SCRIPT_DIR}")
    print(f"  WORKTREE_ROOT  = {WORKTREE_ROOT}")
    print(f"  MAIN_REPO_ROOT = {MAIN_REPO_ROOT}")
    print(f"  CLASSIFIER_REPO= {CLASSIFIER_REPO}")
    print(f"  OUTPUT_DIR     = {OUTPUT_DIR}")

    print("\n  Loading expression cache...")
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        sys.exit("  Expression cache invalid; run expr_cache_builder.py --build in main repo.")
    n_expr = expr_cache.n_expressions
    print(f"  Expression cache: {n_expr:,} expressions")
    if n_expr < MIN_N_EXPRESSIONS:
        sys.exit(f"  ABORT: n_expressions {n_expr} < floor {MIN_N_EXPRESSIONS}")

    ohlcv = load_ohlcv()
    earnings_map = load_earnings_map()

    t_total = time.time()
    run_setup(args.setup, ohlcv, earnings_map, expr_cache, args.n_thresholds, args.top_n)
    print(f"\n  TOTAL: {time.time()-t_total:.1f}s")


if __name__ == "__main__":
    main()
