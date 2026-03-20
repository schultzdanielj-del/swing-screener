"""2-stage trim search for profit_grinder.py — optimized loop order.

Extracts each expression column ONCE and tests against all final exits.
12,878 extractions instead of 50 x 12,878 = 643,900.

Imported by profit_grinder.py. Not run standalone.
"""
import time, os, sys
import numpy as np

TRIM_PCTS = [0.33, 0.50, 0.67]


def grind_2stage(stage1_results, fwd_expr_list, entry_high_bar_expr_list,
                entry_high_offset_v, valid_indices, close_2d,
                entry_prices_v, adr_values_v, weights_v, move_adrs_v,
                n_bars_per_signal, filtered_names, direction, exit_horizon,
                top_n_final, n_thresholds, loss_assumption_adr,
                extract_col_fn, extract_eb_fn, stats_fn, check_ram_fn, print_ram_fn):
    """2-stage trim search — outer loop = expressions, inner = final exits."""
    nv = len(valid_indices)
    ne = len(filtered_names)
    n_final = min(top_n_final, len(stage1_results))
    if n_final == 0:
        print("\n  -- 2-STAGE: No 1-stage results --")
        return []

    print(f"\n  -- 2-STAGE TRIM SEARCH (optimized) --")
    print(f"  {ne} trim exprs x {n_final} final exits x ~{n_thresholds} thresholds x 2 dirs x {len(TRIM_PCTS)} trim%")
    print(f"  Trim%: {[f'{p:.0%}' for p in TRIM_PCTS]}  (optional)")
    print_ram_fn("(before 2-stage)")
    t0 = time.time()

    bi = np.arange(exit_horizon)[np.newaxis, :]

    # -- Pre-compute all final exit profiles --
    print(f"  Pre-computing {n_final} final exit profiles...")
    final_data = []
    for fi in range(n_final):
        final = stage1_results[fi]
        fn_name = final["expr_name"]
        fn_dir = final["direction"]
        fn_thresh = final["threshold"]

        fei = None
        for j, name in enumerate(filtered_names):
            if name == fn_name:
                fei = j
                break
        if fei is None:
            continue

        fcol = extract_col_fn(fwd_expr_list, valid_indices, fei, exit_horizon)
        fsearch = np.zeros((nv, exit_horizon), dtype=bool)
        for vi in range(nv):
            s = entry_high_offset_v[vi] + 1
            if s < n_bars_per_signal[vi]:
                fsearch[vi, s:n_bars_per_signal[vi]] = True
        fm = np.isfinite(fcol) & fsearch
        if fn_dir == "above":
            fhit = (fcol >= fn_thresh) & fm
        else:
            fhit = (fcol <= fn_thresh) & fm
        fhb = np.where(fhit, bi, exit_horizon + 1)
        fexit_bars = np.min(fhb, axis=1)

        fcap = np.full(nv, -loss_assumption_adr, dtype=np.float64)
        ftrig = fexit_bars < exit_horizon + 1
        for vi in np.where(ftrig)[0]:
            fb = fexit_bars[vi]
            ec = close_2d[vi, fb]
            if np.isfinite(ec) and adr_values_v[vi] > 0:
                if direction == "short":
                    fcap[vi] = (entry_prices_v[vi] - ec) / adr_values_v[vi]
                else:
                    fcap[vi] = (ec - entry_prices_v[vi]) / adr_values_v[vi]

        pre_exit = np.zeros((nv, exit_horizon), dtype=bool)
        n_room = 0
        for vi in range(nv):
            eh = entry_high_offset_v[vi]
            feb = fexit_bars[vi]
            if feb < exit_horizon + 1 and eh + 2 <= feb:
                pre_exit[vi, eh + 1:feb] = True
                n_room += 1

        bh = np.full(nv, exit_horizon, dtype=np.int32)
        for vi in np.where(ftrig)[0]:
            bh[vi] = fexit_bars[vi] - entry_high_offset_v[vi]

        if n_room >= 20:
            final_data.append({
                "name": fn_name, "direction": fn_dir, "threshold": fn_thresh,
                "exit_bars": fexit_bars, "capture": fcap, "triggered": ftrig,
                "pre_exit_mask": pre_exit, "n_with_room": n_room,
                "bars_held": bh, "expectancy": final["expectancy"],
            })

    n_active = len(final_data)
    print(f"  Active final exits (>=20 signals with room): {n_active}/{n_final}")
    if n_active == 0:
        print("  No final exits have room for trim.")
        return []

    # -- Main grind: outer = expressions, inner = final exits --
    all_combos = []
    total_tested = 0

    for ei in range(ne):
        if (ei + 1) % 1000 == 0:
            el = time.time() - t0
            r = (ei + 1) / el if el > 0 else 0
            print(f"    [{ei+1}/{ne}] {r:.0f} expr/s, {len(all_combos):,} combos, {total_tested:,} tested")
        if (ei + 1) % 2000 == 0:
            check_ram_fn(f"(2stg expr {ei+1})", min_gb=1.0)

        col = extract_col_fn(fwd_expr_list, valid_indices, ei, exit_horizon)
        eb_vals = extract_eb_fn(entry_high_bar_expr_list, valid_indices, ei)
        eb_finite = np.isfinite(eb_vals)
        trim_name = filtered_names[ei]

        for fd in final_data:
            fm_trim = np.isfinite(col) & fd["pre_exit_mask"]
            fv = col[fm_trim]
            if len(fv) < 20:
                continue
            ths = np.unique(np.percentile(fv, np.linspace(5, 95, n_thresholds)))
            if len(ths) < 2:
                continue

            for th in ths:
                for dl, above in [("above", True), ("below", False)]:
                    total_tested += 1
                    hit = ((col >= th) if above else (col <= th)) & fm_trim
                    hb = np.where(hit, bi, exit_horizon + 1)
                    trim_bars = np.min(hb, axis=1)
                    trim_triggered = trim_bars < exit_horizon + 1

                    ate = eb_finite & ((eb_vals >= th) if above else (eb_vals <= th))
                    trim_triggered = trim_triggered & (~ate)

                    n_trimmed = int(trim_triggered.sum())
                    if n_trimmed < 5:
                        continue

                    for trim_pct in TRIM_PCTS:
                        blended = fd["capture"].copy()
                        for vi in np.where(trim_triggered)[0]:
                            tb = trim_bars[vi]
                            tc = close_2d[vi, tb]
                            if np.isfinite(tc) and adr_values_v[vi] > 0:
                                if direction == "short":
                                    tcap = (entry_prices_v[vi] - tc) / adr_values_v[vi]
                                else:
                                    tcap = (tc - entry_prices_v[vi]) / adr_values_v[vi]
                                blended[vi] = trim_pct * tcap + (1 - trim_pct) * fd["capture"][vi]

                        st = stats_fn(blended, weights_v, fd["triggered"], move_adrs_v, fd["bars_held"])
                        if st is None:
                            continue

                        st["mode"] = "2-stage"
                        st["trim_expr"] = trim_name
                        st["trim_direction"] = dl
                        st["trim_threshold"] = round(float(th), 6)
                        st["trim_pct"] = trim_pct
                        st["final_expr"] = fd["name"]
                        st["final_direction"] = fd["direction"]
                        st["final_threshold"] = fd["threshold"]
                        st["n_trimmed"] = n_trimmed
                        st["trim_rate"] = round(n_trimmed / nv, 4)
                        st["final_exit_expectancy"] = fd["expectancy"]
                        all_combos.append(st)

    el = time.time() - t0
    print(f"\n  2-stage: {el:.1f}s ({el/60:.1f} min), {total_tested:,} tested, {len(all_combos):,} raw")
    print_ram_fn("(after 2-stage)")

    all_combos.sort(key=lambda c: c.get("expectancy", float('-inf')), reverse=True)
    seen = set()
    deduped = []
    for c in all_combos:
        key = (c["trim_expr"], c["final_expr"], c["trim_pct"])
        if key not in seen:
            seen.add(key)
            deduped.append(c)
    print(f"  After dedup: {len(deduped):,}")

    improved = [c for c in deduped if c["expectancy"] > c["final_exit_expectancy"]]
    print(f"  Combos beating 1-stage: {len(improved)}")

    if improved:
        print(f"\n  Top 10 2-stage (beating 1-stage):")
        print(f"    {'#':<3} {'Trim Expr':<30} {'Dir':<6} {'Trim%':>5} "
              f"{'Final Expr':<30} {'Exp':>6} {'1stg':>6} {'D':>5} {'TrR':>5}")
        print(f"    {'-'*110}")
        for i, c in enumerate(improved[:10]):
            d = c["expectancy"] - c["final_exit_expectancy"]
            print(f"    {i+1:<3} {c['trim_expr']:<30} {c['trim_direction']:<6} "
                  f"{c['trim_pct']:>5.0%} {c['final_expr']:<30} "
                  f"{c['expectancy']:>6.3f} {c['final_exit_expectancy']:>6.3f} "
                  f"{d:>+5.3f} {c['trim_rate']:>5.1%}")
    else:
        print(f"\n  No 2-stage combo beats 1-stage.")

    return deduped
