"""
cycle_health.py  —  V2 health check. Run after scan_signals.py completes.

Reads all data from Railway (cycle_signals, grind_cycles, examples).
Computes all cycle_health metrics per DATA_CONTRACT.md.
Uploads a single cycle_health row to Railway.
Prints a human-readable health report.

Usage:
    python scripts/cycle_health.py --setup dtss
    python scripts/cycle_health.py --setup dtss --cycle dtss_20260306_143022
"""

import argparse
import json
import sys
from datetime import datetime, timezone

import requests

RAILWAY_URL = "https://web-production-e3025.up.railway.app"


# ── Railway helpers ───────────────────────────────────────────────────────────

def _get(endpoint, timeout=30):
    r = requests.get(f"{RAILWAY_URL}{endpoint}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def _post(endpoint, payload, timeout=30):
    r = requests.post(f"{RAILWAY_URL}{endpoint}", json=payload, timeout=timeout)
    if not r.ok:
        print(f"  ERROR {r.status_code} posting to {endpoint}: {r.text[:300]}")
        r.raise_for_status()
    return r.json()


# ── Data loading ──────────────────────────────────────────────────────────────

def load_cycle(setup_type, cycle_id=None):
    """Load target cycle and all cycles for this setup (to find prev)."""
    data    = _get(f"/api/v2/cycles/{setup_type}")
    cycles  = data.get("cycles", [])
    if not cycles:
        print(f"  ERROR: No cycles found for {setup_type}")
        sys.exit(1)

    if cycle_id:
        target = next((c for c in cycles if c["cycle_id"] == cycle_id), None)
        if not target:
            print(f"  ERROR: cycle_id {cycle_id!r} not found")
            sys.exit(1)
    else:
        current = [c for c in cycles if c["is_current"] == 1]
        if not current:
            print(f"  ERROR: No is_current cycle for {setup_type}")
            sys.exit(1)
        target = current[0]

    return target, cycles


def load_signals(cycle_id):
    """Load all cycle_signals rows for this cycle."""
    data = _get(f"/api/v2/cycles/{cycle_id}/signals")
    return data.get("signals", [])


def load_examples(setup_type):
    """Load validated examples."""
    data = _get(f"/api/examples/{setup_type}")
    return data.get("examples", [])


# ── Metric helpers ────────────────────────────────────────────────────────────

def median(values):
    if not values:
        return None
    s = sorted(v for v in values if v is not None)
    if not s:
        return None
    return s[len(s) // 2]


def _active_trading_days(signals):
    """Count distinct calendar days that had at least one signal."""
    return len(set(s["signal_date"] for s in signals))


# ── Health computation ────────────────────────────────────────────────────────

def compute_health(target_cycle, all_cycles, signals, examples, setup_type):
    """
    Compute all cycle_health fields per DATA_CONTRACT.md.
    Returns a dict ready to POST.
    """
    cycle_id = target_cycle["cycle_id"]
    n_examples_at_grind = target_cycle.get("n_examples_at_grind") or 0

    # ── Previous cycle ────────────────────────────────────────────────────────
    # Cycles are returned newest-first. Find the most recent COMPLETE cycle
    # before this one for this setup_type.
    complete_cycles = [
        c for c in all_cycles
        if c["status"] == "complete" and c["cycle_id"] != cycle_id
    ]
    # all_cycles is newest-first, so first match is the immediately prior cycle
    prev_cycle = complete_cycles[0] if complete_cycles else None
    prev_cycle_id = prev_cycle["cycle_id"] if prev_cycle else None

    # ── Signal quality ────────────────────────────────────────────────────────
    n_signals = len(signals)

    if n_signals == 0:
        peak_per_day = 0.0
        avg_per_day  = 0.0
    else:
        from collections import Counter
        day_counts   = Counter(s["signal_date"] for s in signals)
        peak_per_day = float(max(day_counts.values()))
        active_days  = len(day_counts)
        avg_per_day  = round(n_signals / active_days, 2) if active_days else 0.0

    # Signal stability vs prev cycle
    signal_stability_pct = None
    if prev_cycle_id:
        prev_signals = load_signals(prev_cycle_id)
        prev_keys    = set(
            (s["ticker"], s["signal_date"]) for s in prev_signals
        )
        curr_keys    = set(
            (s["ticker"], s["signal_date"]) for s in signals
        )
        if curr_keys:
            overlap = len(curr_keys & prev_keys)
            signal_stability_pct = round(overlap / len(curr_keys) * 100, 1)
        else:
            signal_stability_pct = 0.0

    # ── Example coverage ──────────────────────────────────────────────────────
    # examples_passing: count of signals flagged is_example=1
    examples_passing = sum(1 for s in signals if s.get("is_example") == 1)

    # examples_added_this_cycle: compare examples at this grind vs previous grind
    prev_n_examples_at_grind = (
        prev_cycle.get("n_examples_at_grind") or 0 if prev_cycle else 0
    )
    examples_added_this_cycle = (
        (n_examples_at_grind - prev_n_examples_at_grind)
        if prev_cycle else 0
    )

    # examples_since_last_grind: current total examples minus what was in the grind
    current_total_examples  = len(examples)
    examples_since_last_grind = max(0, current_total_examples - n_examples_at_grind)

    # ── Classification quality ────────────────────────────────────────────────
    auto_signals   = [s for s in signals
                      if s.get("classification_source") in ("example", "exit_filter")]
    vetted_signals = [s for s in signals
                      if s.get("classification_source") in ("manual", "ai_approved")]

    n_auto  = len(auto_signals)
    n_total = len(signals)

    auto_wins  = [s for s in auto_signals if s.get("classification") == "AUTO_WIN"]
    win_rate_auto = round(len(auto_wins) / n_auto, 4) if n_auto else None

    if vetted_signals:
        vetted_wins    = [s for s in vetted_signals
                          if s.get("classification") in ("MANUAL_WIN", "AI_WIN")]
        win_rate_vetted = round(len(vetted_wins) / len(vetted_signals), 4)
    else:
        win_rate_vetted = None

    pct_manually_vetted = (
        round(len(vetted_signals) / n_total, 4) if n_total else 0.0
    )

    # ── EV estimate ───────────────────────────────────────────────────────────
    win_adrs  = [s["move_adr"] for s in auto_signals
                 if s.get("classification") == "AUTO_WIN"
                 and s.get("move_adr") is not None]
    loss_adrs = [s["move_adr"] for s in auto_signals
                 if s.get("classification") == "AUTO_LOSS"
                 and s.get("move_adr") is not None]

    median_winner_adr = median(win_adrs)
    median_loser_adr  = median(loss_adrs)

    if (win_rate_auto is not None
            and median_winner_adr is not None
            and median_loser_adr is not None):
        ev_estimate = round(
            win_rate_auto * median_winner_adr
            - (1 - win_rate_auto) * median_loser_adr,
            4,
        )
    else:
        ev_estimate = None

    # ── Cycle delta ───────────────────────────────────────────────────────────
    signal_count_delta    = None
    condition_count_delta = None
    win_rate_delta        = None

    if prev_cycle_id:
        prev_health_data = _get(f"/api/v2/health/{prev_cycle_id}")
        prev_health      = prev_health_data.get("health")

        if prev_health:
            prev_n = prev_health.get("n_signals")
            if prev_n is not None:
                signal_count_delta = n_signals - prev_n

            prev_wr = prev_health.get("win_rate_auto")
            if prev_wr is not None and win_rate_auto is not None:
                win_rate_delta = round(win_rate_auto - prev_wr, 4)

        # condition_count_delta: compare condition counts
        curr_n_conds = target_cycle.get("n_conditions", 0)
        prev_n_conds = prev_cycle.get("n_conditions", 0)
        if curr_n_conds and prev_n_conds:
            condition_count_delta = curr_n_conds - prev_n_conds
    else:
        prev_health = None

    # ── Promote recommendation ────────────────────────────────────────────────
    flag_reasons = []

    # Hard reject
    if examples_passing < n_examples_at_grind:
        promote_recommendation = "hard_reject"
        flag_reasons.append(
            f"examples_passing={examples_passing} < n_examples_at_grind={n_examples_at_grind}"
        )
    else:
        # Flag conditions
        if (signal_count_delta is not None and signal_count_delta > 0
                and win_rate_delta is not None and win_rate_delta < 0):
            flag_reasons.append(
                f"signal_count +{signal_count_delta} but win_rate {win_rate_delta:+.1%}"
            )
        if signal_stability_pct is not None and signal_stability_pct < 50:
            flag_reasons.append(
                f"signal_stability={signal_stability_pct:.0f}% < 50%"
            )
        promote_recommendation = "flag" if flag_reasons else "promote"

    flag_reason = "; ".join(flag_reasons) if flag_reasons else None

    # ── Live-readiness ────────────────────────────────────────────────────────
    blockers = []

    if signal_stability_pct is None or signal_stability_pct < 80:
        blockers.append("signal_stability_pct < 80")

    if not (2.0 <= avg_per_day <= 7.0):
        blockers.append(f"avg_per_day={avg_per_day:.2f} not in [2.0, 7.0]")

    if win_rate_auto is None or win_rate_auto < 0.40:
        blockers.append(f"win_rate_auto={win_rate_auto} < 0.40")

    if ev_estimate is None or ev_estimate <= 0:
        blockers.append(f"ev_estimate={ev_estimate} <= 0")

    if median_loser_adr is None or median_loser_adr >= 1.0:
        blockers.append(f"median_loser_adr={median_loser_adr} >= 1.0")

    # examples_added_this_cycle + prev cycle examples_added_this_cycle < 5
    prev_added = (
        prev_health.get("examples_added_this_cycle", 0)
        if prev_health else 0
    ) or 0
    two_cycle_adds = examples_added_this_cycle + prev_added
    if two_cycle_adds >= 5:
        blockers.append(
            f"examples_added_last_two_cycles={two_cycle_adds} >= 5"
        )

    live_ready          = 1 if not blockers else 0
    live_ready_blockers = json.dumps(blockers) if blockers else None

    computed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "cycle_id":                  cycle_id,
        "setup_type":                setup_type,
        "n_signals":                 n_signals,
        "peak_per_day":              peak_per_day,
        "avg_per_day":               avg_per_day,
        "signal_stability_pct":      signal_stability_pct,
        "examples_passing":          examples_passing,
        "examples_added_this_cycle": examples_added_this_cycle,
        "examples_since_last_grind": examples_since_last_grind,
        "win_rate_auto":             win_rate_auto,
        "win_rate_vetted":           win_rate_vetted,
        "pct_manually_vetted":       pct_manually_vetted,
        "median_winner_adr":         median_winner_adr,
        "median_loser_adr":          median_loser_adr,
        "ev_estimate":               ev_estimate,
        "prev_cycle_id":             prev_cycle_id,
        "signal_count_delta":        signal_count_delta,
        "condition_count_delta":     condition_count_delta,
        "win_rate_delta":            win_rate_delta,
        "promote_recommendation":    promote_recommendation,
        "flag_reason":               flag_reason,
        "live_ready":                live_ready,
        "live_ready_blockers":       live_ready_blockers,
        "computed_at":               computed_at,
    }


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(h):
    """Print human-readable health report."""
    RESET  = "\033[0m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    RED    = "\033[91m"

    rec = h["promote_recommendation"]
    rec_color = GREEN if rec == "promote" else (YELLOW if rec == "flag" else RED)

    lr_color = GREEN if h["live_ready"] else YELLOW

    print(f"\n{'='*62}")
    print(f"  CYCLE HEALTH  —  {h['setup_type'].upper()}  —  {h['cycle_id']}")
    print(f"{'='*62}")

    print(f"\n  Signal Quality")
    print(f"    n_signals      : {h['n_signals']:,}")
    print(f"    peak_per_day   : {h['peak_per_day']}")
    print(f"    avg_per_day    : {h['avg_per_day']}")
    stab = h['signal_stability_pct']
    stab_str = f"{stab:.1f}%" if stab is not None else "n/a (first cycle)"
    print(f"    stability      : {stab_str}")

    print(f"\n  Example Coverage")
    print(f"    passing        : {h['examples_passing']} / {h.get('n_examples_at_grind','?')}")
    print(f"    added this run : {h['examples_added_this_cycle']}")
    print(f"    since grind    : {h['examples_since_last_grind']}")

    print(f"\n  Classification")
    wr = h['win_rate_auto']
    print(f"    win_rate_auto  : {wr:.1%}" if wr is not None else "    win_rate_auto  : n/a")
    wrv = h['win_rate_vetted']
    print(f"    win_rate_vetted: {wrv:.1%}" if wrv is not None else "    win_rate_vetted: n/a")
    print(f"    pct_vetted     : {h['pct_manually_vetted']:.1%}")

    print(f"\n  EV Estimate")
    mw = h['median_winner_adr']
    ml = h['median_loser_adr']
    ev = h['ev_estimate']
    print(f"    median winner  : {mw:.2f} ADR" if mw is not None else "    median winner  : n/a")
    print(f"    median loser   : {ml:.2f} ADR" if ml is not None else "    median loser   : n/a")
    print(f"    ev_estimate    : {ev:.3f}" if ev is not None else "    ev_estimate    : n/a")

    if h['prev_cycle_id']:
        print(f"\n  Cycle Delta  (vs {h['prev_cycle_id']})")
        sd = h['signal_count_delta']
        cd = h['condition_count_delta']
        wd = h['win_rate_delta']
        print(f"    signals        : {sd:+d}" if sd is not None else "    signals        : n/a")
        print(f"    conditions     : {cd:+d}" if cd is not None else "    conditions     : n/a")
        print(f"    win_rate       : {wd:+.1%}" if wd is not None else "    win_rate       : n/a")

    print(f"\n  {'─'*58}")
    print(f"  Recommendation : {rec_color}{rec.upper()}{RESET}")
    if h['flag_reason']:
        print(f"  Reason         : {h['flag_reason']}")

    blockers = json.loads(h['live_ready_blockers']) if h['live_ready_blockers'] else []
    lr_label = "YES" if h['live_ready'] else "NO"
    print(f"  Live-ready     : {lr_color}{lr_label}{RESET}")
    if blockers:
        for b in blockers:
            print(f"    ✗ {b}")
    else:
        print(f"    ✓ All thresholds met")
    print(f"{'='*62}\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Compute and upload cycle health metrics."
    )
    parser.add_argument("--setup",  required=True, help="Setup type, e.g. dtss")
    parser.add_argument("--cycle",  default=None,
                        help="Specific cycle_id (default: current cycle)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report but do not upload to Railway")
    args = parser.parse_args()

    setup = args.setup.lower()

    print(f"\n{'='*62}")
    print(f"  CYCLE HEALTH  —  {setup.upper()}")
    print(f"{'='*62}\n")

    # Load
    print("  [railway] Loading cycle list...")
    target_cycle, all_cycles = load_cycle(setup, args.cycle)
    cycle_id = target_cycle["cycle_id"]
    print(f"  Target cycle: {cycle_id}")

    print("  [railway] Loading signals...")
    signals = load_signals(cycle_id)
    print(f"  Loaded {len(signals):,} signals")

    print("  [railway] Loading examples...")
    examples = load_examples(setup)
    print(f"  Loaded {len(examples)} examples")

    # Compute
    print("\n  Computing health metrics...")
    health = compute_health(target_cycle, all_cycles, signals, examples, setup)

    # Attach n_examples_at_grind for report readability
    health["n_examples_at_grind"] = target_cycle.get("n_examples_at_grind")

    # Print report
    print_report(health)

    # Upload
    if args.dry_run:
        print("  [dry-run] Skipping Railway upload.")
        return

    print("  Uploading health metrics to Railway...")
    resp = _post("/api/v2/health", health)
    print(f"  ✓ Uploaded: {resp.get('message', 'OK')}")

    # If promote_recommendation == 'promote', auto-activate
    if health["promote_recommendation"] == "promote":
        print(f"  Auto-promoting cycle {cycle_id} as current...")
        resp = _post(f"/api/v2/cycles/{cycle_id}/activate", {})
        print(f"  ✓ {resp.get('message', 'Activated')}")
    elif health["promote_recommendation"] == "flag":
        print(f"  ⚠  FLAGGED — manual confirmation required before activating.")
        print(f"     Reason: {health['flag_reason']}")
        print(f"     To activate anyway: POST /api/v2/cycles/{cycle_id}/activate")
    else:
        print(f"  ✗  HARD REJECT — cycle NOT activated.")
        print(f"     Reason: {health['flag_reason']}")


if __name__ == "__main__":
    main()
