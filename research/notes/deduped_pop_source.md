# Deduped pop materialization — where the full population lives

## Verified fact

The latest pyramid JSON per setup (`pyramid_{setup}_mp_sig*.json`) does **not** serialize cluster classifications or even the deduped signal list. Top-level keys:

```
['all_conditions', 'example_signals', 'examples_failing', 'examples_passing',
 'multi_pass', 'n_conditions', 'params', 'pass_summaries', 'peak_target',
 'refinement', 'setup_type', 'summary', 'tier_results', 'timestamp', 'total_time_s']
```

No `clusters`, no `raw_signal_clusters`, no per-cluster `classification` field. The race logic in `pyramid_grinder.py` (fade @ line 3491, breakout @ line 3636) builds clusters in-memory, classifies them, and throws the result away after updating summary stats.

The raw signal list *is* serialized — inside `tier_results[last_tier].final_signals` — as a list of `{ticker, date}` dicts, sorted.

## Option A — add a pyramid_grinder output

Modify `_gather_raw_signal_clusters()` in `local_runner/pyramid_grinder.py` to also emit the full cluster list (including leftward bars, example match info, and per-cluster classification under whichever race is in force) to a new file, e.g. `deduped_signals_{setup}_{timestamp}.json`:

```
{
  setup_type, timestamp, direction, setup_class,
  clusters: [
    {ticker, rightmost_bar_idx, rightmost_date, leftward: [...],
     is_example, example_entry_date, example_entry_idx,
     classification, classification_reason,
     stop_level, exit_bar}
  ]
}
```

Pros: one file read gets the full pop with the production race's opinion attached. Downstream consumers (classifier trainer, grinder rescorer) don't re-race.

Cons: touches production pyramid_grinder. Adds disk IO (~1–5 MB per setup). Couples downstream consumers to whatever race logic pyramid_grinder happens to use today — which is the thing we're trying to change.

## Option B — standalone extractor

Do nothing to pyramid_grinder. Read `tier_results.final_signals` from the pyramid JSON, apply consecutive-bar dedupe per ticker (rightmost wins), race with whatever kit the caller wants. This is exactly what `research/08_race_v1_examples.py` through `research/14_exit_rule_check.py` already do in-session.

Pros: no production changes. Each caller picks its own race kit — matches the "exit is not locked" principle. No duplicate-of-truth problem.

Cons: every consumer re-races. For the full pop of ~400–1100 signals per setup this is fast (sub-second on one core for the race alone; the expensive part is loading the expr cache which is already amortized).

## Recommendation

**Option B.** Two reasons:

1. Race logic is under active redesign. Serializing the current race's classifications into the pyramid output would freeze a snapshot of a race that's about to change. Downstream consumers reading that file would see stale labels.

2. The deduped signal list is already fully derivable from `final_signals` + standard consecutive-bar dedupe. No information is lost by reading on demand.

If a future session needs a **frozen** labels artifact (e.g., a versioned snapshot of "here are WIN/LOSS/BE labels under the agreed race kit as of 2026-04-17"), write that as a separate file in `data/signal_filter_labels/` with the kit parameters stamped into the header. Don't make it a pyramid_grinder output; make it an independent artifact that downstream consumers explicitly reference by date/version.

## Verified by

`research/13_race_population.py` successfully reconstructed deduped clusters for all four active setups from `final_signals` alone:

| setup | raw signals | deduped clusters | example-matched | non-example raceable |
|---|---|---|---|---|
| htf | 545 | 493 | 23 | 466 |
| bf | 530 | 440 | 40 | 398 |
| base | 646 | 538 | 35 | 490 |
| dtss | 1369 | 1108 | 48 | 1048 |

The cluster-match counts match (within ±2) the "clusters_matched_to_example" numbers in `research/out/v1_test.json` from the classifier worktree's prior session, confirming the dedupe logic is equivalent.
