"""
Spiderweb Search — Branching combination explorer.

Explores a tree of condition combinations:
- Each node = a set of conditions applied together (AND logic)
- Each branch = adding one more condition
- Score = % of universe that passes (lower = tighter = better)
- Constraint = ALL examples must always pass (thresholds from example range)

Slider controls beam_width (parallel paths) and depth (conditions stacked).
"""

import numpy as np
import time
from dataclasses import dataclass
from typing import List, Tuple, Optional


@dataclass
class SearchNode:
    conditions: Tuple[int, ...]
    universe_mask: np.ndarray
    pass_rate: float
    depth: int

    def __hash__(self):
        return hash(self.conditions)


class SpiderwebSearch:

    def __init__(self, example_values: np.ndarray, universe_values: np.ndarray,
                 expr_names: List[str], expr_categories: List[str] = None,
                 universe_tickers: List[str] = None):
        self.n_examples, self.n_exprs = example_values.shape
        self.n_universe = universe_values.shape[0]
        self.expr_names = expr_names
        self.expr_categories = expr_categories or ["unknown"] * self.n_exprs
        self.universe_tickers = universe_tickers or [f"T{i}" for i in range(self.n_universe)]

        self.expr_thresholds = []
        self.universe_passes = []
        self.valid_exprs = []
        self._precompute(example_values, universe_values)

    def _precompute(self, example_values, universe_values):
        t0 = time.time()
        masks = []

        for i in range(self.n_exprs):
            ex_vals = example_values[:, i]
            uni_vals = universe_values[:, i]

            ex_valid = ex_vals[~np.isnan(ex_vals)]
            # CRITICAL: require ALL examples to have valid values.
            # If any example is NaN, this expression cannot be a condition —
            # it would fail validation for that example. No exceptions.
            if len(ex_valid) < self.n_examples:
                self.expr_thresholds.append((np.nan, np.nan))
                masks.append(np.ones(self.n_universe, dtype=bool))
                continue

            ex_min = np.nanmin(ex_vals)
            ex_max = np.nanmax(ex_vals)
            margin = (ex_max - ex_min) * 0.05
            low = ex_min - margin
            high = ex_max + margin
            self.expr_thresholds.append((low, high))

            passes = ((uni_vals >= low) & (uni_vals <= high)) | np.isnan(uni_vals)
            masks.append(passes)

            pass_rate = np.sum(passes) / self.n_universe
            if pass_rate < 0.95:
                self.valid_exprs.append(i)

        self.universe_passes = np.array(masks)
        print(f"  Precomputed: {len(self.valid_exprs)} useful expressions "
              f"out of {self.n_exprs} ({time.time()-t0:.2f}s)")

    def run(self, depth=8, beam_width=50, progress_callback=None):
        t0 = time.time()
        nodes_explored = 0

        # Score individuals
        individual_scores = []
        for i in self.valid_exprs:
            pr = np.sum(self.universe_passes[i]) / self.n_universe
            individual_scores.append((i, pr))
        individual_scores.sort(key=lambda x: x[1])

        if not individual_scores:
            return {"error": "No useful expressions found", "levels": []}

        print(f"\n  Spiderweb: depth={depth}, beam={beam_width}, "
              f"{len(self.valid_exprs)} valid expressions")

        # Seed with best individuals
        n_seeds = min(beam_width * 2, len(individual_scores))
        current_level = []
        for idx, pr in individual_scores[:n_seeds]:
            current_level.append(SearchNode(
                conditions=(idx,),
                universe_mask=self.universe_passes[idx].copy(),
                pass_rate=pr, depth=1,
            ))
            nodes_explored += 1

        current_level.sort(key=lambda n: n.pass_rate)
        current_level = current_level[:beam_width]

        levels = [self._summarize(1, current_level, time.time() - t0)]
        best = current_level[0]
        self._print_level(1, current_level, nodes_explored, time.time() - t0)

        if progress_callback:
            progress_callback(1, current_level[0].pass_rate, nodes_explored, time.time() - t0)

        # Explore deeper
        # Pre-stack all valid expression masks into a 2D array for vectorized AND
        valid_masks = self.universe_passes[self.valid_exprs]  # shape: (n_valid, n_universe)
        valid_expr_arr = np.array(self.valid_exprs)
        n_valid = len(self.valid_exprs)
        valid_set = set(self.valid_exprs)
        # Map expr_idx -> position in valid_masks
        expr_to_pos = {idx: pos for pos, idx in enumerate(self.valid_exprs)}

        # Pre-cast valid masks to float32 for matmul pass-rate estimation
        valid_masks_f = valid_masks.astype(np.float32)
        valid_exprs_arr = np.array(self.valid_exprs)

        for lv in range(2, depth + 1):
            next_level = []
            seen = set()

            # --- Matmul batch: estimate pass rates for ALL beam×candidate pairs ---
            beam_masks_f = np.array([n.universe_mask for n in current_level], dtype=np.float32)
            # (beam, n_universe) @ (n_universe, n_valid) → (beam, n_valid)
            joint_counts = beam_masks_f @ valid_masks_f.T
            joint_rates = joint_counts / self.n_universe
            nodes_explored += joint_rates.size

            # Build node pass rate vector for broadcasting
            node_rates = np.array([n.pass_rate for n in current_level], dtype=np.float32)
            # improved[i,j] = True if candidate j reduces pass rate for node i
            improved = joint_rates < node_rates[:, np.newaxis]

            # Mask out expressions already in each node's condition set
            for node_idx, node in enumerate(current_level):
                for cond_idx in node.conditions:
                    if cond_idx in expr_to_pos:
                        improved[node_idx, expr_to_pos[cond_idx]] = False

            # Find all (node_idx, cand_pos) pairs that improve
            hit_nodes, hit_cands = np.where(improved)

            # Sort by estimated pass rate so best combos are processed first
            hit_rates = joint_rates[hit_nodes, hit_cands]
            sort_order = np.argsort(hit_rates)
            hit_nodes = hit_nodes[sort_order]
            hit_cands = hit_cands[sort_order]

            for h in range(len(hit_nodes)):
                node_idx = int(hit_nodes[h])
                cand_pos = int(hit_cands[h])
                node = current_level[node_idx]
                expr_orig_idx = int(valid_exprs_arr[cand_pos])

                combo = tuple(sorted(node.conditions + (expr_orig_idx,)))
                if combo in seen:
                    continue
                seen.add(combo)

                # Materialize exact bool mask only for survivors
                mask = valid_masks[cand_pos] & node.universe_mask
                exact_pr = np.sum(mask) / self.n_universe

                next_level.append(SearchNode(
                    conditions=combo, universe_mask=mask,
                    pass_rate=exact_pr, depth=lv,
                ))

                # Early exit: once we have enough candidates, skip the rest
                # (they're sorted by rate so the best are first)
                if len(next_level) >= beam_width * 4:
                    break

            if not next_level:
                print(f"\n  ▓ Ceiling at level {lv}")
                break

            next_level.sort(key=lambda n: n.pass_rate)
            current_level = next_level[:beam_width]

            if current_level[0].pass_rate < best.pass_rate:
                best = current_level[0]

            levels.append(self._summarize(lv, current_level, time.time() - t0))
            self._print_level(lv, current_level, nodes_explored, time.time() - t0)

            if progress_callback:
                progress_callback(lv, current_level[0].pass_rate,
                                  nodes_explored, time.time() - t0)

            if np.sum(current_level[0].universe_mask) == 0:
                print(f"\n  ▓ Zero universe tickers at level {lv} — stopping")
                break

        elapsed = time.time() - t0

        # Get passing tickers
        passing_indices = np.where(best.universe_mask)[0]
        passing_tickers = [self.universe_tickers[i] for i in passing_indices]

        return {
            "best_path": [self.expr_names[i] for i in best.conditions],
            "best_categories": [self.expr_categories[i] for i in best.conditions],
            "best_rate": float(best.pass_rate),
            "best_passing": int(np.sum(best.universe_mask)),
            "passing_tickers": passing_tickers,
            "best_thresholds": [
                {"expr": self.expr_names[i], "category": self.expr_categories[i],
                 "low": float(self.expr_thresholds[i][0]),
                 "high": float(self.expr_thresholds[i][1])}
                for i in best.conditions
            ],
            "best_depth": best.depth,
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

    def _summarize(self, level, nodes, elapsed):
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
        best = nodes[0]
        passing = int(np.sum(best.universe_mask))
        print(f"  Level {level:2d}: {best.pass_rate:6.2%} ({passing:,} tickers) | "
              f"{len(nodes)} paths | {nodes_explored:,} nodes | {elapsed:.1f}s")
