"""Dry-run helper for apply_audit_to_theme_map: compute and print operations, no write."""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "local_runner"))

import importlib.util
spec = importlib.util.spec_from_file_location("apply_mod", REPO_ROOT / "scripts" / "apply_audit_to_theme_map.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

import json
with open(mod.AUDIT_PATH, encoding="utf-8") as f:
    audit = json.load(f)

ops, skipped = mod.compute_operations(audit["placements"])

print(f"themes touched: {len(ops)}")
print(f"skipped per session decision (NRG/NXPI/QRVO/SWKS): {[t for t,_ in skipped]}")
print()

total_raw_adds = sum(len(v["add"]) for v in ops.values())
total_raw_rems = sum(len(v["remove"]) for v in ops.values())
print(f"planned ADDS (raw):    {total_raw_adds}")
print(f"planned REMOVES (raw): {total_raw_rems}")
print()

from theme_map import THEMES

total_real_adds = 0
total_real_rems = 0
print("Per-theme summary (after idempotency filter):")
for theme in sorted(ops.keys()):
    op = ops[theme]
    current = set(THEMES.get(theme, []))
    real_adds = [(t, r) for t, r in op["add"] if t not in current]
    real_removes = [t for t in op["remove"] if t in current]
    if not real_adds and not real_removes:
        continue
    total_real_adds += len(real_adds)
    total_real_rems += len(real_removes)
    add_tickers = sorted([t for t, _ in real_adds])
    print(f"  {theme:38s} ADD={len(real_adds):3d}  REM={len(real_removes):2d}")
    if real_removes:
        print(f"      remove: {real_removes}")
    if add_tickers:
        if len(add_tickers) > 12:
            print(f"      add: {add_tickers[:12]} ...({len(add_tickers)-12} more)")
        else:
            print(f"      add: {add_tickers}")
print()
print(f"TOTAL real ADDS:    {total_real_adds}")
print(f"TOTAL real REMOVES: {total_real_rems}")
