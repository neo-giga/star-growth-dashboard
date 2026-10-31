"""The page publication workflow: GITHUB_TOKEN only, contents read-only, no git writes; after each daily capture, on a push to main of
the deploy paths, and on dispatch; the gate step red before the upload and deploy steps (B2: windows/latest.json is built from
the branch first, then a stale file, low coverage and a null-heavy column each refuse; a good file stages; no capture yet is red),
the data-branch checkout on a real remote, the stale-run guard."""
import datetime as dt, importlib.util, json, os, pathlib, re, shutil, subprocess, sys, tempfile, unittest
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from capture.history import WINDOWS, pacific_date
from capture.store import DataDir
_spec = importlib.util.spec_from_file_location('deploy_paths', ROOT / 'scripts/deploy_paths.py'); _m = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_m); DEPLOY_PATHS = _m.DEPLOY_PATHS
WORKFLOW = (ROOT / '.github/workflows/publish-page.yml').read_text()
CAPTURE = (ROOT / '.github/workflows/daily-capture.yml').read_text()
SITE_FILES = ('index.html', 'app.js', 'style.css', 'llms.txt', '.nojekyll', 'docs/agent-feed.md', 'docs/publish-page.yml', 'scripts/publish_page.py', 'scripts/build_static_release.py',
              'publish/__init__.py', 'publish/stage.py', 'publish/agent_feed.py', 'scripts/build_windows.py', *(f'capture/{p.name}' for p in sorted((ROOT / 'capture').glob('*.py'))))


def step_script(name):
    return re.search(rf'      - name: {re.escape(name)}\n(?:        (?!run: \|).*\n)*        run: \|\n((?:          .*\n)+)', WORKFLOW)[1]


def data_branch(root, captured_at, total=200, drop_90d=0, missed=()):
    """A checkout of the data branch as the capture commits it (no windows/latest.json): 200 repos, a snapshot today and N days
    before for every window, a year of snapshots behind it; `drop_90d` repos missing from the 90-day baseline (no history: null);
    `missed`: windows whose baseline day's capture did not run."""
    data, date = DataDir(root), pacific_date(captured_at)
    repos = {f'R_{i:03}': {'full_name': f'o/r{i}', 'language': 'Go', 'created_at': '2020-01-01T00:00:00Z', 'first_seen': '2025-01-01'} for i in range(200)}
    data.write_repos({'threshold': 2000, 'updated_at': captured_at.isoformat(), 'repos': repos})
    for days in (0, *(days for days in WINDOWS if days not in missed)):
        counts = {node_id: 5000 + i - days for i, node_id in enumerate(repos)}
        if days == 90:
            counts = dict(list(counts.items())[drop_90d:])
        data.write_snapshot(date - dt.timedelta(days=days), (captured_at - dt.timedelta(days=days)).isoformat(), counts, 2000)
    data.write_meta({'first_snapshot_date': (date - dt.timedelta(days=365)).isoformat(), 'last_total_count': total, 'last_capture_date': date.isoformat()})
    data.write_diagnostic(date, {'date': date.isoformat(), 'captured_at': captured_at.isoformat(), 'discovery': {'status': 'complete'},
                                 'coverage': {'total_count': total, 'total_count_source': 'search'}})


class PublishWorkflowTests(unittest.TestCase):
    def test_docs_copy_is_byte_identical(self):
        self.assertEqual((ROOT / 'docs/publish-page.yml').read_bytes(), (ROOT / '.github/workflows/publish-page.yml').read_bytes())
        self.assertFalse((ROOT / '.github/workflows/weekly-refresh.yml').exists()); self.assertFalse((ROOT / 'docs/weekly-refresh.yml').exists())

    def test_github_token_only_nothing_committed_contents_read_only(self):
        for forbidden in ('git push', 'git commit', 'git add', 'contents: write', 'actions: write', 'secrets.', '--ignore-gate', '--published-site', 'bootstrap'):
            self.assertNotIn(forbidden, WORKFLOW)
        self.assertEqual(WORKFLOW.count('contents: read'), 2)  # top level and the job
        self.assertRegex(WORKFLOW, r'\n    permissions:\n      contents: read\n      pages: write\n      id-token: write\n')
        self.assertEqual(re.findall(r'^  \w+:\n', WORKFLOW.split('jobs:\n', 1)[1], re.M), ['  publish:\n'])
        self.assertIn("github.ref == 'refs/heads/main'", WORKFLOW); self.assertIn("github.repository == 'neo-giga/star-growth-dashboard'", WORKFLOW)
        self.assertIn('persist-credentials: false', WORKFLOW); self.assertIn('python3 -m unittest discover -s tests', WORKFLOW)

    def test_triggers_after_the_daily_capture_on_deploy_path_pushes_and_on_dispatch(self):
        on = WORKFLOW.split('permissions:', 1)[0]
        capture_name = re.search(r'^name: (.*)$', CAPTURE, re.M)[1]
        self.assertRegex(on, rf"\n  workflow_run:\n    workflows: \['{re.escape(capture_name)}'\]\n    types: \[completed\]\n")
        self.assertIn("github.event_name != 'workflow_run' || github.event.workflow_run.head_branch == 'main'", WORKFLOW)
        self.assertRegex(on, r'\n  push:\n    branches: \[main\]')
        paths = re.search(r'    paths:\n((?:      - .*\n)+)', on)[1].split()[1::2]
        self.assertEqual(tuple(paths), DEPLOY_PATHS)  # the push filter and the stale-run guard share one list
        for name in ('index.html', 'app.js', 'style.css', 'publish/**', 'scripts/**', 'docs/agent-feed.md', 'llms.txt', '.github/workflows/publish-page.yml'):
            self.assertIn(name, paths)
        self.assertNotIn('tests/**', paths); self.assertNotIn('trending/**', paths)
        self.assertRegex(on, r'\n  workflow_dispatch:\n'); self.assertNotIn('schedule:', WORKFLOW)  # the capture's completion is the schedule
        group = re.search(r'\nconcurrency:\n  group: (.*)\n  cancel-in-progress: false\n', WORKFLOW)[1]
        self.assertEqual(group, 'public-feed-publication'); self.assertNotEqual(group, re.search(r'\n  group: (.*)\n', CAPTURE)[1])

    def test_the_gate_step_fails_before_the_upload_and_deploy_steps(self):
        job = WORKFLOW.split('    steps:\n', 1)[1]
        names = [n or u for n, u in re.findall(r'      - (?:name: (.*)|uses: (.*))\n', job)]
        self.assertEqual(names, ['actions/checkout@v4', 'Unit checks (no network fixtures)', 'Read the data branch', 'Stage the page from windows/latest.json, or refuse', 'Preserve the diagnostic',
                                 'Refuse to deploy a stale run', 'Upload exact staged Pages artifact', 'Deploy exact artifact'])
        stage = step_script('Stage the page from windows/latest.json, or refuse')
        self.assertNotIn('set +e', stage); self.assertNotIn('|| true', stage); self.assertNotIn('exit 0', stage)
        self.assertIn('python3 scripts/publish_page.py --site-dir . --data-dir data-branch --output-dir "$RUNNER_TEMP/site"', stage)
        self.assertIn('--diagnostic "$RUNNER_TEMP/publish-diagnostic.json"', stage)
        for name in ('Stage the page from windows/latest.json, or refuse', 'Refuse to deploy a stale run', 'Upload exact staged Pages artifact', 'Deploy exact artifact'):
            self.assertNotIn('        if:', job.split(f'      - name: {name}\n', 1)[1].split('      - name:', 1)[0].split('      - uses:', 1)[0], name)
            self.assertNotIn('continue-on-error', job.split(f'      - name: {name}\n', 1)[1].split('      - name:', 1)[0])
        self.assertIn('      - name: Preserve the diagnostic\n        if: always()\n', job)
        self.assertRegex(job, r'uses: actions/upload-pages-artifact@v3\n        with:\n          path: \$\{\{ runner.temp \}\}/site\n')
        self.assertIn('python3 scripts/deploy_paths.py HEAD FETCH_HEAD', step_script('Refuse to deploy a stale run'))

    def test_b2_the_gate_refuses_stale_low_coverage_and_null_heavy_files_and_stages_a_good_one(self):
        stage = step_script('Stage the page from windows/latest.json, or refuse')
        self.assertLess(stage.index('python3 scripts/build_windows.py --data-dir data-branch'), stage.index('python3 scripts/publish_page.py'))  # built, then gated
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        good, stale = now - dt.timedelta(hours=2), now - dt.timedelta(hours=37)
        cases = {'stale': (stale, 200, 0, (), 'over 36 h'), 'low-coverage': (good, 205, 0, (), 'under 99% of the search total 205'), 'null-heavy': (good, 200, 3, (), 'gain_90d is null for 3 of 200 rows (1.5%)'),
                 'good': (good, 200, 0, (), None), 'missed-7-days-ago': (good, 200, 0, (7,), None)}
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            for case, (captured_at, total, drop, missed, problem) in cases.items():
                with self.subTest(case=case):
                    work = tmp / case; work.mkdir()
                    for name in SITE_FILES:
                        (work / name).parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(ROOT / name, work / name)
                    data_branch(work / 'data-branch', captured_at, total, drop, missed)
                    runner = tmp / f'runner-{case}'; runner.mkdir()
                    env = dict(os.environ, RUNNER_TEMP=str(runner), PATH=f'{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}')
                    proc = subprocess.run(['bash', '-e', '-c', stage], cwd=work, capture_output=True, text=True, env=env)
                    diagnostic = json.loads((runner / 'publish-diagnostic.json').read_text())
                    if problem:
                        self.assertNotEqual(proc.returncode, 0, case); self.assertEqual(diagnostic['status'], 'refused'); self.assertIn(problem, '; '.join(diagnostic['problems']))
                        self.assertFalse((runner / 'site').exists(), case); self.assertIn('current site unchanged', proc.stderr)  # the job stops here: nothing to upload or deploy
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr); self.assertEqual(diagnostic['status'], 'ok'); self.assertEqual(diagnostic['row_count'], 200)
                        self.assertTrue((runner / 'site/index.html').is_file()); self.assertTrue((runner / 'site/api/v2/catalog.json').is_file())
                        self.assertEqual([w['source'] for w in diagnostic['windows']], ['history' if w in missed else 'snapshot' for w in WINDOWS])  # read from the rebuilt file
                        self.assertEqual(diagnostic['measurements']['null_exempt'], [f'gain_{days}d' for days in missed])  # the missed day's column is all null, yet staged
            work = tmp / 'no-capture'; shutil.copytree(tmp / 'good', work, ignore=shutil.ignore_patterns('data-branch'))
            DataDir(work / 'data-branch').write_meta({})  # the branch exists but holds no capture: build_windows.py exits 2
            runner = tmp / 'runner-no-capture'; runner.mkdir()
            proc = subprocess.run(['bash', '-e', '-c', stage], cwd=work, capture_output=True, text=True, env=dict(env, RUNNER_TEMP=str(runner)))
            self.assertEqual(proc.returncode, 2, proc.stderr); self.assertIn('no capture in this data dir', proc.stderr)
            self.assertFalse((runner / 'site').exists()); self.assertFalse((work / 'data-branch/windows/latest.json').exists())

    def test_the_data_branch_step_reads_the_branch_tip_or_fails_without_one(self):
        checkout = step_script('Read the data branch')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@t', GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@t')
            git = lambda *a, cwd: subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git')], check=True)
            (tmp / 'work').mkdir(); git('init', '-q', '-b', 'main', cwd=tmp / 'work'); (tmp / 'work/README').write_text('x'); git('add', '-A', cwd=tmp / 'work'); git('commit', '-qm', 'main', cwd=tmp / 'work')
            git('remote', 'add', 'origin', (tmp / 'origin.git').as_uri(), cwd=tmp / 'work'); git('push', '-q', 'origin', 'main', cwd=tmp / 'work')
            subprocess.run(['git', 'clone', '-q', '--depth=1', (tmp / 'origin.git').as_uri(), str(tmp / 'runner')], check=True, env=env)
            run = lambda: subprocess.run(['bash', '-e', '-c', checkout], cwd=tmp / 'runner', capture_output=True, text=True, env=env)
            proc = run(); self.assertNotEqual(proc.returncode, 0); self.assertFalse((tmp / 'runner/data-branch').exists())  # no data branch yet: red, nothing published
            (tmp / 'data').mkdir(); git('init', '-q', '-b', 'data', cwd=tmp / 'data'); (tmp / 'data/windows').mkdir(); (tmp / 'data/windows/latest.json').write_text('{}')
            git('add', '-A', cwd=tmp / 'data'); git('commit', '-qm', 'capture', cwd=tmp / 'data'); git('push', '-q', (tmp / 'origin.git').as_uri(), 'HEAD:refs/heads/data', cwd=tmp / 'data')
            proc = run(); self.assertEqual(proc.returncode, 0, proc.stderr); self.assertEqual((tmp / 'runner/data-branch/windows/latest.json').read_text(), '{}')
            self.assertFalse((tmp / 'runner/data-branch/README').exists())

    def test_the_stale_run_guard_ignores_changes_outside_the_deploy_paths(self):
        step = step_script('Refuse to deploy a stale run')
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp); git = lambda *a, cwd=tmp / 'work': subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@t', *a], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()
            subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(tmp / 'origin.git')], check=True); (tmp / 'work').mkdir(); git('init', '-q', '-b', 'main')
            for name in ('app.js', 'tests/test_x.py', 'scripts/deploy_paths.py', '.github/workflows/publish-page.yml'):
                (tmp / 'work' / name).parent.mkdir(parents=True, exist_ok=True); (tmp / 'work' / name).write_bytes((ROOT / name).read_bytes() if (ROOT / name).exists() else b'x')
            git('add', '-A'); git('commit', '-qm', 'run'); run = git('rev-parse', 'HEAD'); git('remote', 'add', 'origin', (tmp / 'origin.git').as_uri())
            env = dict(os.environ, PATH=f'{pathlib.Path(sys.executable).parent}{os.pathsep}{os.environ["PATH"]}')
            guard = lambda: subprocess.run(['bash', '-e', '-c', step], cwd=tmp / 'work', capture_output=True, text=True, env=env)
            def land(name, text):
                git('checkout', '-q', 'main'); (tmp / 'work' / name).write_text(text); git('commit', '-qam', name); git('push', '-q', 'origin', 'main'); git('checkout', '-q', '--detach', run)
            land('tests/test_x.py', 'y'); proc = guard(); self.assertEqual(proc.returncode, 0, proc.stderr); self.assertIn('deploy-relevant sources match live main', proc.stdout)
            land('app.js', 'changed'); proc = guard(); self.assertEqual(proc.returncode, 1); self.assertIn('newer UI/scripts on main', proc.stderr)


if __name__ == '__main__':
    unittest.main()
