"""Apply the high-confidence ungrouped-ticker placements from
propose_theme_placements.py directly to local_runner/theme_map.py.

The output of propose_theme_placements.main() is a dict of
ticker -> (theme_id, reason). For each placement, we locate the
target theme's list in theme_map.py and append a new entry just
before its closing ']' with a rationale comment explaining the
auto-placement signals that fired.

Re-running is safe: existing tickers (already members) are skipped.
"""
import os, re, sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'local_runner'))

from theme_map import THEMES
from propose_theme_placements import main as propose

THEME_MAP_PATH = os.path.join(os.path.dirname(__file__), '..',
                              'local_runner', 'theme_map.py')


def apply_placements(placements):
    with open(THEME_MAP_PATH, 'r', encoding='utf-8') as f:
        src = f.read()

    # Group placements by theme
    by_theme = {}
    for tk, (theme, reason) in placements.items():
        # Skip if ticker is already in the theme (idempotent re-runs)
        if tk in THEMES.get(theme, []):
            continue
        by_theme.setdefault(theme, []).append((tk, reason))

    n_added = 0
    for theme, entries in by_theme.items():
        # Find the theme's list opening: "theme_id": [ ... ]
        # Match the literal key, allow any whitespace before "[", then
        # capture everything up to the matching "]" — themes are flat
        # lists of strings + comments, no nested brackets.
        pattern = re.compile(
            r'(["\']' + re.escape(theme) + r'["\']\s*:\s*\[)([^\[\]]*?)(\]\s*,?)',
            re.DOTALL,
        )
        m = pattern.search(src)
        if not m:
            print(f'  WARNING: could not find theme "{theme}" in theme_map.py')
            continue

        opening = m.group(1)
        body    = m.group(2)
        closing = m.group(3)

        # Build the new entries, one per line, indented to match the
        # existing style (8 spaces).
        new_lines = []
        for tk, reason in sorted(entries):
            new_lines.append(
                f'        "{tk:6s}", # rationale: auto-placed ({reason})'
            )
        addition = ('\n' + '\n'.join(new_lines)
                    if not body.rstrip().endswith(',') else
                    '\n' + '\n'.join(new_lines))

        # Ensure body ends with a trailing comma + newline so our insert
        # lands cleanly above the closing bracket.
        body_stripped = body.rstrip()
        if not body_stripped.endswith(','):
            body_stripped += ','

        new_body = body_stripped + addition + '\n    '
        new_block = opening + new_body + closing
        src = src[:m.start()] + new_block + src[m.end():]
        n_added += len(entries)
        print(f'  + [{theme}] added {len(entries)} tickers')

    with open(THEME_MAP_PATH, 'w', encoding='utf-8') as f:
        f.write(src)
    print(f'\nTotal tickers added across themes: {n_added}')


if __name__ == '__main__':
    placements = propose()
    print(f'\nApplying {len(placements)} placements to theme_map.py...')
    apply_placements(placements)
