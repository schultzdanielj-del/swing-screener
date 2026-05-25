"""Apply the theme placement audit (local_runner/cache/theme_placement_output.json)
to local_runner/theme_map.py.

Skips the 4 asymmetry-contentious strips per session decision:
NRG, NXPI, QRVO, SWKS (audit's "remove from current cohort" calls that
co-movement evidence would reject).

Per-bucket behavior:
  A_ungrouped_to_theme        : add ticker to proposed_themes
  B_cross_listing_added       : add ticker to secondary themes (proposed - current)
  C_change_requires_review    : strip from current_themes not in proposed,
                                add to proposed_themes not in current
  C_review_required           : per-ticker manual disposition (see TABLE below)
  D_confirmed_no_change       : no-op
  E_stays_ungrouped           : no-op

For each theme touched: first apply removals, then apply additions.
Additions get a rationale comment from theme_reasoning (truncated).

Removals handle two formats:
  - own-line:    "TICKER",   # rationale: ...
  - inline list: "TICKER", "OTHER", "MORE",

Idempotent: re-running skips already-added and already-removed tickers.
"""
from __future__ import annotations
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "local_runner"))

from local_runner.theme_map import THEMES  # noqa: E402

AUDIT_PATH = REPO_ROOT / "local_runner" / "cache" / "theme_placement_output.json"
THEME_MAP_PATH = REPO_ROOT / "local_runner" / "theme_map.py"

# Per-session decision: skip these C_review_required strips (asymmetry rule).
SKIP_TICKERS = {"NRG", "NXPI", "QRVO", "SWKS"}

# Manual dispositions for C_review_required (no structured proposed_themes in JSON).
# Each entry: ticker -> (remove_from_themes, add_to_themes, rationale)
C_REVIEW_MANUAL = {
    "SN": (
        ["building_products"],
        ["consumer_retail", "ecommerce"],
        "small kitchen appliances + vacuums + beauty (SharkNinja) — consumer brand, not building materials; DTC mix adds ecommerce",
    ),
    "WOK": (
        ["medtech_devices_consumer_health"],
        ["fintech_disruptors"],
        "Cohen Circle is a fintech-focused SPAC (Paya/Perella/INX track record), auto-placed medtech label was incorrect",
    ),
    # PBF: audit explicitly didn't pick between two options — leave unchanged
}

# Truncate rationale to keep theme_map.py readable.
RATIONALE_MAX_LEN = 180


def trim_rationale(text: str) -> str:
    if not text:
        return ""
    # Strip newlines and excess whitespace
    text = " ".join(text.split())
    if len(text) > RATIONALE_MAX_LEN:
        text = text[: RATIONALE_MAX_LEN - 3] + "..."
    return text


def compute_operations(audit_rows: list[dict]):
    """Returns dict[theme_id] -> {'add': [(ticker, rationale)], 'remove': [tickers]}."""
    ops: dict[str, dict] = {}

    def add(theme: str, ticker: str, rationale: str):
        ops.setdefault(theme, {"add": [], "remove": []})
        ops[theme]["add"].append((ticker, rationale))

    def remove(theme: str, ticker: str):
        ops.setdefault(theme, {"add": [], "remove": []})
        ops[theme]["remove"].append(ticker)

    skipped_tickers = []
    for r in audit_rows:
        ticker = r["ticker"]
        bucket = r.get("bucket", "")
        reasoning = trim_rationale(r.get("theme_reasoning") or "")
        cur = set(r.get("current_themes") or [])
        prop = set(r.get("proposed_themes") or [])

        if ticker in SKIP_TICKERS:
            skipped_tickers.append((ticker, bucket))
            continue

        if bucket == "A_ungrouped_to_theme":
            for theme in prop:
                add(theme, ticker, reasoning)

        elif bucket == "B_cross_listing_added":
            secondary = prop - cur
            for theme in secondary:
                add(theme, ticker, reasoning)

        elif bucket == "C_change_requires_review":
            for theme in cur - prop:
                remove(theme, ticker)
            for theme in prop - cur:
                add(theme, ticker, reasoning)

        elif bucket == "C_review_required":
            if ticker in C_REVIEW_MANUAL:
                remove_themes, add_themes, manual_rationale = C_REVIEW_MANUAL[ticker]
                for theme in remove_themes:
                    remove(theme, ticker)
                for theme in add_themes:
                    add(theme, ticker, trim_rationale(manual_rationale))
            # else: PBF and any other unrouted review_required — no-op

        elif bucket in ("D_confirmed_no_change", "E_stays_ungrouped"):
            continue

    return ops, skipped_tickers


def find_theme_block(src: str, theme: str):
    """Locate the (opening, body, closing) of THEMES["theme_id"] = [...]."""
    pattern = re.compile(
        r'(["\']' + re.escape(theme) + r'["\']\s*:\s*\[)([^\[\]]*?)(\]\s*,?)',
        re.DOTALL,
    )
    return pattern.search(src)


def remove_ticker_from_body(body: str, ticker: str) -> tuple[str, bool]:
    """Remove ticker from theme block body. Returns (new_body, did_remove).

    Handles two formats:
      own-line:    `        "TICKER",   # rationale: ...`  -> delete whole line
      inline list: `        "TICKER", "OTHER", ...`        -> strip just the entry
    """
    # Own-line form: whole line with the ticker quoted, optional trailing comma + comment
    own_line_re = re.compile(
        r'^[ \t]*["\']' + re.escape(ticker) + r'["\'][ \t]*,?[ \t]*(?:#[^\n]*)?\n',
        re.MULTILINE,
    )
    if own_line_re.search(body):
        new_body, n = own_line_re.subn("", body, count=1)
        if n > 0:
            return new_body, True

    # Inline form: `"TICKER",` possibly with surrounding spaces — strip the entry only
    inline_re = re.compile(r'["\']' + re.escape(ticker) + r'["\'][ \t]*,?[ \t]*')
    if inline_re.search(body):
        new_body, n = inline_re.subn("", body, count=1)
        if n > 0:
            # Cleanup: collapse multiple spaces left behind on the same line, strip trailing commas before newlines
            new_body = re.sub(r'[ \t]+\n', '\n', new_body)
            new_body = re.sub(r',[ \t]*,', ',', new_body)  # double-comma collapse
            return new_body, True

    return body, False


def insert_additions(body: str, additions: list[tuple[str, str]]) -> str:
    """Insert new ticker lines at the end of the theme body, before closing ]."""
    if not additions:
        return body

    # Detect indent from existing content; default to 8 spaces
    indent_match = re.search(r'^([ \t]+)["\']', body, re.MULTILINE)
    indent = indent_match.group(1) if indent_match else "        "

    new_lines = []
    for ticker, rationale in additions:
        if rationale:
            new_lines.append(f'{indent}"{ticker}",   # rationale: {rationale}')
        else:
            new_lines.append(f'{indent}"{ticker}",')

    body_stripped = body.rstrip()
    if body_stripped and not body_stripped.endswith(","):
        body_stripped += ","

    insertion = "\n" + "\n".join(new_lines)
    return body_stripped + insertion + "\n    "


def apply():
    with open(AUDIT_PATH, encoding="utf-8") as f:
        audit = json.load(f)
    rows = audit["placements"]

    ops, skipped = compute_operations(rows)

    with open(THEME_MAP_PATH, "r", encoding="utf-8") as f:
        src = f.read()

    total_added = 0
    total_removed = 0
    missing_themes = []
    skipped_already_member = 0
    skipped_already_absent = 0

    print("\n=== Per-theme operations ===")
    for theme in sorted(ops.keys()):
        op = ops[theme]
        # Filter idempotent: skip add if already member, skip remove if already absent
        current_members = set(THEMES.get(theme, []))
        filtered_add = []
        for ticker, rationale in op["add"]:
            if ticker in current_members:
                skipped_already_member += 1
            else:
                filtered_add.append((ticker, rationale))
        filtered_remove = []
        for ticker in op["remove"]:
            if ticker not in current_members:
                skipped_already_absent += 1
            else:
                filtered_remove.append(ticker)

        if not filtered_add and not filtered_remove:
            continue

        m = find_theme_block(src, theme)
        if not m:
            missing_themes.append(theme)
            continue
        opening, body, closing = m.group(1), m.group(2), m.group(3)

        # Apply removals first
        for ticker in filtered_remove:
            body, did = remove_ticker_from_body(body, ticker)
            if did:
                total_removed += 1
            else:
                print(f"  ! [{theme}] could not locate '{ticker}' for removal")

        # Then apply additions
        body = insert_additions(body, filtered_add)
        total_added += len(filtered_add)

        # Splice back
        new_block = opening + body + closing
        src = src[: m.start()] + new_block + src[m.end():]

        adds_str = f"+{len(filtered_add)}" if filtered_add else ""
        rems_str = f"-{len(filtered_remove)}" if filtered_remove else ""
        joiner = " " if adds_str and rems_str else ""
        print(f"  [{theme:42s}] {adds_str}{joiner}{rems_str}")

    with open(THEME_MAP_PATH, "w", encoding="utf-8") as f:
        f.write(src)

    print("\n=== Summary ===")
    print(f"  Tickers added across themes:   {total_added}")
    print(f"  Tickers removed across themes: {total_removed}")
    print(f"  Skipped (already member):      {skipped_already_member}")
    print(f"  Skipped (already absent):      {skipped_already_absent}")
    print(f"  Skipped per session decision:  {len(skipped)} ({', '.join(t for t,_ in skipped)})")
    if missing_themes:
        print(f"  WARNING: themes not found in theme_map.py: {missing_themes}")


if __name__ == "__main__":
    apply()
