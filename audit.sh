#!/bin/bash
# ScanPerfect Code Auditor
# Runs automatically after every commit via git hook

REPO_ROOT="$(git rev-parse --show-toplevel)"
DIFF="$(git diff HEAD~1 HEAD)"
CHANGED="$(git diff --name-only HEAD~1 HEAD)"
COMMIT_MSG="$(git log -1 --pretty=%s)"

# Map changed files to their spec docs
SPECS=""
echo "$CHANGED" | grep -q "expr_cache_builder" && SPECS="$SPECS EXPRESSION_ENGINE_V2.md"
echo "$CHANGED" | grep -q "scanperfect.py" && SPECS="$SPECS UI_FLOW.md"
echo "$CHANGED" | grep -q "nightly.py" && SPECS="$SPECS NIGHTLY_REFRESH.md LOCALIZE.md"
echo "$CHANGED" | grep -q "seed_vault" && SPECS="$SPECS LOCALIZE.md"
echo "$CHANGED" | grep -q "pyramid_grinder\|signal_grinder" && SPECS="$SPECS SIGNAL_GRINDER.md"
echo "$CHANGED" | grep -q "refinement" && SPECS="$SPECS REFINEMENT_GRINDER.md"
echo "$CHANGED" | grep -q "ev_grinder" && SPECS="$SPECS EV_GRINDER.md"
echo "$CHANGED" | grep -q "cache_builder.py" && SPECS="$SPECS LOCALIZE.md NIGHTLY_REFRESH.md"
echo "$CHANGED" | grep -q "matrix_builder" && SPECS="$SPECS LOCALIZE.md"
echo "$CHANGED" | grep -q "signal_filter\|signal_exit" && SPECS="$SPECS PIPELINE_V2.md"
echo "$CHANGED" | grep -q "entry_candle" && SPECS="$SPECS ENTRY_GRINDER.md"
echo "$CHANGED" | grep -q "profit_grinder" && SPECS="$SPECS PIPELINE_V2.md"
echo "$CHANGED" | grep -q "market_cache" && SPECS="$SPECS LOCALIZE.md"

# If no spec matched, use PIPELINE_V2.md as fallback
[ -z "$SPECS" ] && SPECS="PIPELINE_V2.md"

# Read the spec docs
SPEC_CONTENT=""
for spec in $SPECS; do
    if [ -f "$REPO_ROOT/$spec" ]; then
        SPEC_CONTENT="$SPEC_CONTENT
========== $spec ==========
$(cat "$REPO_ROOT/$spec")
"
    fi
done

# Read the full content of every changed file (not just the diff)
FULL_FILES=""
while IFS= read -r file; do
    if [ -f "$REPO_ROOT/$file" ]; then
        FULL_FILES="$FULL_FILES
========== FULL FILE: $file ==========
$(cat "$REPO_ROOT/$file")
"
    fi
done <<< "$CHANGED"

# Run the audit
RESULT=$(claude -p "You are a code auditor for ScanPerfect, a quantitative swing trading screener.

YOUR ROLE: You evaluate code. You do NOT write code. You do NOT suggest fixes. You do NOT say 'consider doing X.' You ONLY evaluate and report PASS or FAIL with evidence.

You have been given:
- A git diff (what changed)
- The full content of every changed file (so you can see context beyond the diff)
- The relevant specification documents
- A list of project-wide rules below

PROJECT-WIDE RULES (violations of any of these are automatic SPEC COMPLIANCE FAILs):
- NEVER use bar_idx or scan_idx to match examples. ALWAYS use entry_date.
- NEVER use yfinance outside of cache_builder.py. All other scripts use local caches.
- ProcessPoolExecutor for CPU-bound parallel work. NEVER ThreadPoolExecutor for CPU work.
- del + gc.collect() between phases is intentional RAM management. Must be preserved.
- Every grinder must produce results where 100% of setup examples pass.
- No API or network calls in grinder/scorer pipelines. All data from local caches.
- Output .npz files must be float32, np.load compatible.
- Expression fingerprint system must be preserved (triggers full rebuild on library changes).
- All exit/entry conditions must be self-referential and normalized (divided by ADR), never absolute price targets.
- Railway is seed vault + file mirror ONLY. No OHLCV dependency on Railway.

EVALUATE THESE THREE CRITERIA:

1. PURPOSE
Read the spec doc for the component being changed. What does the spec say this component should do? Does the code change achieve that purpose? Does it do what it's supposed to do, or does it do something else that might look similar but isn't what was specified?
FAIL examples: spec says 'incremental append of one new bar' but code rebuilds the full expression engine. Spec says 'backup all non-rebuildable data' but code only backs up 3 of 8 file patterns.

2. SPEC COMPLIANCE
Go through the spec requirements one by one. For each requirement, check whether the code satisfies it. Check:
- File paths: are inputs read from and outputs written to the correct directories?
- Data flow: is data coming from the right source (local cache vs network vs database)?
- Function signatures: do they match what callers expect?
- Worker patterns: correct executor type, correct parallelism?
- RAM management: del + gc.collect patterns present where required?
- Output formats: correct types, schemas, file extensions?
- Constants and thresholds: do hardcoded values match the spec?
FAIL if ANY spec requirement is not met. Cite the specific spec line and what the code does differently.

3. REGRESSION SAFETY
Does this change break or conflict with anything else in the project?
- If function signatures changed, will existing callers break?
- If output file paths or names changed, will downstream readers find them?
- If data formats changed, will consumers handle the new format?
- If nightly pipeline steps changed, is the ordering still correct?
- If imports were added, are all dependencies available in the environment?
- Does this change affect other scripts that read the same files?
- Could this change cause silent wrong numbers (plausible-looking output that is actually incorrect)?
FAIL if any regression risk exists. The most dangerous failure mode is code that produces plausible wrong numbers with no errors — flag anything that could cause this.

4. CODE QUALITY
Is this a clean, minimal implementation? Or is it bloated, patched-together, or working around a problem instead of solving it properly?
- Does the code do the job in the fewest lines and simplest logic possible?
- Are there unnecessary layers of abstraction, wrapper functions, or indirection?
- Is there dead code, commented-out code, or redundant checks?
- Does it copy-paste logic that already exists elsewhere instead of reusing it?
- Does it add special cases or if-else chains that paper over a design problem?
- Is it solving the symptom instead of the root cause?
- Would a senior engineer look at this and say 'this should be rewritten' or 'this is clean'?
FAIL if the code is unnecessarily complex, patched together, or avoids a proper solution in favor of a shortcut. This project has to be maintained long-term by someone who cannot read code — every line of unnecessary complexity is a liability.

OUTPUT FORMAT:

If all four criteria pass:
PASS

If any criterion fails:
FAIL: [which criteria failed] — [one sentence why]

Examples:
PASS
FAIL: purpose — builds full engine instead of incremental append
FAIL: spec, quality — wrong ticker count (4,167 vs 10,542), bandaid architecture
FAIL: regression — changes output path, downstream matrix_builder won't find files

Be pedantic. Be thorough. A false PASS is catastrophic — the project owner cannot read code and relies entirely on this audit to catch problems. When in doubt, FAIL.

COMMIT: $COMMIT_MSG

CHANGED FILES:
$CHANGED

DIFF:
$DIFF

FULL FILE CONTENTS:
$FULL_FILES

SPECIFICATION DOCUMENTS:
$SPEC_CONTENT
")

# Output
echo ""
echo "$RESULT"
echo ""

# Save to log
echo "$RESULT" > "$REPO_ROOT/local_runner/cache/last_audit.txt"
echo "Audit saved to local_runner/cache/last_audit.txt"
