"""
Consensus Engine — Multi-Run Stability Selection for Signal & Refinement Conditions.

Reads N grind outputs, matches conditions across runs by expression name,
counts frequency of appearance, applies consensus threshold + EPV cap,
outputs a locked condition set with stability metrics.

Based on Meinshausen & Bühlmann (2010) stability selection framework.
Recommended: 15 signal runs, 10 refinement runs, 0.7 consensus threshold.

Usage:
    python scripts/consensus_engine.py --setup dtss --stage signal --threshold 0.7
    python scripts/consensus_engine.py --setup dtss --stage refinement --threshold 0.7
    python scripts/consensus_engine.py --setup dtss --stage signal --list-runs
"""

import os
import sys
import json
import glob
import sqlite3
import argparse
from datetime import datetime, timezone
from collections import Counter, defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(REPO_ROOT, "local_runner")
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
DB_PATH = os.path.join(REPO_ROOT, "data", "scanperfect.db")

# ══════════════════════════════════════════════════════════════
# FILE DISCOVERY
# ══════════════════════════════════════════════════════════════

def find_signal_grind_files(setup_type, limit=None):
    """Find pyramid grind outputs for a setup (signal grind, not refinement)."""
    pattern = os.path.join(CACHE_DIR, f"pyramid_{setup_type}_*.json")
    candidates = []
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            # Skip refinement outputs
            if data.get("refinement"):
                continue
            n_conds = data.get("n_conditions", 0)
            if n_conds == 0:
                continue
            candidates.append({
                "path": path,
                "filename": os.path.basename(path),
                "timestamp": data.get("timestamp", ""),
                "n_conditions": n_conds,
                "total_time_s": data.get("total_time_s", 0),
                "summary": data.get("summary", {}),
                "mtime": os.path.getmtime(path),
            })
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    # Sort by modification time, newest first
    candidates.sort(key=lambda c: c["mtime"], reverse=True)
    if limit:
        candidates = candidates[:limit]
    return candidates


def find_refinement_grind_files(setup_type, limit=None):
    """Find refinement grind outputs for a setup."""
    pattern = os.path.join(CACHE_DIR, f"refinement_{setup_type}_*.json")
    candidates = []
    for path in glob.glob(pattern):
        try:
            with open(path) as f:
                data = json.load(f)
            n_conds = len(data.get("refinement_conditions_only", []))
            if n_conds == 0:
                continue
            candidates.append({
                "path": path,
                "filename": os.path.basename(path),
                "timestamp": data.get("timestamp", ""),
                "n_conditions": n_conds,
                "total_time_s": data.get("total_time_s", 0),
                "summary": data.get("summary", {}),
                "mtime": os.path.getmtime(path),
            })
        except Exception as e:
            print(f"  WARNING: Could not read {os.path.basename(path)}: {e}")
    candidates.sort(key=lambda c: c["mtime"], reverse=True)
    if limit:
        candidates = candidates[:limit]
    return candidates


# ══════════════════════════════════════════════════════════════
# CONDITION EXTRACTION
# ══════════════════════════════════════════════════════════════

def extract_signal_conditions(path):
    """Extract condition names + bounds from a signal grind output."""
    with open(path) as f:
        data = json.load(f)
    conditions = data.get("all_conditions", [])
    result = []
    for c in conditions:
        result.append({
            "name": c.get("name", c.get("expr", "")),
            "low": c.get("low"),
            "high": c.get("high"),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            "filter_power": c.get("filter_power"),
        })
    return result


def extract_refinement_conditions(path):
    """Extract condition names + bounds from a refinement grind output."""
    with open(path) as f:
        data = json.load(f)
    conditions = data.get("refinement_conditions_only", [])
    result = []
    for c in conditions:
        result.append({
            "name": c.get("name", c.get("expr", "")),
            "low": c.get("low"),
            "high": c.get("high"),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            "filter_power": c.get("filter_power"),
        })
    return result


# ══════════════════════════════════════════════════════════════
# CONSENSUS COMPUTATION
# ══════════════════════════════════════════════════════════════

def compute_consensus(all_run_conditions, n_runs, threshold=0.7, max_conditions=35):
    """Compute consensus across N runs.

    Matches conditions by expression name (exact match — the expression name
    uniquely identifies the condition, thresholds are derived from examples
    and will be recomputed from the locked example set).

    Args:
        all_run_conditions: list of lists, one per run, each containing condition dicts
        n_runs: total number of runs
        threshold: minimum selection frequency (0.0-1.0), default 0.7
        max_conditions: maximum conditions to keep (EPV cap)

    Returns:
        dict with consensus results
    """
    # Count how many runs each condition appears in
    condition_freq = Counter()
    condition_runs = defaultdict(list)  # name -> list of run indices
    condition_examples = {}  # name -> latest condition dict (for compute, category etc.)

    for run_i, conditions in enumerate(all_run_conditions):
        seen_this_run = set()
        for c in conditions:
            name = c["name"]
            if name not in seen_this_run:
                condition_freq[name] += 1
                seen_this_run.add(name)
            condition_runs[name].append(run_i)
            condition_examples[name] = c  # keep latest version for metadata

    # All unique conditions found across all runs
    all_names = sorted(condition_freq.keys(), key=lambda n: condition_freq[n], reverse=True)
    n_unique = len(all_names)

    # Apply consensus threshold
    min_appearances = max(1, int(n_runs * threshold))
    consensus_names = [name for name in all_names if condition_freq[name] >= min_appearances]

    # Apply condition cap (if provided — usually None, EPV enforced at grinder)
    if max_conditions is not None:
        capped_names = consensus_names[:max_conditions]
    else:
        capped_names = consensus_names

    # Build consensus condition list with metadata
    consensus_conditions = []
    for name in capped_names:
        c = condition_examples[name]
        consensus_conditions.append({
            "name": name,
            "frequency": condition_freq[name],
            "frequency_pct": round(condition_freq[name] / n_runs, 3),
            "runs_appeared": sorted(set(condition_runs[name])),
            "category": c.get("category", ""),
            "tier": c.get("tier", ""),
            "compute": c.get("compute", ""),
            # Bounds will be recomputed from locked example set — these are reference only
            "ref_low": c.get("low"),
            "ref_high": c.get("high"),
        })

    # Stability metrics
    # Pairwise overlap: for each pair of runs, what fraction of conditions overlap?
    pairwise_overlaps = []
    for i in range(len(all_run_conditions)):
        names_i = set(c["name"] for c in all_run_conditions[i])
        for j in range(i + 1, len(all_run_conditions)):
            names_j = set(c["name"] for c in all_run_conditions[j])
            union = names_i | names_j
            inter = names_i & names_j
            if union:
                pairwise_overlaps.append(len(inter) / len(union))

    avg_overlap = sum(pairwise_overlaps) / len(pairwise_overlaps) if pairwise_overlaps else 0

    # Frequency distribution
    freq_dist = Counter(condition_freq.values())

    return {
        "n_runs": n_runs,
        "threshold": threshold,
        "min_appearances": min_appearances,
        "max_conditions_cap": max_conditions,
        "n_unique_conditions": n_unique,
        "n_above_threshold": len(consensus_names),
        "n_after_cap": len(capped_names),
        "consensus_conditions": consensus_conditions,
        "stability_metrics": {
            "avg_pairwise_overlap": round(avg_overlap, 4),
            "avg_conditions_per_run": round(
                sum(len(rc) for rc in all_run_conditions) / max(n_runs, 1), 1),
            "frequency_distribution": {
                str(k): v for k, v in sorted(freq_dist.items(), reverse=True)
            },
        },
        "all_conditions_ranked": [
            {
                "name": name,
                "frequency": condition_freq[name],
                "frequency_pct": round(condition_freq[name] / n_runs, 3),
                "above_threshold": condition_freq[name] >= min_appearances,
            }
            for name in all_names
        ],
    }


# ══════════════════════════════════════════════════════════════
# EXAMPLE COUNT (for EPV cap)
# ══════════════════════════════════════════════════════════════

def get_example_count(setup_type):
    """Get example count from local SQLite."""
    if not os.path.exists(DB_PATH):
        print(f"  WARNING: No DB at {DB_PATH}")
        return 0
    try:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM examples WHERE setup_type=?", (setup_type,)
        ).fetchone()[0]
        conn.close()
        return n
    except Exception as e:
        print(f"  WARNING: DB error: {e}")
        return 0


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════

def run(setup_type, stage, threshold=0.7, n_latest=None, epv_divisor=3,
        signal_cap=35, refinement_cap=50):
    """Run consensus engine.

    Args:
        setup_type: e.g. "dtss"
        stage: "signal" or "refinement"
        threshold: consensus frequency threshold (0.0-1.0)
        n_latest: only use the N most recent runs (None = use all)
        epv_divisor: EPV denominator (default 3)
        signal_cap: hard ceiling for signal conditions (default 35)
        refinement_cap: hard ceiling for refinement conditions (default 50)
    """
    print("\n" + "=" * 70)
    print("  CONSENSUS ENGINE")
    print("=" * 70)
    print(f"  Setup: {setup_type.upper()}")
    print(f"  Stage: {stage}")
    print(f"  Threshold: {threshold} ({threshold:.0%})")

    # Find grind files
    if stage == "signal":
        run_files = find_signal_grind_files(setup_type, limit=n_latest)
        extract_fn = extract_signal_conditions
        hard_cap = signal_cap
    elif stage == "refinement":
        run_files = find_refinement_grind_files(setup_type, limit=n_latest)
        extract_fn = extract_refinement_conditions
        hard_cap = refinement_cap
    else:
        print(f"  ERROR: Unknown stage '{stage}'. Use 'signal' or 'refinement'.")
        return None

    n_runs = len(run_files)
    print(f"\n  Found {n_runs} {stage} grind outputs")

    if n_runs < 3:
        print(f"  ERROR: Need at least 3 runs for consensus. Found {n_runs}.")
        print(f"  Run: python local_runner/pyramid_grinder.py --setup {setup_type} --runs 15")
        return None

    # List runs
    print(f"\n  Runs:")
    for i, rf in enumerate(run_files):
        s = rf["summary"]
        total = s.get("final_total", s.get("losing_clusters_eliminated", "?"))
        print(f"    {i+1:>3}. {rf['filename']:<60s} "
              f"{rf['n_conditions']:>3} conds  {total} signals")

    # EPV validation — cap should be enforced at grinder level via --depth
    if stage == "signal":
        n_examples = get_example_count(setup_type)
        epv_cap = n_examples // epv_divisor if n_examples > 0 else hard_cap
        recommended_depth = min(epv_cap, hard_cap)
        print(f"\n  Examples: {n_examples}")
        print(f"  EPV recommended depth: min({n_examples} ÷ {epv_divisor}, {hard_cap}) = {recommended_depth}")

        # Check if the runs were actually EPV-capped
        avg_conds = sum(rf["n_conditions"] for rf in run_files) / n_runs
        if avg_conds > recommended_depth * 1.5:
            print(f"\n  ⚠ WARNING: Runs averaged {avg_conds:.0f} conditions, but EPV says max {recommended_depth}.")
            print(f"    Re-run with: --depth {recommended_depth}")
            print(f"    Without EPV capping at the grinder level, consensus selects")
            print(f"    the most popular overfit conditions — not the most robust ones.")
        max_conditions = None  # no cap at consensus level — threshold is the filter
    else:
        n_examples = None
        recommended_depth = hard_cap
        max_conditions = None  # threshold only
        print(f"\n  Refinement ceiling: {hard_cap} (enforce via --depth at grinder level)")

    # Extract conditions from each run
    print(f"\n  Extracting conditions...")
    all_run_conditions = []
    for rf in run_files:
        try:
            conds = extract_fn(rf["path"])
            all_run_conditions.append(conds)
            print(f"    {rf['filename']}: {len(conds)} conditions")
        except Exception as e:
            print(f"    ERROR reading {rf['filename']}: {e}")
            all_run_conditions.append([])

    # Compute consensus — threshold only, no cap (EPV enforced at grinder level)
    result = compute_consensus(
        all_run_conditions, n_runs,
        threshold=threshold,
        max_conditions=None,
    )

    # Print results
    print(f"\n  ══ CONSENSUS RESULTS ══")
    print(f"  Runs analyzed: {n_runs}")
    print(f"  Unique conditions found: {result['n_unique_conditions']}")
    print(f"  Above {threshold:.0%} threshold ({result['min_appearances']}/{n_runs}): "
          f"{result['n_above_threshold']}")
    if stage == "signal":
        if result['n_above_threshold'] > recommended_depth:
            print(f"  ⚠ {result['n_above_threshold']} survived threshold but EPV recommends max {recommended_depth}")
            print(f"    This means grinder --depth was too high. Re-run with --depth {recommended_depth}")
        else:
            print(f"  ✓ {result['n_above_threshold']} conditions — within EPV limit of {recommended_depth}")

    sm = result["stability_metrics"]
    print(f"\n  Stability Metrics:")
    print(f"    Avg pairwise overlap (Jaccard): {sm['avg_pairwise_overlap']:.1%}")
    print(f"    Avg conditions per run: {sm['avg_conditions_per_run']:.0f}")

    if sm['avg_pairwise_overlap'] < 0.2:
        print(f"\n  ⚠ WARNING: Very low overlap — beam search is highly unstable.")
        print(f"    Conditions vary wildly between runs. Consider:")
        print(f"    - Adding more examples to tighten the search space")
        print(f"    - Lowering the consensus threshold")
        print(f"    - Checking that examples are consistent (same pattern)")
    elif sm['avg_pairwise_overlap'] < 0.4:
        print(f"\n  ⚠ Moderate overlap — some instability in condition selection.")
    else:
        print(f"\n  ✓ Good overlap — conditions are reasonably stable across runs.")

    # Frequency distribution
    print(f"\n  Frequency Distribution:")
    fd = result["stability_metrics"]["frequency_distribution"]
    for freq, count in sorted(fd.items(), key=lambda x: int(x[0]), reverse=True):
        bar = "█" * count
        marker = " ← threshold" if int(freq) == result["min_appearances"] else ""
        print(f"    {freq:>3}/{n_runs}: {count:>3} conditions  {bar}{marker}")

    # Top consensus conditions
    cc = result["consensus_conditions"]
    if cc:
        print(f"\n  Top Consensus Conditions ({len(cc)}):")
        print(f"    {'#':>3} {'Freq':>5} {'%':>5}  {'Name':<50s} {'Tier':<6}")
        print(f"    {'-'*3} {'-'*5} {'-'*5}  {'-'*50} {'-'*6}")
        for i, c in enumerate(cc):
            print(f"    {i+1:>3} {c['frequency']:>3}/{n_runs} "
                  f"{c['frequency_pct']:>4.0%}  {c['name']:<50s} {c.get('tier',''):<6}")
    else:
        print(f"\n  ✗ No conditions survived consensus. Pipeline cannot proceed.")
        print(f"    This means no condition appeared in {result['min_appearances']}/{n_runs} runs.")
        print(f"    Lower the threshold or add more examples.")

    # Save output
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    output = {
        "setup_type": setup_type,
        "stage": stage,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "n_runs": n_runs,
        "threshold": threshold,
        "n_examples": n_examples if stage == "signal" else None,
        "epv_divisor": epv_divisor if stage == "signal" else None,
        "recommended_grinder_depth": recommended_depth,
        "run_files": [rf["filename"] for rf in run_files],
        "consensus": result,
    }

    os.makedirs(CACHE_DIR, exist_ok=True)
    out_path = os.path.join(CACHE_DIR, f"consensus_{stage}_{setup_type}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved: {out_path}")

    # Also save a latest pointer
    latest_path = os.path.join(CACHE_DIR, f"consensus_{stage}_{setup_type}.json")
    import shutil
    shutil.copy2(out_path, latest_path)
    print(f"  Latest: {latest_path}")

    # Mirror to Railway
    try:
        sys.path.insert(0, LOCAL_DIR)
        from file_mirror import mirror_file
        mirror_file(out_path)
        print(f"  Mirrored to Railway")
    except Exception as e:
        print(f"  WARNING: Mirror failed: {e}")

    print(f"\n  {'='*50}")
    if cc:
        print(f"  ✓ CONSENSUS: {len(cc)} conditions locked")
        print(f"    Stability: {sm['avg_pairwise_overlap']:.1%} avg overlap")
        print(f"    Next: Use these conditions for {'scanning' if stage == 'signal' else 'filtering'}")
    else:
        print(f"  ✗ CONSENSUS FAILED: No conditions survived")
        print(f"    Vet more examples or lower threshold")
    print(f"  {'='*50}")

    return output


def list_runs(setup_type, stage):
    """List available grind outputs without running consensus."""
    print(f"\n  Available {stage} grind outputs for {setup_type.upper()}:")
    if stage == "signal":
        files = find_signal_grind_files(setup_type)
    else:
        files = find_refinement_grind_files(setup_type)

    if not files:
        print(f"  None found.")
        return

    for i, rf in enumerate(files):
        s = rf["summary"]
        total = s.get("final_total", s.get("losing_clusters_eliminated", "?"))
        ts = rf.get("timestamp", "")[:19]
        print(f"  {i+1:>3}. {rf['n_conditions']:>3} conds  "
              f"{total:>6} signals  {ts}  {rf['filename']}")
    print(f"\n  Total: {len(files)} runs")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Consensus Engine — Multi-Run Stability Selection")
    parser.add_argument("--setup", default="dtss", help="Setup type (default: dtss)")
    parser.add_argument("--stage", required=True, choices=["signal", "refinement"],
                        help="Which grind stage to analyze")
    parser.add_argument("--threshold", type=float, default=0.7,
                        help="Consensus frequency threshold 0.0-1.0 (default: 0.7)")
    parser.add_argument("--n-latest", type=int, default=None,
                        help="Only use the N most recent runs (default: all)")
    parser.add_argument("--epv-divisor", type=int, default=3,
                        help="EPV denominator for condition cap (default: 3)")
    parser.add_argument("--signal-cap", type=int, default=35,
                        help="Hard ceiling for signal conditions (default: 35)")
    parser.add_argument("--refinement-cap", type=int, default=50,
                        help="Hard ceiling for refinement conditions (default: 50)")
    parser.add_argument("--list-runs", action="store_true",
                        help="Just list available runs, don't compute consensus")
    args = parser.parse_args()

    if args.list_runs:
        list_runs(args.setup, args.stage)
    else:
        result = run(
            setup_type=args.setup,
            stage=args.stage,
            threshold=args.threshold,
            n_latest=args.n_latest,
            epv_divisor=args.epv_divisor,
            signal_cap=args.signal_cap,
            refinement_cap=args.refinement_cap,
        )
        if result is None:
            sys.exit(1)
