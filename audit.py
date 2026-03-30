"""
ScanPerfect Code Auditor
Run: python audit.py           (audits last commit only)
     python audit.py --all     (audits everything since last audit)
"""

import os
import re
import subprocess
import sys

REPO_ROOT = subprocess.check_output(
    ["git", "rev-parse", "--show-toplevel"], text=True
).strip()

STATE_FILE = os.path.join(REPO_ROOT, "local_runner", "cache", "last_audited_commit.txt")
DEP_MAP = os.path.join(REPO_ROOT, "DEPENDENCY_MAP.md")
DATA_CONTRACT = os.path.join(REPO_ROOT, "DATA_CONTRACT.md")


def git(cmd):
    return subprocess.run(
        ["git"] + cmd, capture_output=True, text=True, cwd=REPO_ROOT
    ).stdout.strip()


def git_ok(cmd):
    return subprocess.run(
        ["git"] + cmd, capture_output=True, text=True, cwd=REPO_ROOT
    ).returncode == 0


def read_file(path):
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def extract_interface_lines(filepath, changed_basenames):
    """Extract only the lines from a downstream file that interact with changed components.
    
    Pulls: imports, file path references, pickle/npz loads, dict key access,
    function calls to changed modules. Much smaller than full file content.
    """
    content = read_file(os.path.join(REPO_ROOT, filepath))
    if not content:
        return ""

    keywords = []
    for basename in changed_basenames:
        name = os.path.splitext(basename)[0]
        keywords.append(name)
        # Also match common file artifacts: .pkl, .npz, .json patterns
        keywords.extend([f"{name}.", f"from {name}", f"import {name}"])

    # Always look for these contract-relevant patterns
    contract_patterns = [
        r'\.pkl', r'\.npz', r'\.json', r'\.db',
        r'pickle\.load', r'pickle\.dump', r'np\.load', r'np\.save',
        r'open\(', r'sqlite3\.connect',
        r'import ', r'from ',
        r'def ', r'class ',
    ]

    relevant = []
    lines = content.split('\n')
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        
        # Check if line references any changed component
        line_lower = line.lower()
        hit = False
        for kw in keywords:
            if kw.lower() in line_lower:
                hit = True
                break
        
        # Check contract-relevant patterns
        if not hit:
            for pat in contract_patterns:
                if re.search(pat, line):
                    hit = True
                    break
        
        if hit:
            relevant.append(f"{i+1}: {line}")

    if not relevant:
        return ""
    
    return f"--- {filepath} (interface lines) ---\n" + "\n".join(relevant)


def find_downstream_consumers(changed_files, depmap_content):
    """Parse DEPENDENCY_MAP.md to find downstream consumer files."""
    if not depmap_content:
        return [], []

    downstream_names = []
    downstream_files = set()

    for filepath in changed_files:
        if not filepath.endswith(".py"):
            continue

        basename = os.path.splitext(os.path.basename(filepath))[0]
        in_section = False
        in_downstream = False
        consumers = []

        for line in depmap_content.split("\n"):
            if line.startswith("### "):
                if basename.lower() in line.lower():
                    in_section = True
                    in_downstream = False
                else:
                    in_section = False
                    in_downstream = False
                continue
            if in_section and "Downstream Consumers" in line:
                in_downstream = True
                continue
            if in_section and in_downstream:
                if line.startswith("##") or line.startswith("---"):
                    in_downstream = False
                    continue
                matches = re.findall(r"`([a-z_]+\.py)`", line)
                consumers.extend(matches)

        if consumers:
            downstream_names.append(f"  {filepath} impacts: {', '.join(consumers)}")
            for consumer in consumers:
                if any(consumer in cf for cf in changed_files):
                    continue
                for search_dir in ["local_runner", "scripts", "."]:
                    candidate = os.path.join(REPO_ROOT, search_dir, consumer)
                    if os.path.exists(candidate):
                        downstream_files.add(os.path.join(search_dir, consumer))
                        break

    return downstream_names, list(downstream_files)


def map_to_specs(changed_files):
    """Map changed files to their governing spec documents.
    
    Each component maps to EVERY spec that defines contracts it participates in:
    - The spec that describes its own behavior
    - Specs that define formats it reads (input contracts)
    - Specs that define formats it writes (output contracts)
    
    DATA_CONTRACT.md is always included (sent separately in the prompt).
    """
    changed_str = "\n".join(changed_files)
    specs = set()

    # EXPRESSION_ENGINE_V2.md — expression library, npz format, pickle->cache data flow
    if re.search(r"cache_builder\.py|expr_cache_builder|vectorized_cache_builder|vectorized_dispatch|vectorized_indicators|brute_expressions|expression_engine|backtest_conditions|lsp_detector|algo_line_detector|market_cache_builder|profiling_engine", changed_str):
        specs.add("EXPRESSION_ENGINE_V2.md")

    # SIGNAL_GRINDER.md — signal grind + consensus
    if re.search(r"pyramid_grinder|spiderweb|consensus_engine", changed_str):
        specs.add("SIGNAL_GRINDER.md")

    # REFINEMENT_GRINDER.md — refinement grind + cluster classification
    if re.search(r"pyramid_grinder", changed_str):
        specs.add("REFINEMENT_GRINDER.md")

    # EV_GRINDER.md — correlative scoring
    if re.search(r"ev_grinder|ev_tree_scorer", changed_str):
        specs.add("EV_GRINDER.md")

    # PROFIT_GRINDER.md — exit optimization
    if re.search(r"profit_grinder", changed_str):
        specs.add("PROFIT_GRINDER.md")

    # ENTRY_GRINDER.md — stop placement
    if re.search(r"entry_grinder|entry_candle_scorer", changed_str):
        specs.add("ENTRY_GRINDER.md")

    # PIPELINE_V2.md — overall pipeline flow and phase ordering
    if re.search(r"nightly\.py|signal_filter|signal_exit_grinder|exit_grinder|exit_compute|exit_expressions|pipeline_agent", changed_str):
        specs.add("PIPELINE_V2.md")

    # NIGHTLY_REFRESH.md — 10-step nightly pipeline
    if re.search(r"nightly\.py|cache_builder\.py|expr_cache_builder|market_cache_builder|fetch_fundamentals|fetch_universe|build_tradable|seed_vault|matrix_builder", changed_str):
        specs.add("NIGHTLY_REFRESH.md")

    # UI_FLOW.md — PySide6 desktop app
    if re.search(r"scanperfect\.py", changed_str):
        specs.add("UI_FLOW.md")

    # GRIND_STORAGE.md — file naming, storage locations, Railway mirror
    if re.search(r"pyramid_grinder|signal_filter|signal_exit_grinder|entry_grinder|entry_candle_scorer|ev_grinder|profit_grinder|consensus_engine|exit_grinder|file_mirror|grind_uploader|seed_vault|bulk_mirror", changed_str):
        specs.add("GRIND_STORAGE.md")

    # LOCALIZE.md — local vs Railway architecture
    if re.search(r"server\.py|seed_vault|file_mirror|grind_uploader|bulk_mirror|agent\.py|pipeline_agent|fetch_universe|build_tradable", changed_str):
        specs.add("LOCALIZE.md")

    if not specs:
        specs.add("PIPELINE_V2.md")

    return sorted(specs)


def main():
    # ── Parse args ──
    batch_mode = "--all" in sys.argv

    # ── Determine diff range ──
    if batch_mode:
        if os.path.exists(STATE_FILE):
            last = read_file(STATE_FILE).strip()
            if not git_ok(["cat-file", "-e", last]):
                print(f"  Warning: last audited commit gone. Using HEAD~1.")
                last = git(["rev-parse", "HEAD~1"])
        else:
            print("  First run — using HEAD~1.")
            last = git(["rev-parse", "HEAD~1"])
        diff_from = last
        label = "batch"
    else:
        diff_from = git(["rev-parse", "HEAD~1"])
        label = "last commit"

    current = git(["rev-parse", "HEAD"])

    if diff_from == current:
        print(f"\n  Nothing to audit — HEAD matches {label} reference.\n")
        return

    n_commits = git(["rev-list", "--count", f"{diff_from}..{current}"])
    short_from = git(["rev-parse", "--short", diff_from])
    short_to = git(["rev-parse", "--short", current])

    print()
    print("=" * 60)
    print("  ScanPerfect Code Auditor")
    print(f"  Mode: {label} — {n_commits} commit(s): {short_from} -> {short_to}")
    print("=" * 60)
    print()

    # ── Gather diff and changed files ──
    diff = git(["diff", diff_from, current])
    changed_str = git(["diff", "--name-only", diff_from, current])
    commit_msgs = git(["log", "--oneline", f"{diff_from}..{current}"])

    if not changed_str:
        print("  No files changed. Nothing to audit.")
        return

    changed_files = [f for f in changed_str.split("\n") if f.strip()]

    print("  Changed files:")
    for f in changed_files:
        print(f"    {f}")
    print()

    # ── Map to specs ──
    specs = map_to_specs(changed_files)
    print(f"  Spec docs: {', '.join(specs)}")

    spec_content = ""
    for spec in specs:
        path = os.path.join(REPO_ROOT, spec)
        c = read_file(path)
        if c:
            spec_content += f"\n{'='*10} {spec} {'='*10}\n{c}\n"

    # ── Read diff only (NOT full file contents — the diff has everything needed) ──

    # ── Find downstream consumers — extract interface lines only ──
    depmap_content = read_file(DEP_MAP)
    downstream_names, downstream_paths = find_downstream_consumers(changed_files, depmap_content)

    changed_basenames = [os.path.basename(f) for f in changed_files]
    
    downstream_interfaces = ""
    if downstream_names:
        print()
        print("  Downstream consumers:")
        for name in downstream_names:
            print(f"  {name}")
        
        for rel_path in downstream_paths:
            interface = extract_interface_lines(rel_path, changed_basenames)
            if interface:
                downstream_interfaces += "\n" + interface + "\n"

    # ── Read DATA_CONTRACT (compact — just the key sections) ──
    contract_content = read_file(DATA_CONTRACT)

    # ── Extract just the relevant sections from DEPENDENCY_MAP ──
    depmap_sections = ""
    if depmap_content:
        for filepath in changed_files:
            if not filepath.endswith(".py"):
                continue
            basename = os.path.splitext(os.path.basename(filepath))[0]
            in_section = False
            section_lines = []
            for line in depmap_content.split("\n"):
                if line.startswith("### ") and basename.lower() in line.lower():
                    in_section = True
                    section_lines.append(line)
                    continue
                elif line.startswith("### ") and in_section:
                    break
                elif line.startswith("## ") and in_section:
                    break
                if in_section:
                    section_lines.append(line)
            if section_lines:
                depmap_sections += "\n".join(section_lines) + "\n\n"

    print()
    print("  Running audit via claude -p ...")
    print()

    # ── Build prompt ──
    prompt = f"""You are a code auditor for ScanPerfect, a quantitative swing trading screener.

You evaluate code changes. You do NOT write code or suggest fixes. Report PASS or FAIL with evidence.

FOUR CRITERIA:

1. PURPOSE — Does the code still do what the spec doc says this component should do?

2. SPEC COMPLIANCE — File paths, data formats, function signatures, worker patterns, RAM management, output formats, constants — all match the spec?

3. REGRESSION SAFETY — Using the dependency map sections and downstream interface lines below, check:
   - Changed output paths/names: will downstream consumers find them?
   - Changed output format: will downstream consumers parse them?
   - Changed function signatures: will callers break?
   - Silent wrong numbers: could this produce plausible but incorrect output?

4. CODE QUALITY — Dead code, commented-out code, unused imports, redundant logic, unnecessary complexity?

OUTPUT:
PASS
or
FAIL: [criteria] — [one sentence why]

Be thorough. A false PASS is dangerous — the project owner cannot read code.

COMMITS ({n_commits}):
{commit_msgs}

CHANGED FILES:
{changed_str}

DIFF:
{diff}

DEPENDENCY MAP (relevant sections):
{depmap_sections}

DOWNSTREAM CONSUMER INTERFACE LINES:
{downstream_interfaces}

DATA CONTRACT:
{contract_content}

SPEC DOCS:
{spec_content}
"""

    # ── Run claude -p ──
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    try:
        prompt_file = os.path.join(REPO_ROOT, "local_runner", "cache", "_audit_prompt.txt")
        os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
        with open(prompt_file, "w", encoding="utf-8") as pf:
            pf.write(prompt)

        cmd = 'claude -p < "' + prompt_file + '"'
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=300,
            shell=True, env=env
        )
        output = result.stdout.strip()
    except FileNotFoundError:
        print("  ERROR: 'claude' command not found.")
        print("  Install: npm install -g @anthropic-ai/claude-code")
        return
    except subprocess.TimeoutExpired:
        print("  ERROR: Audit timed out after 5 minutes.")
        return

    if not output:
        print("  ERROR: claude -p returned empty output.")
        if result.stderr:
            print(f"  stderr: {result.stderr[:500]}")
        return

    # ── Output ──
    print()
    print("=" * 60)
    print(output)
    print("=" * 60)
    print()

    os.makedirs(os.path.join(REPO_ROOT, "local_runner", "cache"), exist_ok=True)
    with open(os.path.join(REPO_ROOT, "local_runner", "cache", "last_audit.txt"), "w") as f:
        f.write(output)
    print("  Audit saved to local_runner/cache/last_audit.txt")

    # ── Update state ──
    first_line = output.split("\n")[0].strip()
    if first_line.startswith("PASS"):
        with open(STATE_FILE, "w") as f:
            f.write(current)
        print(f"  Last audited commit updated to {short_to}")
    else:
        print(f"  FAIL — last audited commit NOT updated (stays at {short_from})")
        print("  Fix the issues and re-run: python audit.py")

    print()


if __name__ == "__main__":
    main()
