#!/usr/bin/env python3
"""Run this locally to restore scanperfect.py from the last good commit.

Usage:
    python restore_scanperfect.py
    git add scanperfect.py && git commit -m 'Restore scanperfect.py' && git push origin v2
"""
import subprocess, sys

GOOD_COMMIT = 'f4e705ce2b0448431a069de26dfc2eb75ab1ccb1'

print(f'Restoring scanperfect.py from {GOOD_COMMIT}...')
result = subprocess.run(
    ['git', 'checkout', GOOD_COMMIT, '--', 'scanperfect.py'],
    capture_output=True, text=True
)
if result.returncode == 0:
    print('Done! Now run:')
    print('  git add scanperfect.py')
    print('  git commit -m "Restore scanperfect.py from pre-corruption commit"')
    print('  git push origin v2')
else:
    print('Error:', result.stderr)
    sys.exit(1)
