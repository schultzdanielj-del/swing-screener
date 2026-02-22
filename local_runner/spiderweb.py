"""
Spiderweb Search — Branching combination explorer.

Instead of flat grid search, explores a tree of condition combinations:
- Each node = a set of conditions applied together
- Each branch = adding one more condition
- Score = % of universe that passes (lower = tighter)
- Constraint = ALL examples must always pass

The search is like a neural network's pathways:
- Multiple starting points (best individual expressions)
- Branches split when multiple conditions could be added next
- Dead branches pruned when examples get dropped
- Best paths kept, worst paths abandoned

Slider controls:
- depth: how many conditions deep to stack (2-15+)
- beam_width: how many parallel paths to explore (5-500+)
- Together these control grind time from seconds to hours

Usage:
    from local_runner.spiderweb import SpiderwebSearch
    
    search = SpiderwebSearch(example_matrix, universe_matrix, expr_names)
    results = search.run(depth=8, beam_width=50)
"""

import numpy as np
import time
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional
import json


@dataclass
class SearchNode:
    """A node in the search tree — one combination of conditions."""
    conditions: Tuple[int, ...]  # indices into expression list
    universe_mask: np.ndarray    # which universe tickers pass (bool array)
    pass_rate: float             # % of universe that passes
    depth: int

    def __hash__(self):
        return hash(self.conditions)

    def __eq__(self, other):
        return self.conditions == other.conditions


class SpiderwebSearch:
    """Branching combination search engine."""

    def __init__(self, example_values: np.ndarray, universe_values: np.ndarray,
                 expr_names: List[str], expr_categories: List[str] = None):
        """
        Args:
            example_values: shape (n_examples, n_expressions) — NaN for missing
            universe_values: shape (n_tickers, n_expressions) — NaN for missing
            expr_names: list of expression names
            expr_categories: optional list of categories per expression
        """
        self.n_examples, self.n_exprs = example_values.shape
        self.n_universe = universe_values.shape[0]
        self.expr_names = expr_names
        self.expr_categories = expr_categories or ["unknown"] * self.n_exprs

        # Precompute thresholds: for each expression, find the range where
        # ALL examples pass. Use min/max of example values.
        # Then create boolean masks for universe.
        self.expr_thresholds = []  # (low, high) per expression
        self.universe_passes = []  # bool array per expression (which universe tickers pass)
        self.valid_exprs = []      # indices of expressions that have valid thresholds

        self._precompute_thresholds(example_values, universe_values)

    def _precompute_thresholds(self, example_values, universe_values):
        """For each expression, find threshold range from examples and apply to universe."""
        t0 = time.time()

        for i in range(self.n_exprs):
            ex_vals = example_values[:, i]
            uni_vals = universe_values[:, i]

            # Skip if too many NaN in examples
            ex_valid = ex_vals[~np.isnan(ex_vals)]
            if len(ex_valid) < max(3, self.n_examples * 0.7):
                self.expr_thresholds.append((np.nan, np.nan))
                self.universe_passes.append(np.ones(self.n_universe, dtype=bool))
                continue

            # Threshold = range of example values (with small margin)
            ex_min = np.nanmin(ex_vals)
            ex_max = np.nanmax(ex_vals)
            margin = (ex_max - ex_min) * 0.05  # 5% margin
            low = ex_min - margin
            high = ex_max + margin

            self.expr_thresholds.append((low, high))

            # Which universe tickers fall within this range?
            # NaN values count as "pass" (no data = can't filter)
            passes = ((uni_vals >= low) & (uni_vals <= high)) | np.isnan(uni_vals)
            self.universe_passes.append(passes)

            # Is this expression useful? (filters at least 5% of universe)
            pass_rate = np.sum(passes) / self.n_universe
            if pass_rate < 0.95:
                self.valid_exprs.append(i)

        self.universe_passes = np.array(self.universe_passes)  # (n_exprs, n_universe)

        elapsed = time.time() - t0
        print(f"  Precomputed thresholds: {len(self.valid_exprs)} useful expressions "
              f"out of {self.n_exprs} ({elapsed:.2f}s)")

    def _get_pass_rate(self, condition_indices: Tuple[int, ...]) -> Tuple[float, np.ndarray]:
        """Get pass rate for a combination of conditions."""
        # AND all the condition masks together
        combined = np.ones(self.n_universe, dtype=bool)
        for idx in condition_indices:
            combined &= self.universe_passes[idx]
        pass_rate = np.sum(combined) / self.n_universe
        return pass_rate, combined

    def _score_individual(self) -> List[Tuple[int, float]]:
        """Score all individual expressions, return sorted by selectivity."""
        scores = []
        for i in self.valid_exprs:
            pass_rate = np.sum(self.universe_passes[i]) / self.n_universe
            scores.append((i, pass_rate))
        scores.sort(key=lambda x: x[1])  # lowest pass rate = most selective
        return scores

    def run(self, depth: int = 8, beam_width: int = 50,
            time_limit_s: float = None, progress_callback=None) -> dict:
        """
        Run the spiderweb search.

        Args:
            depth: max number of conditions to stack
            beam_width: how many parallel paths to explore at each level
            time_limit_s: optional time limit in seconds
            progress_callback: optional fn(level, best_rate, nodes_explored, elapsed)

        Returns dict with:
            - best_path: list of expression names in the best combo
            - best_rate: pass rate of the best combo
            - levels: results at each depth level
            - stats: timing and search stats
        """
        t0 = time.time()
        nodes_explored = 0

        # Score individuals first
        individual_scores = self._score_individual()
        if not individual_scores:
            return {"error": "No useful expressions found", "levels": []}

        print(f"\n  Starting spiderweb search: depth={depth}, beam_width={beam_width}")
        print(f"  Valid expressions to search: {len(self.valid_exprs)}")

        # Level 0: seed with the best individual expressions
        n_seeds = min(beam_width * 2, len(individual_scores))
        current_level: List[SearchNode] = []

        for idx, pass_rate in individual_scores[:n_seeds]:
            mask = self.universe_passes[idx].copy()
            node = SearchNode(
                conditions=(idx,),
                universe_mask=mask,
                pass_rate=pass_rate,
                depth=1,
            )
            current_level.append(node)
            nodes_explored += 1

        # Dedupe and keep best beam_width
        current_level.sort(key=lambda n: n.pass_rate)
        current_level = current_level[:beam_width]

        # Track results at each level
        levels = []
        best_overall = current_level[0] if current_level else None

        # Record level 1
        levels.append(self._summarize_level(1, current_level, time.time() - t0))
        self._print_level(1, current_level, nodes_explored, time.time() - t0)

        if progress_callback:
            progress_callback(1, current_level[0].pass_rate, nodes_explored, time.time() - t0)

        # Explore deeper levels
        for level in range(2, depth + 1):
            if time_limit_s and (time.time() - t0) > time_limit_s:
                print(f"\n  ⏱ Time limit reached at level {level-1}")
                break

            next_level: List[SearchNode] = []
            seen_combos = set()

            for node in current_level:
                # Try adding each valid expression not already in this combo
                for expr_idx in self.valid_exprs:
                    if expr_idx in node.conditions:
                        continue

                    # Create new combo (sorted for dedup)
                    new_conditions = tuple(sorted(node.conditions + (expr_idx,)))
                    if new_conditions in seen_combos:
                        continue
                    seen_combos.add(new_conditions)

                    # Compute new mask (AND with existing)
                    new_mask = node.universe_mask & self.universe_passes[expr_idx]
                    new_rate = np.sum(new_mask) / self.n_universe
                    nodes_explored += 1

                    # Only keep if it actually improves
                    if new_rate < node.pass_rate:
                        new_node = SearchNode(
                            conditions=new_conditions,
                            universe_mask=new_mask,
                            pass_rate=new_rate,
                            depth=level,
                        )
                        next_level.append(new_node)

                    # Time check within inner loop
                    if time_limit_s and nodes_explored % 10000 == 0:
                        if (time.time() - t0) > time_limit_s:
                            break
                if time_limit_s and (time.time() - t0) > time_limit_s:
                    break

            if not next_level:
                print(f"\n  ▓ Ceiling hit at level {level} — no combo improves further")
                break

            # Keep best beam_width nodes
            next_level.sort(key=lambda n: n.pass_rate)
            current_level = next_level[:beam_width]

            # Track best
            if current_level[0].pass_rate < best_overall.pass_rate:
                best_overall = current_level[0]

            # Record level
            levels.append(self._summarize_level(level, current_level, time.time() - t0))
            self._print_level(level, current_level, nodes_explored, time.time() - t0)

            if progress_callback:
                progress_callback(level, current_level[0].pass_rate,
                                  nodes_explored, time.time() - t0)

            # Check if we've hit the floor (< 0.5% or < 20 tickers)
            passing_count = np.sum(current_level[0].universe_mask)
            if passing_count < 20:
                print(f"\n  ▓ Floor hit at level {level} — only {passing_count} tickers pass")
                break

        elapsed = time.time() - t0

        # Build final result
        best_path = [self.expr_names[i] for i in best_overall.conditions]
        best_categories = [self.expr_categories[i] for i in best_overall.conditions]
        best_thresholds = [
            {"expr": self.expr_names[i],
             "category": self.expr_categories[i],
             "low": float(self.expr_thresholds[i][0]),
             "high": float(self.expr_thresholds[i][1])}
            for i in best_overall.conditions
        ]

        return {
            "best_path": best_path,
            "best_categories": best_categories,
            "best_rate": float(best_overall.pass_rate),
            "best_passing": int(np.sum(best_overall.universe_mask)),
            "best_thresholds": best_thresholds,
            "best_depth": best_overall.depth,
            "levels": levels,
            "stats": {
                "total_time_s": round(elapsed, 1),
                "nodes_explored": nodes_explored,
                "nodes_per_second": round(nodes_explored / elapsed) if elapsed > 0 else 0,
                "depth_reached": len(levels),
                "max_depth": depth,
                "beam_width": beam_width,
                "valid_expressions": len(self.valid_exprs),
                "total_expressions": self.n_exprs,
                "n_examples": self.n_examples,
                "n_universe": self.n_universe,
            }
        }

    def _summarize_level(self, level, nodes, elapsed):
        """Create summary for one depth level."""
        best = nodes[0]
        return {
            "level": level,
            "best_rate": round(best.pass_rate, 6),
            "best_passing": int(np.sum(best.universe_mask)),
            "best_conditions": [self.expr_names[i] for i in best.conditions],
            "paths_explored": len(nodes),
            "elapsed_s": round(elapsed, 1),
        }

    def _print_level(self, level, nodes, nodes_explored, elapsed):
        """Print progress for one level."""
        best = nodes[0]
        worst = nodes[-1]
        passing = int(np.sum(best.universe_mask))
        conditions_str = " + ".join(self.expr_names[i][:25] for i in best.conditions[-3:])
        if level > 3:
            conditions_str = f"...{len(best.conditions)} conditions... + " + conditions_str

        print(f"  Level {level:2d}: {best.pass_rate:6.2%} pass ({passing:,} tickers) | "
              f"{len(nodes)} paths | {nodes_explored:,} nodes | {elapsed:.1f}s | "
              f"{conditions_str}")
