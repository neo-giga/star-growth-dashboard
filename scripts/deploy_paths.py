"""Refuse to deploy a run whose UI/scripts are not live main's: compare the digest of the deploy-relevant paths at two revisions.

DEPLOY_PATHS is exactly the push trigger's path filter in .github/workflows/weekly-refresh.yml (tests/test_workflow.py asserts
the two lists match). A change on main outside them (tests, docs) leaves the digest equal, so it never strands a running refresh
or UI release; a change inside them triggers its own run, so this one stops and that one publishes.
"""
import argparse, hashlib, subprocess, sys
DEPLOY_PATHS = ('index.html', 'app.js', 'style.css', 'llms.txt', 'docs/agent-feed.md', 'scripts/**', 'trending/**',
                '.github/workflows/weekly-refresh.yml')


def digest(rev, cwd='.'):
    """SHA-256 over `git ls-tree -r` (mode, blob ID, path) of DEPLOY_PATHS at rev."""
    paths = [path[:-3] if path.endswith('/**') else path for path in DEPLOY_PATHS]
    listing = subprocess.run(['git', 'ls-tree', '-r', '--full-tree', rev, '--', *paths], cwd=cwd, check=True, capture_output=True, text=True).stdout
    return hashlib.sha256(listing.encode()).hexdigest()


def main():
    p = argparse.ArgumentParser(); p.add_argument('run_rev'); p.add_argument('main_rev'); a = p.parse_args()
    ours, live = digest(a.run_rev), digest(a.main_rev)
    if ours != live:
        print(f'newer UI/scripts on main ({live[:12]}, this run {ours[:12]}); a new run is queued', file=sys.stderr)
        return 1
    print(f'deploy-relevant sources match live main ({ours[:12]})')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
