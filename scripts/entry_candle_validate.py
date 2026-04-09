"""
Entry Candle Scorer — Validation

Checks whether validated example signals rank higher than non-examples
in the scored output. If the scorer is working, examples should cluster
near the top because their forward windows contain actual entry candles.
"""

import os
import sys
import json
import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(REPO_ROOT, "local_runner", "cache")


def main():
    setup = "dtss"

    path = os.path.join(CACHE_DIR, f"entry_scores_{setup}.json")
    if not os.path.exists(path):
        print(f"  ERROR: No entry scores file found at {path}")
        return

    with open(path) as f:
        data = json.load(f)

    scored = data.get("scored_signals", [])
    print(f"  Total scored signals: {len(scored)}")

    # Split by is_example
    examples = [s for s in scored if s.get("is_example") == 1]
    non_examples = [s for s in scored if s.get("is_example") != 1]
    print(f"  Examples: {len(examples)}")
    print(f"  Non-examples: {len(non_examples)}")

    # Scored list is already sorted by combined_score descending — rank = position
    ex_ranks = []
    non_ex_ranks = []
    for rank, s in enumerate(scored, 1):
        if s.get("is_example") == 1:
            ex_ranks.append(rank)
        else:
            non_ex_ranks.append(rank)

    ex_scores = [s.get("entry_candle_score", 0) or 0 for s in examples]
    non_ex_scores = [s.get("entry_candle_score", 0) or 0 for s in non_examples]
    ex_combined = [s.get("combined_score", 0) or 0 for s in examples]
    non_ex_combined = [s.get("combined_score", 0) or 0 for s in non_examples]

    print(f"\n  Example signals (ranked by combined score):")
    print(f"    Avg rank:  {np.mean(ex_ranks):.1f} / {len(scored)}")
    print(f"    Avg entry candle score: {np.mean(ex_scores):.4f}")
    print(f"    Avg combined score: {np.mean(ex_combined):.4f}")
    print(f"    In top half (rank <= {len(scored)//2}): {sum(1 for r in ex_ranks if r <= len(scored)//2)}/{len(ex_ranks)}")
    print(f"    In top quarter (rank <= {len(scored)//4}): {sum(1 for r in ex_ranks if r <= len(scored)//4)}/{len(ex_ranks)}")

    print(f"\n  Non-example signals:")
    print(f"    Avg rank:  {np.mean(non_ex_ranks):.1f} / {len(scored)}")
    print(f"    Avg entry candle score: {np.mean(non_ex_scores):.4f}")
    print(f"    Avg combined score: {np.mean(non_ex_combined):.4f}")

    print(f"\n  Separation (entry candle score):")
    diff_ec = np.mean(ex_scores) - np.mean(non_ex_scores)
    print(f"    Example vs non-example: {np.mean(ex_scores):.4f} vs {np.mean(non_ex_scores):.4f} ({diff_ec:+.4f})")

    print(f"\n  Separation (combined score):")
    diff_comb = np.mean(ex_combined) - np.mean(non_ex_combined)
    print(f"    Example vs non-example: {np.mean(ex_combined):.4f} vs {np.mean(non_ex_combined):.4f} ({diff_comb:+.4f})")

    if diff_comb > 0:
        print(f"    ✓ Examples score higher on combined — ranking is working")
    else:
        print(f"    ✗ Non-examples score higher on combined — ranking is NOT working")


if __name__ == "__main__":
    main()
