#!/bin/bash
# ScanPerfect Code Auditor
# Manual trigger only. Run: ./audit.sh
# Diffs from last audited commit to current HEAD.
# Uses DEPENDENCY_MAP.md to check downstream consumers.

REPO_ROOT="$(git rev-parse --show-toplevel)"
STATE_FILE="$REPO_ROOT/local_runner/cache/last_audited_commit.txt"
DEP_MAP="$REPO_ROOT/DEPENDENCY_MAP.md"
DATA_CONTRACT="$REPO_ROOT/DATA_CONTRACT.md"

# ── Determine diff range ──

if [ -f "$STATE_FILE" ]; then
    LAST_AUDITED=$(cat "$STATE_FILE" | tr -d '[:space:]')
    # Verify the commit still exists (could have been rebased away)
    if ! git cat-file -e "$LAST_AUDITED" 2>/dev/null; then
        echo "  Warning: last audited commit $LAST_AUDITED no longer exists. Diffing HEAD~1."
        LAST_AUDITED=$(git rev-parse HEAD~1)
    fi
else
    echo "  First run — no audit history. Diffing HEAD~1."
    LAST_AUDITED=$(git rev-parse HEAD~1)
fi

CURRENT=$(git rev-parse HEAD)

if [ "$LAST_AUDITED" = "$CURRENT" ]; then
    echo ""
    echo "  Nothing to audit — HEAD is the same as last audited commit."
    echo "  ($CURRENT)"
    echo ""
    exit 0
fi

# How many commits in this batch
N_COMMITS=$(git rev-list --count "$LAST_AUDITED".."$CURRENT")
SHORT_FROM=$(git rev-parse --short "$LAST_AUDITED")
SHORT_TO=$(git rev-parse --short "$CURRENT")

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "  ScanPerfect Code Auditor"
echo "  Auditing $N_COMMITS commit(s): $SHORT_FROM → $SHORT_TO"
echo "═══════════════════════════════════════════════════════════"
echo ""

# ── Gather diff and changed files ──

DIFF="$(git diff "$LAST_AUDITED" "$CURRENT")"
CHANGED="$(git diff --name-only "$LAST_AUDITED" "$CURRENT")"
COMMIT_MSGS="$(git log --oneline "$LAST_AUDITED".."$CURRENT")"

if [ -z "$CHANGED" ]; then
    echo "  No files changed. Nothing to audit."
    echo "$CURRENT" > "$STATE_FILE"
    exit 0
fi

echo "  Changed files:"
echo "$CHANGED" | sed 's/^/    /'
echo ""

# ── Map changed files to spec docs ──

SPECS=""
echo "$CHANGED" | grep -q "expr_cache_builder\|vectorized_cache_builder\|vectorized_dispatch\|vectorized_indicators" && SPECS="$SPECS EXPRESSION_ENGINE_V2.md"
echo "$CHANGED" | grep -q "scanperfect.py" && SPECS="$SPECS UI_FLOW.md"
echo "$CHANGED" | grep -q "nightly.py" && SPECS="$SPECS NIGHTLY_REFRESH.md LOCALIZE.md"
echo "$CHANGED" | grep -q "seed_vault" && SPECS="$SPECS LOCALIZE.md"
echo "$CHANGED" | grep -q "pyramid_grinder\|signal_grinder\|spiderweb" && SPECS="$SPECS SIGNAL_GRINDER.md"
echo "$CHANGED" | grep -q "refinement" && SPECS="$SPECS REFINEMENT_GRINDER.md"
echo "$CHANGED" | grep -q "ev_grinder\|ev_tree_scorer" && SPECS="$SPECS EV_GRINDER.md"
echo "$CHANGED" | grep -q "cache_builder.py" && SPECS="$SPECS LOCALIZE.md NIGHTLY_REFRESH.md"
echo "$CHANGED" | grep -q "matrix_builder" && SPECS="$SPECS LOCALIZE.md"
echo "$CHANGED" | grep -q "signal_filter\|signal_exit" && SPECS="$SPECS PIPELINE_V2.md"
echo "$CHANGED" | grep -q "entry_candle\|entry_grinder" && SPECS="$SPECS ENTRY_GRINDER.md"
echo "$CHANGED" | grep -q "profit_grinder" && SPECS="$SPECS PROFIT_GRINDER.md"
echo "$CHANGED" | grep -q "market_cache_builder\|fetch_missing_market" && SPECS="$SPECS LOCALIZE.md"
echo "$CHANGED" | grep -q "consensus_engine" && SPECS="$SPECS CONSENSUS_SPEC.md"
echo "$CHANGED" | grep -q "exit_grinder\|exit_compute\|exit_expressions" && SPECS="$SPECS PIPELINE_V2.md"
echo "$CHANGED" | grep -q "fetch_fundamentals\|fetch_universe\|build_tradable" && SPECS="$SPECS NIGHTLY_REFRESH.md"
echo "$CHANGED" | grep -q "lsp_detector\|algo_line_detector" && SPECS="$SPECS EXPRESSION_ENGINE_V2.md"
echo "$CHANGED" | grep -q "brute_expressions" && SPECS="$SPECS EXPRESSION_ENGINE_V2.md"
echo "$CHANGED" | grep -q "server.py" && SPECS="$SPECS DATA_CONTRACT.md"
echo "$CHANGED" | grep -q "local_db\|analysis_api" && SPECS="$SPECS DATA_CONTRACT.md"

# Deduplicate specs
SPECS=$(echo "$SPECS" | tr ' ' '\n' | sort -u | tr '\n' ' ')

[ -z "$SPECS" ] && SPECS="PIPELINE_V2.md"

echo "  Spec docs: $SPECS"

# ── Read spec doc contents ──

SPEC_CONTENT=""
for spec in $SPECS; do
    if [ -f "$REPO_ROOT/$spec" ]; then
        SPEC_CONTENT="$SPEC_CONTENT
========== $spec ==========
$(cat "$REPO_ROOT/$spec")
"
    fi
done

# ── Read full content of every changed file ──

FULL_FILES=""
while IFS= read -r file; do
    if [ -f "$REPO_ROOT/$file" ]; then
        FULL_FILES="$FULL_FILES
========== FULL FILE: $file ==========
$(cat "$REPO_ROOT/$file")
"
    fi
done <<< "$CHANGED"

# ── Find downstream consumers from DEPENDENCY_MAP.md ──
# For each changed .py file, find its "Downstream Consumers" section
# and pull those files too.

DOWNSTREAM_FILES=""
DOWNSTREAM_NAMES=""

if [ -f "$DEP_MAP" ]; then
    while IFS= read -r file; do
        # Only care about .py files
        echo "$file" | grep -q "\.py$" || continue

        basename=$(basename "$file" .py)

        # Search DEPENDENCY_MAP.md for downstream consumer filenames
        # Look for lines like "- `some_script.py` —" under Downstream Consumers sections
        # that appear after a heading matching our changed file
        consumers=$(awk -v target="$basename" '
            BEGIN { in_section=0; in_downstream=0 }
            /^### / {
                if (index(tolower($0), tolower(target))) { in_section=1; in_downstream=0 }
                else { in_section=0; in_downstream=0 }
            }
            in_section && /Downstream Consumers/ { in_downstream=1; next }
            in_section && in_downstream && /^$/ { in_downstream=0 }
            in_section && in_downstream && /^##/ { in_downstream=0 }
            in_section && in_downstream && /`[a-z_]+\.py`/ {
                match($0, /`([a-z_]+\.py)`/, arr)
                if (arr[1] != "") print arr[1]
            }
        ' "$DEP_MAP" 2>/dev/null)

        if [ -n "$consumers" ]; then
            DOWNSTREAM_NAMES="$DOWNSTREAM_NAMES
  $file impacts: $consumers"
            for consumer in $consumers; do
                # Skip if this file is already in the changed set
                echo "$CHANGED" | grep -q "$consumer" && continue
                # Find the actual path
                for search_dir in "local_runner" "scripts" "."; do
                    candidate="$REPO_ROOT/$search_dir/$consumer"
                    if [ -f "$candidate" ]; then
                        rel_path="$search_dir/$consumer"
                        # Don't add duplicates
                        echo "$DOWNSTREAM_FILES" | grep -q "$rel_path" && break
                        DOWNSTREAM_FILES="$DOWNSTREAM_FILES
========== DOWNSTREAM: $rel_path ==========
$(cat "$candidate")
"
                        break
                    fi
                done
            done
        fi
    done <<< "$CHANGED"
fi

if [ -n "$DOWNSTREAM_NAMES" ]; then
    echo ""
    echo "  Downstream consumers pulled:"
    echo "$DOWNSTREAM_NAMES" | sed 's/^/  /'
fi

# ── Read DATA_CONTRACT.md and DEPENDENCY_MAP.md ──

CONTRACT_CONTENT=""
if [ -f "$DATA_CONTRACT" ]; then
    CONTRACT_CONTENT="$(cat "$DATA_CONTRACT")"
fi

DEPMAP_CONTENT=""
if [ -f "$DEP_MAP" ]; then
    DEPMAP_CONTENT="$(cat "$DEP_MAP")"
fi

echo ""
echo "  Running audit via claude -p ..."
echo ""

# ── Run the audit ──

RESULT=$(claude -p "You are a code auditor for ScanPerfect, a quantitative swing trading screener.

YOUR ROLE: You evaluate code changes. You do NOT write code. You do NOT suggest fixes. You ONLY evaluate and report PASS or FAIL with evidence.

You have been given:
- A git diff (what changed across $N_COMMITS commits)
- The full content of every changed file
- The full content of downstream consumer files (files that read output from the changed files)
- The relevant specification documents
- DATA_CONTRACT.md (schemas, file formats, data flow rules)
- DEPENDENCY_MAP.md (per-component inputs, outputs, downstream consumers)

EVALUATE THESE FOUR CRITERIA:

1. PURPOSE
Read the spec doc for each changed component. What does the spec say this component should do?
Does the code change still achieve that purpose? Does it do what the spec says, or has it drifted
to do something else?

FAIL if the code no longer fulfills the purpose defined in its spec doc.

2. SPEC COMPLIANCE
Go through the spec requirements for each changed component. For each requirement, check whether
the code satisfies it:
- File paths: inputs read from and outputs written to the correct locations?
- Data flow: data coming from the right source (local cache vs network vs database)?
- Function signatures: match what callers expect?
- Worker patterns: correct executor type, correct parallelism?
- RAM management: del + gc.collect patterns present where required?
- Output formats: correct types, schemas, file extensions?
- Constants and thresholds: hardcoded values match the spec?

FAIL if ANY spec requirement is not met. Cite the specific spec requirement and what the code does differently.

3. REGRESSION SAFETY
Use DATA_CONTRACT.md and DEPENDENCY_MAP.md to check:
- If output file paths or names changed, will downstream consumers (listed in DEPENDENCY_MAP.md) find them?
- If output data format changed, will downstream consumers handle the new format?
- If function signatures changed, will callers (listed in DEPENDENCY_MAP.md) break?
- If file schemas changed, does DATA_CONTRACT.md still describe them accurately?
- Could this change cause silent wrong numbers — plausible-looking output that is actually incorrect?

You have been given the full source of downstream consumer files. CHECK THEM. Verify that the
changed code's outputs are still compatible with how downstream files read them.

FAIL if any regression risk exists. The most dangerous failure is code that produces plausible
wrong numbers with no errors.

4. CODE QUALITY
- Is there dead code, commented-out code, or unused imports?
- Is there redundant logic or copy-pasted code that should be shared?
- Are there unnecessary layers of abstraction or wrapper functions?
- Is the code efficient — no obvious performance problems?
- Would a senior engineer say this is clean and maintainable?

FAIL if there is dead code that should be cleaned up, or if the implementation is unnecessarily
complex when a simpler approach would work.

OUTPUT FORMAT:

If all four criteria pass:
PASS

If any criterion fails:
FAIL: [which criteria failed] — [one sentence why]

Examples:
PASS
FAIL: purpose — builds full engine instead of incremental append
FAIL: spec, quality — wrong ticker count, 40 lines of dead code
FAIL: regression — changes output dict keys, ev_grinder reads old keys
FAIL: quality — 3 commented-out functions, unused imports

Be thorough. A false PASS is dangerous — the project owner cannot read code and relies
entirely on this audit to catch problems. When in doubt, FAIL.

COMMITS ($N_COMMITS):
$COMMIT_MSGS

CHANGED FILES:
$CHANGED

DIFF:
$DIFF

FULL FILE CONTENTS:
$FULL_FILES

DOWNSTREAM CONSUMER FILES:
$DOWNSTREAM_FILES

DATA_CONTRACT.md:
$CONTRACT_CONTENT

DEPENDENCY_MAP.md:
$DEPMAP_CONTENT

SPECIFICATION DOCUMENTS:
$SPEC_CONTENT
")

# ── Output ──

echo ""
echo "═══════════════════════════════════════════════════════════"
echo "$RESULT"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Save result
mkdir -p "$REPO_ROOT/local_runner/cache"
echo "$RESULT" > "$REPO_ROOT/local_runner/cache/last_audit.txt"
echo "  Audit saved to local_runner/cache/last_audit.txt"

# ── Update last audited commit ──

if echo "$RESULT" | head -1 | grep -q "^PASS"; then
    echo "$CURRENT" > "$STATE_FILE"
    echo "  Last audited commit updated to $SHORT_TO"
else
    echo "  FAIL — last audited commit NOT updated (stays at $SHORT_FROM)"
    echo "  Fix the issues and re-run ./audit.sh"
fi

echo ""
