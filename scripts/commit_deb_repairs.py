#!/usr/bin/env python3
"""Commit only verified repaired paths, with a non-force stale-HEAD guard."""
import json
import os
from pathlib import Path
import subprocess


def git(*args):
    return subprocess.check_output(['git', *args], text=True).strip()


captured = git('rev-parse', 'HEAD')
report = json.loads(Path('deb-repair-report.json').read_text())
if not report['applied'] or report['blocking']:
    raise SystemExit('Blocking audit; refusing commit')
paths = report['repaired_paths']
# Only filenames from the validated manifest; never stage unrelated changes.
if any(not p.startswith('debs/') or '/' in p[5:] or not p.endswith('.deb') for p in paths):
    raise SystemExit('Unsafe repaired manifest path')
remote = git('ls-remote', 'origin', 'refs/heads/main').split()[0]
if remote != captured:
    raise SystemExit('Remote main advanced; refusing stale commit/deployment. Run latest workflow.')
if paths:
    git('config', 'user.name', 'github-actions[bot]')
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
    git('add', '--', *paths)
    if set(git('diff', '--cached', '--name-only').splitlines()) != set(paths):
        raise SystemExit('Unexpected staged paths')
    git('commit', '-m', 'Repair unreadable deb control permissions (verified; data unchanged)')
    # Check again directly before non-force push; a concurrent push also rejects at Git.
    if git('ls-remote', 'origin', 'refs/heads/main').split()[0] != captured:
        raise SystemExit('Remote main advanced before push; refusing deployment')
    git('push', 'origin', 'HEAD:refs/heads/main')
result = git('rev-parse', 'HEAD')
with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
    output.write(f'commit={result}\n')
print(f'Build/deployment source commit: {result}')
