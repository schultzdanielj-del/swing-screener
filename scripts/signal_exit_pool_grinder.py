"""
Signal Exit Pool Grinder — L14 labeler.

Per CLASSIFIER_SPEC.md §2 (fixed constraints), §15 (labeler mechanic),
§17.7 (entry-tag mechanic), and SIGNAL_EXIT_GRINDER.md.

For a setup in {htf, bf, base}:

  1. Read <classifier worktree>/research/classifier_tags/{setup}_tags.json.
  2. Filter to tag == "ENTRY". Non-ENTRY rows keep pool-order slots with
     SKIPPED_NOT_ENTRY status.
  3. Build cluster meta per ENTRY row (§2 c5/c7/c9a):
       ADR14 recomputed at entry_idx from OHLCV.
       effective_entry = min(entry_high, entry_low + 1·ADR14).
       stop = entry_low.
       cap_bar = min(entry+120, end_of_tape, earnings_bar-1).
       stop_hit_bar = first j where low[entry+1+j] <= stop, else horizon.
       eff_horizon = min(stop_hit_bar + 1, horizon).
       mfe_during_life = (max(high[entry+1 .. entry+eff_horizon]) - eff) / ADR14.
       mfe_full_window = (max(high[entry+1 .. cap_bar]) - eff) / ADR14
                          (diagnostic — labeler does not use; preserved for downstream).
  4. T = min_{examples} mfe_during_life.
  5. final_label = WIN iff mfe_during_life >= T else LOSS.
     Lock by construction: every example has mfe_during_life >= T trivially.
  6. Verify lock; halt on failure (should never trigger by construction).
  7. Write output.

Output: <WORKTREE>/data/signal_exit_grind/signal_exit_pool_{setup}.json
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


# ============================================================
# Constants
# ============================================================
OHLCV_PATH = os.path.join(MAIN_REPO_ROOT, "local_runner", "cache", "universe_ohlcv_daily.pkl")
DB_PATH = os.path.join(MAIN_REPO_ROOT, "data", "scanperfect.db")
TAGS_DIR = os.path.join(CLASSIFIER_REPO, "research", "classifier_tags")
OUTPUT_DIR = os.path.join(WORKTREE_ROOT, "data", "signal_exit_grind")

EXPECTED_MIN_TICKERS = 11200
TRADE_LIFETIME_CAP_BARS = 120
ADR_LOOKBACK = 14

SUPPORTED_SETUPS = ("htf", "bf", "base")

# Cap-cause codes
CAP_EARNINGS = 0
CAP_TIME = 1


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
    entry_bar: Optional[int] = None
    entry_date: Optional[str] = None
    cap_bar: Optional[int] = None
    cap_date: Optional[str] = None
    cap_cause: Optional[int] = None
    horizon: int = 0
    eff_horizon: Optional[int] = None
    entry_high: Optional[float] = None
    entry_low: Optional[float] = None
    entry_close: Optional[float] = None
    adr14_at_entry: Optional[float] = None
    effective_entry: Optional[float] = None
    stop: Optional[float] = None
    stop_hit_bar: Optional[int] = None
    mfe_during_life: Optional[float] = None
    mfe_full_window: Optional[float] = None
    final_label: Optional[str] = None


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
                    reason="cap_bar <= entry_bar",
                )))
                continue

            if earnings_cap is not None and cap_bar == earnings_cap:
                cap_cause = CAP_EARNINGS
            else:
                cap_cause = CAP_TIME

            horizon = cap_bar - entry_bar
            fwd_lows = lows[entry_bar + 1: cap_bar + 1]
            fwd_highs = highs[entry_bar + 1: cap_bar + 1]

            stop_mask = fwd_lows <= stop
            stop_hit_bar = int(np.argmax(stop_mask)) if stop_mask.any() else horizon
            eff_horizon = min(int(stop_hit_bar) + 1, horizon)
            if eff_horizon == 0:
                meta_list.append((i, ClusterMeta(
                    cluster_id=c["cluster_id"], ticker=ticker,
                    is_example=c.get("is_example", 0),
                    tag="ENTRY",
                    signal_bar_idx=sig_idx,
                    status="SKIPPED_MISSING_DATA",
                    reason="eff_horizon == 0 (cap or stop on bar 0)",
                )))
                continue

            mfe_during_life = float(
                (fwd_highs[: eff_horizon].max() - effective_entry) / adr14
            )
            mfe_full_window = float(
                (fwd_highs.max() - effective_entry) / adr14
            ) if fwd_highs.size > 0 else 0.0

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
                eff_horizon=eff_horizon,
                entry_high=float(e_high), entry_low=float(e_low), entry_close=float(e_close),
                adr14_at_entry=adr14,
                effective_entry=float(effective_entry), stop=float(stop),
                stop_hit_bar=stop_hit_bar,
                mfe_during_life=mfe_during_life,
                mfe_full_window=mfe_full_window,
            )))

    meta_list.sort(key=lambda x: x[0])
    return [m for _, m in meta_list]


# ============================================================
# Phase 2 — apply L14 labeler
# ============================================================
def apply_l14_labeler(meta: List[ClusterMeta]):
    """T = min over examples (forced WIN by ground truth) of mfe_during_life.
       final_label = WIN iff mfe_during_life >= T else LOSS.

       Returns (T, T_setting_example_meta, examples_lock_pass_count, n_examples_entered).
    """
    examples_entered = [m for m in meta if m.status == "ENTERED" and m.is_example == 1]
    if not examples_entered:
        return None, None, 0, 0
    ex_mfes = [m.mfe_during_life for m in examples_entered]
    T = float(min(ex_mfes))
    T_setting = next(m for m in examples_entered if m.mfe_during_life == T)

    for m in meta:
        if m.status != "ENTERED":
            continue
        m.final_label = "WIN" if m.mfe_during_life >= T else "LOSS"

    lock_pass = sum(1 for m in examples_entered if m.final_label == "WIN")
    return T, T_setting, lock_pass, len(examples_entered)


# ============================================================
# Phase 3 — write output
# ============================================================
def write_output(setup, meta, n_clusters_pool, T, T_setting, halted, halt_reason):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    n_entered = sum(1 for m in meta if m.status == "ENTERED")
    n_not_entry = sum(1 for m in meta if m.status == "SKIPPED_NOT_ENTRY")
    n_skipped = sum(1 for m in meta if m.status == "SKIPPED_MISSING_DATA")

    examples_entered = [m for m in meta if m.status == "ENTERED" and m.is_example == 1]
    wild_entered = [m for m in meta if m.status == "ENTERED" and m.is_example == 0]
    n_ex_pass = sum(1 for m in examples_entered if m.final_label == "WIN")
    n_wild_win = sum(1 for m in wild_entered if m.final_label == "WIN")
    n_wild_loss = sum(1 for m in wild_entered if m.final_label == "LOSS")
    examples_lock_passed = (n_ex_pass == len(examples_entered)) and len(examples_entered) > 0

    cap_cause_str = {CAP_EARNINGS: "earnings", CAP_TIME: "time"}

    output = {
        "setup_type": setup,
        "grinder_type": "signal_exit_pool_l14_labeler",
        "timestamp": datetime.now().isoformat(),
        "adr_source": "ADR14_at_entry (recomputed from OHLCV)",
        "trade_lifetime_cap_bars": TRADE_LIFETIME_CAP_BARS,
        "T_threshold_adr": T,
        "T_setting_example": (
            None if T_setting is None else {
                "cluster_id": T_setting.cluster_id,
                "ticker": T_setting.ticker,
                "horizon": T_setting.horizon,
                "stop_hit_bar": T_setting.stop_hit_bar,
                "eff_horizon": T_setting.eff_horizon,
                "mfe_during_life": T_setting.mfe_during_life,
            }
        ),
        "n_clusters_pool": n_clusters_pool,
        "n_entered": n_entered,
        "n_skipped_not_entry": n_not_entry,
        "n_skipped_missing_data": n_skipped,
        "n_examples_entered": len(examples_entered),
        "n_wild_entered": len(wild_entered),
        "n_wild_win": n_wild_win,
        "n_wild_loss": n_wild_loss,
        "wild_win_rate": (n_wild_win / len(wild_entered)) if wild_entered else None,
        "examples_lock_passed": bool(examples_lock_passed),
        "n_examples_lock_pass": n_ex_pass,
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
                "cap_cause": cap_cause_str.get(m.cap_cause),
                "horizon": m.horizon,
                "eff_horizon": m.eff_horizon,
                "adr14_at_entry": m.adr14_at_entry,
                "effective_entry": m.effective_entry,
                "stop": m.stop,
                "stop_hit_bar": m.stop_hit_bar,
                "mfe_during_life": m.mfe_during_life,
                "mfe_full_window": m.mfe_full_window,
                "final_label": m.final_label,
            }
            for m in meta
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
# Main per-setup runner
# ============================================================
def run_setup(setup, ohlcv, earnings_map):
    print(f"\n{'=' * 60}\n  SIGNAL EXIT POOL GRINDER — {setup.upper()} (L14 labeler)\n{'=' * 60}")
    tags_json = load_tags(setup)
    n_clusters_pool = len(tags_json["clusters"])

    print("\n  PHASE 1: build cluster meta (ADR14, eff_entry, stop, cap, stop_hit_bar, mfe_during_life)")
    t0 = time.time()
    meta = build_cluster_meta(tags_json, ohlcv, earnings_map)
    n_entered = sum(1 for m in meta if m.status == "ENTERED")
    n_not_entry = sum(1 for m in meta if m.status == "SKIPPED_NOT_ENTRY")
    n_skipped = sum(1 for m in meta if m.status == "SKIPPED_MISSING_DATA")
    print(f"  meta built in {time.time()-t0:.1f}s — entered={n_entered}  "
          f"not_entry={n_not_entry}  skipped_missing={n_skipped}")

    n_examples_entered = sum(1 for m in meta if m.status == "ENTERED" and m.is_example == 1)
    if n_examples_entered == 0:
        print("  No example clusters entered — aborting setup.")
        write_output(setup, meta, n_clusters_pool, None, None,
                     halted=True, halt_reason="no_examples_entered")
        return

    print(f"\n  PHASE 2: apply L14 labeler (T = min(example mfe_during_life))")
    T, T_setting, lock_pass, n_ex = apply_l14_labeler(meta)
    print(f"  T = {T:.4f} ADR  set by {T_setting.ticker} cluster {T_setting.cluster_id}  "
          f"(horizon={T_setting.horizon}, stop_hit_bar={T_setting.stop_hit_bar}, "
          f"eff_horizon={T_setting.eff_horizon}, mfe_during_life={T_setting.mfe_during_life:.3f})")
    print(f"  examples_lock: {lock_pass}/{n_ex}  "
          f"({'PASS' if lock_pass == n_ex else 'FAIL — should never happen by construction'})")

    halted = False
    halt_reason = ""
    if lock_pass != n_ex:
        halted = True
        halt_reason = "examples_lock"
        print("  HALTED — examples-lock failed (impossible by construction; investigate)")

    print("\n  PHASE 3: summary + write")
    wild_entered = [m for m in meta if m.status == "ENTERED" and m.is_example == 0]
    n_wild_win = sum(1 for m in wild_entered if m.final_label == "WIN")
    n_wild_loss = sum(1 for m in wild_entered if m.final_label == "LOSS")
    wr = (n_wild_win / len(wild_entered)) if wild_entered else 0.0
    print(f"  Wild WIN: {n_wild_win}/{len(wild_entered)} ({100*wr:.1f}%)  LOSS: {n_wild_loss}")

    if wild_entered:
        win_mfes = sorted([m.mfe_during_life for m in wild_entered if m.final_label == "WIN"], reverse=True)
        loss_mfes = sorted([m.mfe_during_life for m in wild_entered if m.final_label == "LOSS"])
        if win_mfes:
            print(f"  Top 5 wild WIN by mfe_during_life:")
            for m in sorted(wild_entered, key=lambda x: -(x.mfe_during_life or -1))[:5]:
                if m.final_label == "WIN":
                    print(f"    {m.ticker:>6s} cid={m.cluster_id:>4d}  mfe={m.mfe_during_life:>6.2f}  "
                          f"horizon={m.horizon:>3d}  stop_hit={m.stop_hit_bar}")
        if loss_mfes:
            print(f"  Bottom 5 wild LOSS by mfe_during_life:")
            for m in sorted(wild_entered, key=lambda x: (x.mfe_during_life or 1e9))[:5]:
                if m.final_label == "LOSS":
                    print(f"    {m.ticker:>6s} cid={m.cluster_id:>4d}  mfe={m.mfe_during_life:>6.2f}  "
                          f"horizon={m.horizon:>3d}  stop_hit={m.stop_hit_bar}")

    write_output(setup, meta, n_clusters_pool, T, T_setting, halted, halt_reason)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", required=True, choices=SUPPORTED_SETUPS,
                        help="Setup to run (one of htf, bf, base)")
    args = parser.parse_args()

    print(f"  SCRIPT_DIR     = {SCRIPT_DIR}")
    print(f"  WORKTREE_ROOT  = {WORKTREE_ROOT}")
    print(f"  MAIN_REPO_ROOT = {MAIN_REPO_ROOT}")
    print(f"  CLASSIFIER_REPO= {CLASSIFIER_REPO}")
    print(f"  OUTPUT_DIR     = {OUTPUT_DIR}")

    ohlcv = load_ohlcv()
    earnings_map = load_earnings_map()

    t_total = time.time()
    run_setup(args.setup, ohlcv, earnings_map)
    print(f"\n  TOTAL: {time.time()-t_total:.1f}s")


if __name__ == "__main__":
    main()
