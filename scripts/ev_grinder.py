"""
EV Grinder — Phase 3 Correlative Scoring Engine.

Scores every signal from Phase 2 with predicted win rate, predicted MFE,
and expected value. Also replays refinement conditions to build the
refinement depth slider data.

See EV_GRINDER.md for full spec.

Usage:
    python scripts/ev_grinder.py --setup dtss

Inputs:
    - raw_signal_clusters_{setup}.json  (pre-refinement, 893 clusters)
    - refinement_{setup}_*.json         (post-refinement, 467 signals)
    - Expression series cache           (local_runner/cache/expr_series/)

Output:
    - ev_{setup}_{timestamp}.json       (local + Railway mirror)

Increment 1: Data loaders + refinement depth replay.
"""

import os
import sys
import glob
import json
import time
import argparse
import numpy as np
from datetime import datetime, timezone
from collections import Counter

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")

sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)


# ══════════════════════════════════════════════════════════════
# FILE FINDERS
# ══════════════════════════════════════════════════════════════

def find_latest_refinement(setup_type):
    """Find the most recent refinement JSON for a setup type.

    Looks for refinement_{setup}_*.json in the cache directory.
    Returns (filepath, filename) or (None, None).
    """
    pattern = os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None, None

    # Sort by timestamp embedded in filename (YYYYMMDD_HHMMSS)
    def _extract_ts(path):
        bn = os.path.basename(path).replace(".json", "")
        parts = bn.split("_")
        # Last two parts should be date and time
        if len(parts) >= 2:
            ts = parts[-2] + parts[-1]
            if len(ts) == 14 and ts.isdigit():
                return ts
        return "0"

    candidates.sort(key=_extract_ts, reverse=True)
    path = candidates[0]
    return path, os.path.basename(path)


def find_raw_clusters(setup_type):
    """Find the raw signal clusters file (latest pointer) for a setup type.

    Returns (filepath, filename) or (None, None).
    """
    # Prefer the latest pointer (no timestamp)
    latest = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}.json")
    if os.path.exists(latest):
        return latest, os.path.basename(latest)

    # Fall back to timestamped files
    pattern = os.path.join(CACHE_DIR, f"raw_signal_clusters_{setup_type}_*.json")
    candidates = glob.glob(pattern)
    if not candidates:
        return None, None

    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    path = candidates[0]
    return path, os.path.basename(path)


# ══════════════════════════════════════════════════════════════
# DATA LOADERS
# ══════════════════════════════════════════════════════════════

def load_clusters(path):
    """Load raw signal clusters and normalize to common signal format.

    Returns list of signal dicts with keys:
        ticker, date, bar_idx, close, classification, move_adr,
        adr_at_signal, entry_high, is_example, cluster_id,
        leftward_bar_idxs
    """
    with open(path) as f:
        data = json.load(f)

    clusters = data.get("clusters", [])
    signals = []

    for c in clusters:
        # Collect all bar indices for this cluster (rightmost + leftward)
        leftward_idxs = [b["bar_idx"] for b in c.get("leftward", [])]

        signals.append({
            "ticker": c["ticker"],
            "date": c["rightmost"]["date"],
            "bar_idx": c["rightmost"]["bar_idx"],
            "close": c["rightmost"].get("close"),
            "classification": c.get("classification", "UNKNOWN"),
            "move_adr": c.get("move_adr"),
            "adr_at_signal": c.get("adr_at_signal"),
            "entry_high": c.get("entry_high"),
            "is_example": bool(c.get("is_example", 0)),
            "cluster_id": c["cluster_id"],
            "leftward_bar_idxs": leftward_idxs,
        })

    return signals, data


def load_refinement(path):
    """Load refinement output.

    Returns:
        refinement_conditions: list of condition dicts in lock order
        post_signals: list of signal dicts (winners + surviving losers)
        eliminated_signals: list of eliminated loser signal dicts
        data: full raw JSON
    """
    with open(path) as f:
        data = json.load(f)

    refinement_conditions = data.get("refinement_conditions_only", [])

    # Post-refinement signals = winners + surviving losers
    winners = data.get("winner_signals", [])
    losers = data.get("loser_signals", [])
    eliminated = data.get("eliminated_signals", [])

    post_signals = winners + losers

    return refinement_conditions, post_signals, eliminated, data


# ══════════════════════════════════════════════════════════════
# SIGNAL STATS COMPUTATION
# ══════════════════════════════════════════════════════════════

def compute_signal_stats(signals):
    """Compute summary stats for a set of signals.

    Returns dict with: total, winners, losers, wr, peak_day, avg_day,
    avg_week, avg_month, avg_year.
    """
    total = len(signals)
    if total == 0:
        return {
            "total": 0, "winners": 0, "losers": 0, "wr": 0.0,
            "peak_day": 0, "avg_day": 0.0, "avg_week": 0.0,
            "avg_month": 0.0, "avg_year": 0.0,
        }

    winners = sum(1 for s in signals if "WIN" in s.get("classification", ""))
    losers = total - winners
    wr = winners / total if total > 0 else 0.0

    # Date-based stats
    dates = [s["date"] for s in signals]
    date_counts = Counter(dates)

    peak_day = max(date_counts.values()) if date_counts else 0
    n_active_days = len(date_counts)
    avg_day = total / n_active_days if n_active_days > 0 else 0.0

    # For avg/week, avg/month, avg/year: use calendar span
    if dates:
        sorted_dates = sorted(dates)
        first = sorted_dates[0]
        last = sorted_dates[-1]
        # Simple span calculation from date strings
        from datetime import date as dt_date
        d_first = dt_date.fromisoformat(first)
        d_last = dt_date.fromisoformat(last)
        span_days = (d_last - d_first).days + 1

        n_weeks = max(span_days / 7, 1)
        n_months = max(span_days / 30.44, 1)
        n_years = max(span_days / 365.25, 1)

        avg_week = total / n_weeks
        avg_month = total / n_months
        avg_year = total / n_years
    else:
        avg_week = avg_month = avg_year = 0.0

    return {
        "total": total,
        "winners": winners,
        "losers": losers,
        "wr": round(wr, 4),
        "peak_day": peak_day,
        "avg_day": round(avg_day, 2),
        "avg_week": round(avg_week, 2),
        "avg_month": round(avg_month, 2),
        "avg_year": round(avg_year, 2),
    }


# ══════════════════════════════════════════════════════════════
# REFINEMENT DEPTH REPLAY
# ══════════════════════════════════════════════════════════════

def replay_refinement_depth(all_signals, refinement_conditions, expr_cache):
    """Replay refinement conditions against all clusters to determine
    which condition killed which losing clusters.

    Uses greedy peeling: start with all 100 conditions applied,
    repeatedly remove the condition whose removal causes the least damage
    (fewest additional clusters coming back alive). This produces a smooth,
    monotonic peel sequence for the UI slider.

    Args:
        all_signals: list of signal dicts (893 pre-refinement signals)
                     each has cluster_id, ticker, bar_idx, leftward_bar_idxs,
                     classification
        refinement_conditions: list of 100 condition dicts in any order.
                               each has name, low, high
        expr_cache: ExprSeriesCache instance

    Returns:
        killed_at_depth: dict {cluster_id: depth} for each loser cluster
                         that gets eliminated. depth is 1-100 where 1 = first
                         condition applied (last peeled), 100 = last condition
                         applied (first peeled). None for clusters that survive
                         all conditions.
        depth_stats: list of 101 stat dicts (depth 0 through 100)
        peel_sequence: list of 100 condition dicts with clusters_killed info
    """
    print("\n  ── REFINEMENT DEPTH REPLAY ──")
    t0 = time.time()

    n_conditions = len(refinement_conditions)
    if n_conditions == 0:
        print("  No refinement conditions — nothing to replay.")
        stats = compute_signal_stats(all_signals)
        return {}, [stats], []

    # ── Step 1: Identify losing clusters and their bars ──
    losing_clusters = {}  # cluster_id → list of (ticker, bar_idx)
    for sig in all_signals:
        if "LOSS" in sig.get("classification", ""):
            cid = sig["cluster_id"]
            bars = [(sig["ticker"], sig["bar_idx"])]
            for lw_idx in sig.get("leftward_bar_idxs", []):
                bars.append((sig["ticker"], lw_idx))
            losing_clusters[cid] = bars

    n_losing = len(losing_clusters)
    print(f"  Losing clusters: {n_losing}")
    print(f"  Refinement conditions: {n_conditions}")

    # ── Step 2: Map condition names to expr cache column indices ──
    cond_col_indices = []
    for cond in refinement_conditions:
        col_idx = expr_cache.expr_index(cond["name"])
        if col_idx is None:
            raise RuntimeError(
                f"Refinement condition '{cond['name']}' not found in expression cache. "
                f"Cache may be stale — rebuild with expr_cache_builder.py --build"
            )
        cond_col_indices.append(col_idx)

    # ── Step 3: For each bar in each losing cluster, check all conditions ──
    # Build a boolean matrix: per_bar_passes[cluster_id][(ticker, bar_idx)][cond_idx] = True/False
    # A bar "passes" a condition if its value is within [low, high] OR is NaN.
    # A cluster is "alive" if ANY of its bars passes ALL applied conditions.

    # Load expression data per ticker (cache ticker loads to avoid repeated I/O)
    print(f"  Loading expression data for losing cluster bars...")
    ticker_cache = {}  # ticker → (dates, data)

    # Collect all unique tickers needed
    tickers_needed = set()
    for bars in losing_clusters.values():
        for (tk, _) in bars:
            tickers_needed.add(tk)

    for tk in tickers_needed:
        dates, data = expr_cache.get_ticker(tk)
        if dates is None or data is None:
            raise RuntimeError(
                f"Ticker '{tk}' not in expression cache but has a losing cluster. "
                f"This should not happen — signals came from scanning this cache."
            )
        ticker_cache[tk] = data  # Only need the data array, not dates

    print(f"  Loaded {len(ticker_cache)} tickers from expression cache")

    # Build pass matrix: for each (cluster_id, bar), does each condition pass?
    # cluster_bar_passes[cluster_id] = list of boolean arrays, one per bar
    # Each boolean array has shape (n_conditions,) — True if bar passes that condition
    print(f"  Computing per-bar condition pass/fail...")

    cluster_bar_cond_passes = {}  # cluster_id → np.array (n_bars_in_cluster, n_conditions)

    for cid, bars in losing_clusters.items():
        n_bars = len(bars)
        passes = np.ones((n_bars, n_conditions), dtype=bool)

        for bi, (tk, bar_idx) in enumerate(bars):
            data = ticker_cache[tk]
            if bar_idx >= data.shape[0]:
                raise RuntimeError(
                    f"bar_idx {bar_idx} >= cached bars {data.shape[0]} for {tk}. "
                    f"Expression cache is stale or clusters are from a different cache version."
                )
            for ci, (cond, col_idx) in enumerate(zip(refinement_conditions, cond_col_indices)):
                val = float(data[bar_idx, col_idx])
                if np.isnan(val):
                    passes[bi, ci] = True  # NaN = pass (matches refinement grinder)
                else:
                    passes[bi, ci] = (val >= cond["low"] and val <= cond["high"])

        cluster_bar_cond_passes[cid] = passes

    # Free ticker cache — no longer needed
    del ticker_cache

    # ── Step 4: Verify full-depth elimination matches refinement output ──
    # With all 100 conditions applied, a bar is alive if it passes ALL conditions.
    # A cluster is alive if ANY bar is alive.
    print(f"\n  Verifying full-depth elimination...")

    def count_alive_clusters(active_condition_mask):
        """Count how many losing clusters have at least one bar alive.

        active_condition_mask: boolean array (n_conditions,) — True for applied conditions.
        """
        alive = 0
        for cid, passes in cluster_bar_cond_passes.items():
            # passes shape: (n_bars, n_conditions)
            # A bar is alive if it passes ALL active conditions
            # passes[:, active_condition_mask] gives (n_bars, n_active) — all must be True
            active_passes = passes[:, active_condition_mask]
            bar_alive = np.all(active_passes, axis=1)  # shape: (n_bars,)
            if np.any(bar_alive):
                alive += 1
        return alive

    all_active = np.ones(n_conditions, dtype=bool)
    surviving_at_full_depth = count_alive_clusters(all_active)
    eliminated_at_full_depth = n_losing - surviving_at_full_depth

    print(f"  Full depth ({n_conditions} conditions):")
    print(f"    Surviving losers: {surviving_at_full_depth}")
    print(f"    Eliminated losers: {eliminated_at_full_depth}")

    # ── Step 5: Greedy peel ──
    # Start with all conditions applied. At each step, find the condition
    # whose removal causes the fewest clusters to come back alive. Remove it.
    # Record the peel order.
    print(f"\n  Running greedy peel...")

    active_mask = np.ones(n_conditions, dtype=bool)
    peel_order = []  # list of condition indices, in order of removal
    peel_details = []  # per-peel stats

    for peel_step in range(n_conditions):
        # Find which condition to remove: the one that causes least damage
        best_cond_idx = None
        best_alive_after = -1  # we want the minimum alive increase

        # Current alive count
        current_alive = count_alive_clusters(active_mask)

        for ci in range(n_conditions):
            if not active_mask[ci]:
                continue  # already removed

            # Try removing this condition
            test_mask = active_mask.copy()
            test_mask[ci] = False
            alive_after = count_alive_clusters(test_mask)

            # We want to remove the condition that causes the smallest increase
            # in alive clusters (least damage from removal)
            if best_cond_idx is None or alive_after < best_alive_after:
                best_alive_after = alive_after
                best_cond_idx = ci
            elif alive_after == best_alive_after:
                # Tie-break: prefer removing the condition with lower filter_power
                # (i.e., the weaker condition)
                pass  # first found wins, which is fine

        # Remove the best condition
        active_mask[best_cond_idx] = False
        clusters_came_back = best_alive_after - current_alive

        peel_order.append(best_cond_idx)
        peel_details.append({
            "peel_step": peel_step + 1,
            "condition_removed_idx": best_cond_idx,
            "condition_removed_name": refinement_conditions[best_cond_idx]["name"],
            "clusters_came_back": clusters_came_back,
            "losers_alive_after": best_alive_after,
        })

        if (peel_step + 1) % 20 == 0 or peel_step == 0 or peel_step == n_conditions - 1:
            depth_remaining = n_conditions - (peel_step + 1)
            print(f"    Peel {peel_step + 1:3d}: removed {refinement_conditions[best_cond_idx]['name']:40s} "
                  f"+{clusters_came_back:3d} clusters back  "
                  f"({best_alive_after} losers alive, depth={depth_remaining})")

    # ── Step 6: Build depth mapping ──
    # depth 0 = no conditions applied (all losers alive)
    # depth N = N conditions applied (in REVERSE peel order — last peeled = first applied)
    # The peel order tells us: peel_order[0] was removed first (= weakest = applied last)
    # So the application order is reversed: peel_order[-1] is applied first (depth 1),
    # peel_order[0] is applied last (depth 100).

    application_order = list(reversed(peel_order))  # condition indices in order of application

    # For each losing cluster, find the depth at which it dies
    print(f"\n  Computing killed_at_depth per cluster...")
    killed_at_depth = {}  # cluster_id → depth (1-100) or None

    for cid, passes in cluster_bar_cond_passes.items():
        # Walk through conditions in application order
        # At each depth, check if the cluster is still alive
        # A bar is alive if it passes all conditions applied so far
        bar_alive = np.ones(passes.shape[0], dtype=bool)  # all bars start alive

        cluster_killed = False
        for depth_idx, cond_idx in enumerate(application_order):
            depth = depth_idx + 1  # depth is 1-indexed
            # Apply this condition: bar stays alive only if it also passes this one
            bar_alive = bar_alive & passes[:, cond_idx]

            if not np.any(bar_alive):
                # All bars dead — cluster killed at this depth
                killed_at_depth[cid] = depth
                cluster_killed = True
                break

        if not cluster_killed:
            # Cluster survives all conditions — killed_at_depth is None
            pass  # not added to dict = None

    n_killed = len(killed_at_depth)
    n_survive = n_losing - n_killed
    print(f"  Clusters killed: {n_killed}")
    print(f"  Clusters surviving all conditions: {n_survive}")

    # ── Step 7: Build depth_stats array ──
    print(f"\n  Building depth_stats...")

    # Map cluster_id → signal for quick lookup
    cluster_to_signal = {}
    for sig in all_signals:
        if "LOSS" in sig.get("classification", ""):
            cluster_to_signal[sig["cluster_id"]] = sig

    # Winners are always alive
    winner_signals = [s for s in all_signals if "WIN" in s.get("classification", "")]

    depth_stats = []
    for depth in range(n_conditions + 1):
        # At this depth, which losers are alive?
        alive_loser_signals = []
        for cid, sig in cluster_to_signal.items():
            kill_depth = killed_at_depth.get(cid)
            if kill_depth is None or depth < kill_depth:
                # Alive: either survives all conditions, or not yet killed
                alive_loser_signals.append(sig)

        alive_signals = winner_signals + alive_loser_signals
        stats = compute_signal_stats(alive_signals)
        stats["depth"] = depth
        depth_stats.append(stats)

    # ── Step 8: Build refinement_depth_map (conditions in application order) ──
    conditions_in_order = []
    for depth_idx, cond_idx in enumerate(application_order):
        depth = depth_idx + 1
        cond = refinement_conditions[cond_idx]

        # Which clusters does this condition kill?
        # (clusters whose killed_at_depth == this depth)
        clusters_killed_here = [
            cid for cid, kd in killed_at_depth.items() if kd == depth
        ]

        conditions_in_order.append({
            "idx": depth_idx,
            "depth": depth,
            "name": cond["name"],
            "low": cond["low"],
            "high": cond["high"],
            "clusters_killed": clusters_killed_here,
            "cumulative_losers_remaining": depth_stats[depth]["losers"],
            "cumulative_winners": depth_stats[depth]["winners"],
            "cumulative_total": depth_stats[depth]["total"],
            "cumulative_wr": depth_stats[depth]["wr"],
            "cumulative_peak": depth_stats[depth]["peak_day"],
            "cumulative_avg": depth_stats[depth]["avg_day"],
        })

    elapsed = time.time() - t0
    print(f"\n  Refinement replay complete ({elapsed:.1f}s)")

    return killed_at_depth, depth_stats, conditions_in_order


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type):
    """Run the EV grinder for a setup type."""
    print("\n" + "=" * 70)
    print("  EV GRINDER — Phase 3 Correlative Scoring")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Time:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    t_total = time.time()

    # ── Load raw clusters (pre-refinement) ──
    clusters_path, clusters_file = find_raw_clusters(setup_type)
    if clusters_path is None:
        print(f"\n  ERROR: No raw signal clusters file found for {setup_type}")
        print(f"  Run refinement grind first.")
        return None
    print(f"\n  Raw clusters: {clusters_file}")

    all_signals, clusters_raw = load_clusters(clusters_path)
    n_pre_win = sum(1 for s in all_signals if "WIN" in s["classification"])
    n_pre_loss = sum(1 for s in all_signals if "LOSS" in s["classification"])
    n_examples = sum(1 for s in all_signals if s["is_example"])
    print(f"  Pre-refinement: {len(all_signals)} signals ({n_pre_win} WIN, {n_pre_loss} LOSS, {n_examples} examples)")

    # ── Load refinement output (post-refinement) ──
    ref_path, ref_file = find_latest_refinement(setup_type)
    if ref_path is None:
        print(f"\n  ERROR: No refinement file found for {setup_type}")
        print(f"  Run refinement grind first.")
        return None
    print(f"\n  Refinement: {ref_file}")

    ref_conditions, post_signals, eliminated_signals, ref_raw = load_refinement(ref_path)
    n_post_win = sum(1 for s in post_signals if "WIN" in s.get("classification", ""))
    n_post_loss = sum(1 for s in post_signals if "LOSS" in s.get("classification", ""))
    print(f"  Post-refinement: {len(post_signals)} signals ({n_post_win} WIN, {n_post_loss} LOSS)")
    print(f"  Eliminated: {len(eliminated_signals)}")
    print(f"  Refinement conditions: {len(ref_conditions)}")

    # ── Load expression cache ──
    print(f"\n  Loading expression cache...")
    from expr_cache_builder import ExprSeriesCache
    expr_cache = ExprSeriesCache()
    if not expr_cache.is_valid():
        print(f"  ERROR: Expression cache not found or invalid.")
        print(f"  Run: python local_runner/expr_cache_builder.py --build")
        return None
    print(f"  Expression cache: {expr_cache.n_expressions} expressions")

    # ── Refinement depth replay ──
    killed_at_depth, depth_stats, peel_sequence = replay_refinement_depth(
        all_signals, ref_conditions, expr_cache
    )

    # ── Verification ──
    print(f"\n  ── VERIFICATION ──")
    d0 = depth_stats[0]
    d100 = depth_stats[-1] if len(depth_stats) > 100 else depth_stats[len(ref_conditions)]

    print(f"  Depth  0: {d0['total']:>5} signals ({d0['winners']}W + {d0['losers']}L) "
          f"WR={d0['wr']:.1%}  peak={d0['peak_day']}/day  avg={d0['avg_day']:.1f}/day")

    for check_depth in [25, 50, 75]:
        if check_depth < len(depth_stats):
            d = depth_stats[check_depth]
            print(f"  Depth {check_depth:2d}: {d['total']:>5} signals ({d['winners']}W + {d['losers']}L) "
                  f"WR={d['wr']:.1%}  peak={d['peak_day']}/day  avg={d['avg_day']:.1f}/day")

    max_depth = len(ref_conditions)
    d_max = depth_stats[max_depth]
    print(f"  Depth{max_depth:3d}: {d_max['total']:>5} signals ({d_max['winners']}W + {d_max['losers']}L) "
          f"WR={d_max['wr']:.1%}  peak={d_max['peak_day']}/day  avg={d_max['avg_day']:.1f}/day")

    # Cross-check against known values
    ok = True
    if d0["total"] != len(all_signals):
        print(f"\n  ✗ FAIL: Depth 0 total {d0['total']} != expected {len(all_signals)}")
        ok = False
    if d0["winners"] != n_pre_win:
        print(f"  ✗ FAIL: Depth 0 winners {d0['winners']} != expected {n_pre_win}")
        ok = False
    if d0["losers"] != n_pre_loss:
        print(f"  ✗ FAIL: Depth 0 losers {d0['losers']} != expected {n_pre_loss}")
        ok = False
    if d_max["total"] != len(post_signals):
        print(f"\n  ✗ FAIL: Depth {max_depth} total {d_max['total']} != expected {len(post_signals)}")
        ok = False
    if d_max["winners"] != n_post_win:
        print(f"  ✗ FAIL: Depth {max_depth} winners {d_max['winners']} != expected {n_post_win}")
        ok = False
    if d_max["losers"] != n_post_loss:
        print(f"  ✗ FAIL: Depth {max_depth} losers {d_max['losers']} != expected {n_post_loss}")
        ok = False

    if ok:
        print(f"\n  ✓ All verification checks passed")
    else:
        print(f"\n  ✗ Verification FAILED — see errors above")

    # ── Summary ──
    total_time = time.time() - t_total
    print(f"\n  {'=' * 50}")
    print(f"  INCREMENT 1 COMPLETE ({total_time:.1f}s)")
    print(f"  {'=' * 50}")
    print(f"  Pre-refinement signals: {len(all_signals)}")
    print(f"  Post-refinement signals: {len(post_signals)}")
    print(f"  Refinement conditions: {len(ref_conditions)}")
    print(f"  Clusters killed: {len(killed_at_depth)}")
    print(f"  Depth stats entries: {len(depth_stats)}")
    print(f"  Peel sequence entries: {len(peel_sequence)}")

    return {
        "all_signals": all_signals,
        "post_signals": post_signals,
        "ref_conditions": ref_conditions,
        "killed_at_depth": killed_at_depth,
        "depth_stats": depth_stats,
        "peel_sequence": peel_sequence,
        "clusters_file": clusters_file,
        "refinement_file": ref_file,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="EV Grinder — Phase 3")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    args = parser.parse_args()

    result = run(args.setup)
    if result is None:
        sys.exit(1)
