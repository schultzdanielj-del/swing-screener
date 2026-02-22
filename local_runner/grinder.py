"""
THE GRINDER v3 — Spiderweb condition search.

Uses prebuilt universe matrix (daily) + fast example matrix (per-setup).
The expensive part (universe matrix) is built once daily by the agent.
Each grind just loads matrices and runs the spiderweb search.

Usage:
    python local_runner/grinder.py --setup dtss --level 3
"""

import os
import sys
import json
import time
import argparse
import warnings
from datetime import datetime, timezone

warnings.filterwarnings("ignore", category=DeprecationWarning)

LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(LOCAL_DIR)
CACHE_DIR = os.path.join(LOCAL_DIR, "cache")
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, LOCAL_DIR)

from matrix_builder import get_universe_matrix, get_example_matrix
from spiderweb import SpiderwebSearch

API_BASE = "https://web-production-e3025.up.railway.app"

GRIND_LEVELS = {
    1: {"name": "Quick scan",    "beam_width": 10,  "depth": 5,  "est_time": "~30s"},
    2: {"name": "Light grind",   "beam_width": 25,  "depth": 8,  "est_time": "~2 min"},
    3: {"name": "Medium grind",  "beam_width": 50,  "depth": 10, "est_time": "~10 min"},
    4: {"name": "Heavy grind",   "beam_width": 100, "depth": 12, "est_time": "~30 min"},
    5: {"name": "Overnight",     "beam_width": 250, "depth": 15, "est_time": "~2-8 hours"},
}


def main():
    parser = argparse.ArgumentParser(description="THE GRINDER v3")
    parser.add_argument("--setup", default="dtss")
    parser.add_argument("--level", type=int, default=3, choices=[1, 2, 3, 4, 5])
    args = parser.parse_args()

    level = GRIND_LEVELS[args.level]

    print("\n" + "=" * 60)
    print("  THE GRINDER v3 — Spiderweb Search")
    print("=" * 60)
    print(f"  Setup: {args.setup.upper()} | Level {args.level}: {level['name']}")
    print(f"  Depth: {level['depth']} | Beam: {level['beam_width']}")

    t0 = time.time()

    # Load matrices
    print(f"\n  Loading universe matrix...")
    uni = get_universe_matrix()
    print(f"  Loading example matrix...")
    ex = get_example_matrix(args.setup)

    # Run search
    print(f"\n  Running spiderweb search...")
    search = SpiderwebSearch(
        example_values=ex["example_matrix"],
        universe_values=uni["universe_matrix"],
        expr_names=uni["expr_names"],
        expr_categories=uni["expr_categories"],
        universe_tickers=uni["universe_tickers"],
    )

    results = search.run(depth=level["depth"], beam_width=level["beam_width"])
    total = time.time() - t0

    # Print results
    if "error" in results:
        print(f"\n  ✗ {results['error']}")
        return

    print(f"\n{'='*60}")
    print(f"  BEST: {results['best_rate']:.2%} ({results['best_passing']} tickers)")
    print(f"  Conditions ({results['best_depth']}):")
    for t in results["best_thresholds"]:
        print(f"    [{t['category']:>18}] {t['expr']:35s} [{t['low']:.3f} — {t['high']:.3f}]")
    print(f"  Time: {total:.1f}s | Nodes: {results['stats']['nodes_explored']:,}")
    print(f"{'='*60}\n")

    # Save
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = {"setup_type": args.setup, "grind_level": args.level,
           "timestamp": datetime.now(timezone.utc).isoformat(),
           "total_time_s": round(total, 1), **results}
    with open(os.path.join(CACHE_DIR, f"grinder_results_{args.setup}.json"), "w") as f:
        json.dump(out, f, indent=2, default=str)


if __name__ == "__main__":
    main()
