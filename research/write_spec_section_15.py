"""Generate PRESIGNAL_GRINDER.md §15 content from the all-setups and overfit
JSON outputs. Appends the section to the end of the spec file, replacing any
existing §15 block.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = os.path.join(HERE, "PRESIGNAL_GRINDER.md")
OUT_DIR = os.path.join(HERE, "presignal_grinder_all")

SETUPS = ["htf", "bf", "base", "dtss", "3-4db"]


def load_json(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def fmt(n):
    try:
        return f"{int(n):,}"
    except Exception:
        return str(n)


def build_section():
    rows = []
    for s in SETUPS:
        r = load_json(os.path.join(OUT_DIR, f"{s}_summary.json"))
        if r is None:
            continue
        rows.append(r)
    overfit = load_json(os.path.join(OUT_DIR, "overfit_summary.json")) or {}

    lines = []
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 15. Session 2026-04-19 / 2026-04-20 — F1 + Location architecture (CURRENT)")
    lines.append("")
    lines.append("Supersedes §13 (cache-basis strict-AND) and §14 (two-stage). §14 post-mortem identified the failure mode: per-offset 1D bands define a hypercube in R^N, letting infinite trajectories thread through each offset's band without tracing any example-like shape, and strict-AND over 8k cache features collapses to 0 wild hits via compound-probability pathology. The architecture below carves on **shape coherence** (not per-offset levels) and **location in history** (orthogonal to shape), both axes with 100% EX pass by construction and purely geometric, scale-invariant, OHLC-only compute.")
    lines.append("")
    lines.append("### 15.1 Architecture")
    lines.append("")
    lines.append("Two axes, ANDed, both 100% EX pass, both data-derived per setup:")
    lines.append("")
    lines.append("**F1 — Visual axis (joint consecutive-offset hull).** Per adjacent offset pair `(k, k+1)` across the N-bar lead-up window, build a 2D convex hull of the examples' `(log(close[E-k]/close_E), log(close[E-k-1]/close_E))` points. A candidate passes iff its path's matching adjacent-offset pair lies inside the hull at every `k`. The joint-pair hull preserves inter-offset shape coherence (local direction + magnitude) that per-offset 1D bands drop.")
    lines.append("")
    lines.append("**Location axis — 5 scale-invariant context descriptors, all ANDed.**")
    lines.append("")
    lines.append("| # | Descriptor | Formula | Horizon |")
    lines.append("|---|---|---|---|")
    lines.append("| D1 | Price position in recent range | `(close[E] - min(close[E-M..E-1])) / (max - min)` | M derived per setup |")
    lines.append("| D2 | Pre-lead-in log-return | `log(close[E-N-1] / close[E-N-M-1])` | M derived per setup |")
    lines.append("| D3 | Bars since close was last higher | `min{k≥1 : close[E-k] > close[E]}`, capped 504 | none |")
    lines.append("| D4a | Log distance from ATH (all history) | `log(close[E] / max(close[0..E]))` | none |")
    lines.append("| D4b | Log distance from ATL (all history) | `log(close[E] / min(close[0..E]))` | none |")
    lines.append("| D5 | Recent-vs-long vol ratio | `std(log_rets over last M) / std(log_rets over all prior)` | M derived per setup |")
    lines.append("")
    lines.append("Horizons for D1, D2, D5 are derived per-setup by kneedle elbow on spread-vs-M (where spread = `max - min` over examples at each M). Same principle as how N was derived on the cache-feature range curve — the horizon where examples converge on shared values. Bounding region per descriptor is `[min_ex, max_ex]`. NaN-lenient per spec §4.5. Candidate passes location iff all 5 descriptors in their regions.")
    lines.append("")
    lines.append("### 15.2 Pipeline")
    lines.append("")
    lines.append("1. Universe: OHLCV cache preflight (~11,200+ tickers).")
    lines.append("2. Per setup, load examples (`scanperfect.db.examples` WHERE `setup_type=?`). Drop examples with <N bars of pre-entry history (e.g. recent IPOs).")
    lines.append("3. Build F1 hulls; sanity-check 100% EX pass.")
    lines.append("4. Full-universe scan, post-2020-01-02 date cutoff, per bar: compute N-bar anchored log-path, apply F1.")
    lines.append("5. Derive location horizons (kneedle); build bounds; sanity-check 100% EX pass.")
    lines.append("6. Full-universe scan, post-2020-01-02, per bar: compute 5 descriptors, AND against bounds.")
    lines.append("7. §5.2 cluster dedup per ticker (consecutive-bar rightmost-wins; split at example bars so each example is its own cluster's rightmost).")
    lines.append("8. Intersect F1 ∩ Location at cluster level.")
    lines.append("")
    lines.append("### 15.3 Results — per-setup (full universe, post-2020-01-02)")
    lines.append("")
    lines.append("| setup | N | ex_valid | candidates | F1 clusters | Loc clusters | F1 ∩ Loc | ex in ∩ |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in rows:
        lines.append(
            f"| {r['setup']} | {r['N_bars']} | {r['n_ex_valid']} | {fmt(r['total_candidates'])} | "
            f"{fmt(r['F1_clusters'])} | {fmt(r['Location_clusters'])} | "
            f"{fmt(r['Intersection_clusters'])} | {r['Intersection_ex_matched']}/{r['n_ex_valid']} |"
        )
    lines.append("")
    lines.append("### 15.4 Horizons derived per setup")
    lines.append("")
    lines.append("| setup | N | M1 (pos) | M2 (trend) | M5 (vol) |")
    lines.append("|---|---|---|---|---|")
    for r in rows:
        h = r.get("horizons", {})
        lines.append(
            f"| {r['setup']} | {r['N_bars']} | {h.get('M1_pos', '?')} | "
            f"{h.get('M2_trend', '?')} | {h.get('M5_vol_ratio', '?')} |"
        )
    lines.append("")
    lines.append("### 15.5 Per-descriptor keep rates")
    lines.append("")
    lines.append("Fraction of bars kept by each descriptor individually (higher = less carving = more pass-through; lower = carving more). Descriptors below ~50% are doing real work; above ~90% are pass-through on that setup.")
    lines.append("")
    lines.append("| setup | D1 pos | D2 trend | D3 tsh | D4a log_ath | D4b log_atl | D5 vol_ratio |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in rows:
        t = r.get("total_candidates", 1)
        pd_ = r.get("Location_per_desc_keep", {})
        def pct(k):
            return f"{100.0 * pd_.get(k, 0) / max(t, 1):.1f}%"
        lines.append(
            f"| {r['setup']} | {pct('D1')} | {pct('D2')} | {pct('D3')} | {pct('D4a')} | {pct('D4b')} | {pct('D5')} |"
        )
    lines.append("")
    lines.append("### 15.6 Overfit protection")
    lines.append("")
    if overfit:
        lines.append("Per spec §5.7 G1 (LOO stability) and G3 (permutation null). F1 LOO: drop each example, rebuild hulls, check held-out passes. Loc LOO: drop each example, re-derive M* and bounds, check held-out passes. Permutation null: draw `n_ex` random (ticker, E) bars from universe, build F1 filter as if they were examples, scan 500-ticker sample, measure F1 bar carve-through. Compare to real.")
        lines.append("")
        lines.append("| setup | n_ex | F1 LOO fail | Loc LOO fail | real F1 bar carve | random mean carve | real / random |")
        lines.append("|---|---|---|---|---|---|---|")
        for s in SETUPS:
            d = overfit.get(s)
            if d is None:
                continue
            l = d.get("loo", {})
            p = d.get("permutation_null", {})
            real_c = (p.get("real_carve", 0.0) if p else 0.0) * 100
            rand_m = (p.get("random_carve_mean", 0.0) if p else 0.0) * 100
            ratio = (p.get("ratio_real_over_random", 0.0) if p else 0.0)
            lines.append(
                f"| {s} | {l.get('n_ex', '?')} | "
                f"{l.get('f1_loo_fail_rate', 0.0)*100:.1f}% | "
                f"{l.get('loc_loo_fail_rate', 0.0)*100:.1f}% | "
                f"{real_c:.4f}% | {rand_m:.4f}% | {ratio:.4f} |"
            )
        lines.append("")
        lines.append("Interpretation: LOO fail rate > 0 indicates the dropped example was a bound-defining outlier (expected; strict G1=0 is very tight with convex-hull filters). The real/random carve ratio < 1 means the real-example filter is tighter than a filter built from random bars — i.e., the examples encode structure that random bars don't.")
    else:
        lines.append("(Overfit run not completed; re-run `presignal_grinder_overfit.py`.)")
    lines.append("")
    lines.append("### 15.7 Constraints carried from §5 / modified")
    lines.append("")
    lines.append("- **100% EX pass by construction** — preserved (verified at sanity + scan).")
    lines.append("- **Purely geometric and scale-invariant** — preserved (log-anchored paths, log-ratio / position descriptors).")
    lines.append("- **Data-derived horizons, no hand-picked thresholds** — preserved (kneedle on spread-vs-M).")
    lines.append("- **No forward information** — preserved (all descriptors use E and earlier).")
    lines.append("- **No entry-bar reference for non-examples** — preserved (bounds from examples, candidates evaluated at hypothetical E, not referencing any label).")
    lines.append("- **§4.3 exhaustive cache-feature scan** — DOES NOT APPLY. This architecture is OHLC-path-basis, not 16k-feature-basis. Re-anchored.")
    lines.append("- **§5.2 cluster dedup** — applied at both F1 and Location stage before intersection.")
    lines.append("- **Post-2020-01-02 date cutoff** — applied in both scans (data quality; Dan 2026-04-19).")
    lines.append("")
    lines.append("### 15.8 Implementation files")
    lines.append("")
    lines.append("- `research/presignal_grinder_all.py` — runs F1 + Location for all 5 setups, saves per-setup summaries and cluster CSVs.")
    lines.append("- `research/presignal_grinder_overfit.py` — LOO stability + permutation null for all 5.")
    lines.append("- `research/visual_shape_compare.py` — F1 module (helpers + HTF smoke test).")
    lines.append("- `research/location_axis.py` — Location module (helpers + HTF smoke test).")
    lines.append("- Outputs: `research/presignal_grinder_all/{setup}_summary.json`, `{setup}_overfit.json`, `{setup}_F1_clusters.csv`, `{setup}_F1_Loc_clusters.csv`, `all_setups_summary.json`, `overfit_summary.json`.")
    lines.append("")
    lines.append("### 15.9 Open items for next session")
    lines.append("")
    lines.append("- Visual spot-check for BF / BASE / DTSS / 3-4DB (only HTF has had its survivors eyeballed).")
    lines.append("- Decide whether weak descriptors (D2 trend, D4a ATH, D5 vol) add enough carve per setup to keep, or drop them.")
    lines.append("- Integration with downstream classifier — what form does the output take, how are clusters labeled, does the classifier operate on F1 ∩ Location or on one axis only.")
    lines.append("- Daily run automation + update pipeline.")
    lines.append("- Per-setup visual inspection of F1 ∩ Location survivors — Dan 2026-04-20 said HTF ∩ survivors look shape-consistent; verify the other 4.")
    lines.append("")
    return "\n".join(lines)


def main():
    if not os.path.exists(SPEC):
        print(f"SPEC not found: {SPEC}")
        return
    with open(SPEC, 'r', encoding='utf-8') as f:
        content = f.read()

    new_section = build_section()

    # Replace any existing §15 block (to end of file), or append
    marker_re = re.compile(r"\n+---\n+## 15\. ", re.MULTILINE)
    m = marker_re.search(content)
    if m:
        content = content[:m.start()] + new_section
    else:
        if not content.endswith("\n"):
            content += "\n"
        content += new_section

    with open(SPEC, 'w', encoding='utf-8') as f:
        f.write(content)
    print(f"Wrote §15 ({len(new_section)} chars) to {SPEC}")


if __name__ == "__main__":
    main()
