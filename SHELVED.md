# ScanPerfect — Shelved / Legacy Scripts

Scripts and docs that have been replaced or abandoned. Kept for reference only — not on the active call graph.

| Item | Location | Reason shelved |
|--------|---------|---------------|
| `dartboard_grinder.py` | `archive/shelved_scripts/` | Additive scoring washes out discrimination. |
| `hybrid_grinder.py` | `archive/shelved_scripts/` | Correlated booleans don't filter effectively. |
| `proximity_grinder.py` | `archive/shelved_scripts/` | Replaced by the refinement grinder in `pyramid_grinder.py --blackout`. |
| `setup_refiner.py` | `archive/shelved_scripts/` | Functionality folded into the refinement path of `pyramid_grinder.py`. Any surviving comments in `pyramid_grinder.py` referencing `setup_refiner` are dead — leave the comments for future cleanup but do not re-import. |
| `signal_filter.py` classified output | (path in signal_filter.py, kept) | Replaced by `raw_signal_clusters_{setup}.json` as the canonical classified output. The classifier function still runs but its standalone output path is superseded. |
| `market_grinder.py` | `archive/shelved_scripts/` | Replaced by `ev_grinder.py`. Historical result kept at `regime_dtss_20260313_095056.json`. |
| `setup_grinder.py` | `archive/shelved_scripts/` | Replaced by `ev_grinder.py`. Historical result kept at `setup_dtss_20260313_135931.json`. |
| `archive/shelved_docs/TODO_REWRITE.md` | `archive/shelved_docs/` | LSP rewrite plan from Feb 2026. Architecture abandoned. |
| `archive/shelved_docs/DARTBOARD_DESIGN.md` | `archive/shelved_docs/` | Dartboard scoring architecture. Tested and rejected (see doc internal notes). |
| `archive/shelved_docs/MULTISTAGE_EXIT_GRINDER.md` | `archive/shelved_docs/` | Old multistage exit idea, folded into `profit_grinder.py` design. |
| `archive/shelved_docs/REFINEMENT_GRIND_FIX.md` | `archive/shelved_docs/` | Obsolete fix doc for the refinement grinder. Current architecture lives in `REFINEMENT_GRINDER.md`. |
| `archive/shelved_docs/EXPRESSION_ENGINE_V2.md` | `archive/shelved_docs/` | Incomplete plan for new expression capabilities. Current authoritative version is root `EXPRESSION_ENGINE_V2.md`. |

## Not shelved (despite previous entries)

- **`pipeline_agent.py`** is NOT shelved. Lives at `local_runner/pipeline_agent.py`, active after the 2026-03-08 fixes (see `BUGS.md` session notes for the step-ID fix + subsequent updates on 2026-03-14). A prior version of this doc listed it as shelved; that was incorrect.
- **`CONSENSUS_SPEC.md`** was previously listed as shelved. It has now been deleted outright (was a 5-line "delete me" stub). Signal consensus spec lives in `SIGNAL_GRINDER.md`, refinement consensus in `REFINEMENT_GRINDER.md`.
